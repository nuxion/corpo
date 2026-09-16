"""Sprint board: per-story merge & deployment state for a given iteration."""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from azure.devops.connection import Connection
from azure.devops.v7_1.work.models import TeamContext
from azure.devops.v7_1.work_item_tracking.models import Wiql
from msrest.authentication import BasicAuthentication

from ado_actions.fetch_test_plan import (
    BACKEND_REPO,
    DEFAULT_ORG,
    DEFAULT_PROJECT,
    DEV_BRANCH,
    FRONTEND_REPO,
    REPO_TO_RELEASE_DEF,
    _artifact_source_commit,
    _is_ancestor,
    collect_descendant_pr_refs,
    get_pat,
    slugify,
)


DONE_STATES = {"Closed", "Resolved", "Done"}


def resolve_iteration(work_client, project: str, team: str) -> Any | None:
    tc = TeamContext(project=project, team=team)
    iters = work_client.get_team_iterations(tc, timeframe="current")
    return iters[0] if iters else None


def get_user_stories(wit_client, project: str, iteration_path: str) -> list[dict[str, Any]]:
    safe_path = iteration_path.replace("'", "''")
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{project}' "
        "AND [System.WorkItemType] = 'User Story' "
        f"AND [System.IterationPath] = '{safe_path}' "
        "ORDER BY [System.Id]"
    )
    result = wit_client.query_by_wiql(Wiql(query=query))
    ids = [r.id for r in (result.work_items or [])]
    if not ids:
        return []
    fields = ["System.Title", "System.State", "System.AssignedTo"]
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        items = wit_client.get_work_items(ids=chunk, fields=fields)
        for wi in items:
            f = wi.fields or {}
            assigned = f.get("System.AssignedTo")
            if isinstance(assigned, dict):
                assigned = assigned.get("displayName")
            out.append({
                "id": wi.id,
                "title": f.get("System.Title", "") or "",
                "state": f.get("System.State", "") or "",
                "assigned_to": assigned,
            })
    out.sort(key=lambda s: s["id"])
    return out


def fetch_pipeline_state(
    release_client, git_client, project: str, repo_name: str, def_name: str, top: int
) -> dict[str, Any] | None:
    try:
        defs = release_client.get_release_definitions(project=project, search_text=def_name)
    except Exception as e:
        print(f"  warn: cannot fetch release defs for {def_name}: {e}", file=sys.stderr)
        return None
    match = next((d for d in defs if d.name == def_name), None)
    if not match:
        return None
    try:
        releases = release_client.get_releases(
            project=project,
            definition_id=match.id,
            top=top,
            expand="environments,artifacts",
        )
    except Exception as e:
        print(f"  warn: cannot fetch releases for {def_name}: {e}", file=sys.stderr)
        return None

    env_latest: dict[str, dict[str, Any]] = {}
    for rel in releases:
        commits = [c for c in (_artifact_source_commit(a) for a in (rel.artifacts or [])) if c]
        for env in (rel.environments or []):
            if env.status == "succeeded" and env.name not in env_latest:
                env_latest[env.name] = {
                    "release_id": rel.id,
                    "release_name": rel.name,
                    "commit": commits[0] if commits else None,
                }

    repo_id: str | None = None
    try:
        repo = git_client.get_repository(repository_id=repo_name, project=project)
        repo_id = getattr(repo, "id", None)
    except Exception:
        pass

    return {"env_latest": env_latest, "repo_id": repo_id}


def envs_for_shas(
    pipeline_state: dict[str, Any] | None,
    git_client,
    project: str,
    merge_shas: list[str],
) -> list[str]:
    if not pipeline_state or not merge_shas or not pipeline_state.get("repo_id"):
        return []
    found: list[str] = []
    for env_name, info in pipeline_state["env_latest"].items():
        c = info.get("commit")
        if not c:
            continue
        if any(_is_ancestor(git_client, project, pipeline_state["repo_id"], m, c) for m in merge_shas):
            found.append(env_name)
    return found


def analyze_story(
    wit_client, git_client, story_id: int, pipeline_states: dict[str, Any], project: str
) -> dict[str, dict[str, Any]]:
    pr_refs, _ = collect_descendant_pr_refs(wit_client, story_id)
    by_repo: dict[str, dict[str, Any]] = {
        BACKEND_REPO: {"merged_dev": False, "merged_release": False, "active": False, "merge_shas": []},
        FRONTEND_REPO: {"merged_dev": False, "merged_release": False, "active": False, "merge_shas": []},
    }
    for project_id, _, pr_id in pr_refs:
        try:
            pr = git_client.get_pull_request_by_id(pull_request_id=pr_id, project=project_id)
        except Exception:
            continue
        repo_name = getattr(getattr(pr, "repository", None), "name", "?") or "?"
        if repo_name not in by_repo:
            continue
        target = (pr.target_ref_name or "").replace("refs/heads/", "")
        status = pr.status
        merge_commit = getattr(pr, "last_merge_commit", None)
        merge_sha = getattr(merge_commit, "commit_id", None) if merge_commit else None
        slot = by_repo[repo_name]
        if status == "completed":
            if target == DEV_BRANCH:
                slot["merged_dev"] = True
            elif target.startswith("release/"):
                slot["merged_release"] = True
            if merge_sha:
                slot["merge_shas"].append(merge_sha)
        elif status == "active":
            slot["active"] = True

    out: dict[str, dict[str, Any]] = {}
    for repo_name, label in [(BACKEND_REPO, "backend"), (FRONTEND_REPO, "frontend")]:
        info = by_repo[repo_name]
        flags = []
        if info["merged_dev"]:
            flags.append("dev")
        if info["merged_release"]:
            flags.append("rel")
        if info["active"]:
            flags.append("open-PR")
        out[label] = {
            "merge": "+".join(flags) if flags else "-",
            "envs": envs_for_shas(pipeline_states.get(repo_name), git_client, project, info["merge_shas"]),
            "has_prs": bool(pr_refs and any(p for p in by_repo[repo_name]["merge_shas"])) or info["active"],
        }
    return out


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def print_table(rows: list[tuple[dict[str, Any], dict[str, Any]]], title_width: int) -> None:
    headers = ["ID", "State", "Title", "BE Merge", "FE Merge", "BE Envs", "FE Envs"]
    body: list[list[str]] = []
    for s, a in rows:
        body.append([
            str(s["id"]),
            s["state"],
            truncate(s["title"], title_width),
            a["backend"]["merge"],
            a["frontend"]["merge"],
            ",".join(a["backend"]["envs"]) or "-",
            ",".join(a["frontend"]["envs"]) or "-",
        ])
    cols = list(zip(*([headers] + body)))
    widths = [max(len(c) for c in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in body:
        print(fmt.format(*r))


def cmd_sprint(args: argparse.Namespace) -> int:
    pat = get_pat()
    conn = Connection(base_url=args.org, creds=BasicAuthentication("", pat))
    wit_client = conn.clients_v7_1.get_work_item_tracking_client()
    git_client = conn.clients_v7_1.get_git_client()
    release_client = conn.clients.get_release_client()
    work_client = conn.clients_v7_1.get_work_client()

    if args.iteration:
        iteration_path = args.iteration
        iteration_name = args.iteration
    else:
        team = args.team or args.project
        try:
            it = resolve_iteration(work_client, args.project, team)
        except Exception as e:
            print(
                f"ERROR: cannot resolve current iteration for team '{team}': {e}\n"
                "Pass --team <team-name> or --iteration <Project\\Path>.",
                file=sys.stderr,
            )
            return 1
        if not it:
            print(f"ERROR: no current iteration for team '{team}'.", file=sys.stderr)
            return 1
        iteration_path = it.path
        iteration_name = it.name

    print(f"Sprint: {iteration_name}")
    print(f"Path:   {iteration_path}")
    print()

    stories = get_user_stories(wit_client, args.project, iteration_path)
    if not stories:
        print("No User Stories in this iteration.")
        return 0

    print(f"Pre-fetching pipeline state ({args.releases_top} releases per pipeline)...")
    pipeline_states: dict[str, Any] = {}
    for repo_name, (_, def_name) in REPO_TO_RELEASE_DEF.items():
        pipeline_states[repo_name] = fetch_pipeline_state(
            release_client, git_client, args.project, repo_name, def_name, args.releases_top
        )

    print(f"Analyzing {len(stories)} story(ies)...\n")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for s in stories:
        analysis = analyze_story(wit_client, git_client, s["id"], pipeline_states, args.project)
        rows.append((s, analysis))

    print_table(rows, args.title_width)

    done = sum(1 for s, _ in rows if s["state"] in DONE_STATES)
    total = len(rows)
    remaining = total - done
    print()
    print(f"Summary: {done} done · {remaining} remaining · {total} total")
    print("Legend: BE/FE Merge — branches the story's PRs were merged into:")
    print("          dev      = at least one PR completed → dev")
    print("          rel      = at least one PR completed → release/* branch")
    print("          open-PR  = a PR exists but is still active (not merged)")
    print("          -        = no PR linked for this repo")
    print("        BE/FE Envs — environments whose latest succeeded release")
    print("          contains a story PR's merge commit (ancestry-checked).")
    return 0


class _HtmlToMd(HTMLParser):
    """Minimal HTML → Markdown converter for ADO rich-text fields."""

    BLOCK_TAGS = {"p", "div", "br", "tr"}
    LIST_TAGS = {"ul", "ol"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.list_stack: list[tuple[str, int]] = []
        self.in_pre = False
        self.in_code = False
        self.link_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in self.HEADING_TAGS:
            level = int(tag[1])
            self.out.append("\n\n" + "#" * level + " ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in {"p", "div"}:
            self.out.append("\n\n")
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "code":
            self.in_code = True
            self.out.append("`")
        elif tag == "pre":
            self.in_pre = True
            self.out.append("\n\n```\n")
        elif tag == "ul":
            self.list_stack.append(("ul", 0))
            self.out.append("\n")
        elif tag == "ol":
            self.list_stack.append(("ol", 0))
            self.out.append("\n")
        elif tag == "li":
            indent = "  " * max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1][0] == "ol":
                kind, n = self.list_stack[-1]
                self.list_stack[-1] = (kind, n + 1)
                self.out.append(f"\n{indent}{n + 1}. ")
            else:
                self.out.append(f"\n{indent}- ")
        elif tag == "a":
            self.link_href = a.get("href")
            self.out.append("[")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag == "tr":
            self.out.append("\n")
        elif tag in {"td", "th"}:
            self.out.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "code":
            self.in_code = False
            self.out.append("`")
        elif tag == "pre":
            self.in_pre = False
            self.out.append("\n```\n")
        elif tag in self.LIST_TAGS and self.list_stack:
            self.list_stack.pop()
            self.out.append("\n")
        elif tag == "a":
            href = self.link_href or ""
            self.link_href = None
            self.out.append(f"]({href})")

    def handle_data(self, data: str) -> None:
        self.out.append(data)

    def render(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(html: str | None) -> str:
    if not html:
        return ""
    parser = _HtmlToMd()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    return parser.render()


_WORK_ITEM_URL_RE = re.compile(
    r"https?://(?:dev\.azure\.com/(?P<org>[^/]+)/(?P<proj>[^/]+)|"
    r"(?P<orgsub>[^./]+)\.visualstudio\.com/(?P<proj2>[^/]+))"
    r"/_workitems/(?:edit|view)/(?P<id>\d+)",
    re.IGNORECASE,
)


def parse_work_item_ref(ref: str) -> tuple[int, str | None, str | None]:
    """Return (id, org_url, project) parsed from an int-string or a work-item URL."""
    ref = ref.strip()
    if ref.isdigit():
        return int(ref), None, None
    m = _WORK_ITEM_URL_RE.search(ref)
    if not m:
        raise ValueError(f"cannot parse work item id/url: {ref!r}")
    wid = int(m.group("id"))
    if m.group("org"):
        org_url = f"https://dev.azure.com/{m.group('org')}"
        project = urllib.parse.unquote(m.group("proj"))
    else:
        org_url = f"https://{m.group('orgsub')}.visualstudio.com"
        project = urllib.parse.unquote(m.group("proj2"))
    return wid, org_url, project


def _identity(value: Any) -> str:
    if isinstance(value, dict):
        name = value.get("displayName") or value.get("uniqueName") or ""
        email = value.get("uniqueName") or ""
        if name and email and name != email:
            return f"{name} <{email}>"
        return name or email or ""
    return str(value or "")


def render_work_item_md(
    wi: Any,
    org_url: str,
    project: str,
    info_map: dict[int, tuple[str, str]] | None = None,
    local_ids: set[int] | None = None,
) -> str:
    """Render a work item as Markdown.

    ``info_map`` maps work-item id -> (type, title) and is used to give the
    Parent/Children hierarchy readable labels. ``local_ids`` is the set of ids
    also rendered in the same document, so those references can be flagged as
    "included in this file".
    """
    info_map = info_map or {}
    local_ids = local_ids or set()

    def hierarchy_ref(wid: int) -> str:
        typ, ttl = info_map.get(wid, ("", ""))
        label = f"{typ} {wid}".strip()
        text = f"{label} — {ttl}" if ttl else label
        ref_url = f"{org_url.rstrip('/')}/{urllib.parse.quote(project)}/_workitems/edit/{wid}"
        tag = " _(included in this file)_" if wid in local_ids else ""
        return f"[{text}]({ref_url}){tag}"

    f: dict[str, Any] = wi.fields or {}
    title = f.get("System.Title", "") or f"Work Item {wi.id}"
    wtype = f.get("System.WorkItemType", "") or ""
    state = f.get("System.State", "") or ""
    reason = f.get("System.Reason", "") or ""
    assigned = _identity(f.get("System.AssignedTo"))
    created_by = _identity(f.get("System.CreatedBy"))
    changed_by = _identity(f.get("System.ChangedBy"))
    created = f.get("System.CreatedDate", "") or ""
    changed = f.get("System.ChangedDate", "") or ""
    area = f.get("System.AreaPath", "") or ""
    iteration = f.get("System.IterationPath", "") or ""
    tags = f.get("System.Tags", "") or ""
    priority = f.get("Microsoft.VSTS.Common.Priority", "")
    severity = f.get("Microsoft.VSTS.Common.Severity", "")
    url = f"{org_url.rstrip('/')}/{urllib.parse.quote(project)}/_workitems/edit/{wi.id}"

    lines: list[str] = []
    lines.append(f"# [{wtype} {wi.id}] {title}")
    lines.append("")
    lines.append(f"**URL:** {url}")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    rows = [
        ("ID", str(wi.id)),
        ("Type", wtype),
        ("State", state),
        ("Reason", reason),
        ("Assigned To", assigned),
        ("Area Path", area),
        ("Iteration Path", iteration),
        ("Priority", str(priority) if priority != "" else ""),
        ("Severity", str(severity) if severity != "" else ""),
        ("Tags", tags),
        ("Created", f"{created} by {created_by}".strip(" by")),
        ("Changed", f"{changed} by {changed_by}".strip(" by")),
    ]
    for k, v in rows:
        if v:
            lines.append(f"| {k} | {v} |")
    lines.append("")

    pid = parent_id(wi)
    kids = child_ids(wi)
    if pid is not None or kids:
        lines.append("## Hierarchy")
        lines.append("")
        if pid is not None:
            lines.append(f"- **Parent:** {hierarchy_ref(pid)}")
        if kids:
            lines.append(f"- **Children ({len(kids)}):**")
            for cid in kids:
                lines.append(f"  - {hierarchy_ref(cid)}")
        lines.append("")

    sections = [
        ("Description", "System.Description"),
        ("Acceptance Criteria", "Microsoft.VSTS.Common.AcceptanceCriteria"),
        ("Repro Steps", "Microsoft.VSTS.TCM.ReproSteps"),
        ("System Info", "Microsoft.VSTS.TCM.SystemInfo"),
    ]
    for label, key in sections:
        body = html_to_md(f.get(key))
        if body:
            lines.append(f"## {label}")
            lines.append("")
            lines.append(body)
            lines.append("")

    rels = list(getattr(wi, "relations", None) or [])
    if rels:
        lines.append("## Relations")
        lines.append("")
        for r in rels:
            rel_type = getattr(r, "rel", "") or ""
            r_url = getattr(r, "url", "") or ""
            attrs = getattr(r, "attributes", None) or {}
            comment = attrs.get("comment", "") if isinstance(attrs, dict) else ""
            suffix = f" — {comment}" if comment else ""
            lines.append(f"- **{rel_type}** {r_url}{suffix}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


TYPE_CODES: dict[str, str] = {
    "user story": "US",
    "task": "TASK",
    "epic": "EPIC",
    "feature": "FEATURE",
    "bug": "BUG",
}


def type_code(wtype: str) -> str:
    """Short filename prefix for a work-item type (e.g. 'User Story' -> 'US')."""
    key = (wtype or "").strip().lower()
    if not key:
        return "WORK"
    if key in TYPE_CODES:
        return TYPE_CODES[key]
    # Fallback: uppercase slug of whatever type it is.
    return slugify(wtype).upper().replace("-", "") or "WORK"


def parent_id(wi: Any) -> int | None:
    """Direct-parent work-item id from the hierarchy-reverse relation, if any."""
    for r in getattr(wi, "relations", None) or []:
        if getattr(r, "rel", "") != "System.LinkTypes.Hierarchy-Reverse":
            continue
        url = getattr(r, "url", "") or ""
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def child_ids(wi: Any) -> list[int]:
    """Direct-child work-item ids from hierarchy-forward relations (one level)."""
    ids: list[int] = []
    for r in getattr(wi, "relations", None) or []:
        if getattr(r, "rel", "") != "System.LinkTypes.Hierarchy-Forward":
            continue
        url = getattr(r, "url", "") or ""
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            ids.append(int(tail))
    return sorted(ids)


def collect_descendants(
    wit_client, root: Any, project: str, max_depth: int
) -> list[tuple[Any, int]]:
    """Pre-order DFS over the hierarchy below ``root``, up to ``max_depth`` levels.

    Returns [(work_item, depth), ...] where depth 1 = direct children,
    2 = grandchildren, etc. Pre-order means each parent is immediately
    followed by its own descendants (e.g. a Feature, then its User Stories,
    then each story's Tasks). Cycles are guarded against via ``visited``.
    """
    out: list[tuple[Any, int]] = []
    visited: set[int] = {root.id}

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        for cid in child_ids(node):
            if cid in visited:
                continue
            visited.add(cid)
            try:
                child = wit_client.get_work_item(id=cid, project=project, expand="All")
            except Exception as e:
                print(f"warn: cannot fetch child work item {cid}: {e}", file=sys.stderr)
                continue
            out.append((child, depth))
            walk(child, depth + 1)

    walk(root, 1)
    return out


def cmd_fetch_work(args: argparse.Namespace) -> int:
    try:
        wid, url_org, url_project = parse_work_item_ref(args.ref)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    org_url = args.org or url_org or DEFAULT_ORG
    project = args.project or url_project or DEFAULT_PROJECT

    pat = get_pat()
    conn = Connection(base_url=org_url, creds=BasicAuthentication("", pat))
    wit_client = conn.clients_v7_1.get_work_item_tracking_client()

    try:
        wi = wit_client.get_work_item(id=wid, project=project, expand="All")
    except Exception as e:
        print(f"ERROR: cannot fetch work item {wid}: {e}", file=sys.stderr)
        return 1

    # --depth controls how many hierarchy levels to descend; --include-child is
    # kept as a backward-compatible shorthand for --depth 1; --recursive descends
    # the whole tree (Epic -> Feature -> User Story -> Task ...).
    if args.recursive:
        max_depth = 100
    elif args.depth is not None:
        max_depth = args.depth
    elif args.include_child:
        max_depth = 1
    else:
        max_depth = 0

    items = [wi]
    if max_depth > 0:
        descendants = collect_descendants(wit_client, wi, project, max_depth)
        print(f"Including {len(descendants)} descendant work item(s) (depth <= {max_depth})")
        items.extend(child for child, _ in descendants)

    # Build id -> (type, title) so the Parent/Children sections are readable.
    # local_ids are the items rendered in this same document.
    local_ids = {it.id for it in items}
    info_map: dict[int, tuple[str, str]] = {}
    for it in items:
        itf = it.fields or {}
        info_map[it.id] = (
            itf.get("System.WorkItemType", "") or "",
            itf.get("System.Title", "") or "",
        )
    # Resolve titles for referenced parents/children we haven't fetched
    # (e.g. the root's own parent, or children beyond --depth).
    referenced: set[int] = set()
    for it in items:
        pid = parent_id(it)
        if pid is not None:
            referenced.add(pid)
        referenced.update(child_ids(it))
    missing = sorted(referenced - set(info_map))
    for i in range(0, len(missing), 200):
        batch = missing[i : i + 200]
        try:
            fetched = wit_client.get_work_items(
                ids=batch, project=project, fields=["System.Title", "System.WorkItemType"]
            )
        except Exception as e:
            print(f"warn: cannot resolve titles for {batch}: {e}", file=sys.stderr)
            continue
        for m in fetched:
            mf = m.fields or {}
            info_map[m.id] = (
                mf.get("System.WorkItemType", "") or "",
                mf.get("System.Title", "") or "",
            )

    def item_filename(it: Any) -> str:
        itf = it.fields or {}
        return (
            f"{type_code(itf.get('System.WorkItemType', '') or '')}"
            f"-{it.id}-{slugify(itf.get('System.Title', '') or '')}.md"
        )

    if args.split:
        # One file per work item, inside the output folder.
        if args.stdout or args.out:
            print("ERROR: --split cannot be combined with --stdout or --out.", file=sys.stderr)
            return 1
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for it in items:
            path = out_dir / item_filename(it)
            path.write_text(
                render_work_item_md(it, org_url, project, info_map, local_ids),
                encoding="utf-8",
            )
        print(f"Wrote {len(items)} file(s) to {out_dir}/")
        return 0

    parts: list[str] = []
    for idx, it in enumerate(items):
        if idx:
            parts.append("---\n")
        parts.append(render_work_item_md(it, org_url, project, info_map, local_ids))
    md = "\n".join(parts)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = out_dir / item_filename(wi)

    if args.stdout:
        sys.stdout.write(md)
    else:
        out_path.write_text(md, encoding="utf-8")
        print(f"Wrote {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="board")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sprint", help="Show merge & deployment status for stories in a sprint.")
    sp.add_argument("--project", default=DEFAULT_PROJECT)
    sp.add_argument("--org", default=DEFAULT_ORG)
    sp.add_argument(
        "--team",
        default=None,
        help="Team name (defaults to project name). Used to resolve the current iteration.",
    )
    sp.add_argument(
        "--iteration",
        default=None,
        help="Explicit iteration path (e.g. 'example-project\\\\PI 2 Sprint 7'). Skips current-iteration lookup.",
    )
    sp.add_argument(
        "--releases-top",
        type=int,
        default=10,
        help="Recent releases to inspect per pipeline (default: 10).",
    )
    sp.add_argument(
        "--title-width",
        type=int,
        default=50,
        help="Truncate story title to this many chars (default: 50).",
    )
    sp.set_defaults(func=cmd_sprint)

    fw = sub.add_parser(
        "fetch-work",
        help="Download a single work item as Markdown.",
    )
    fw.add_argument(
        "ref",
        help="Work item ID (e.g. 7102940) or full URL "
        "(e.g. https://dev.azure.com/<org>/<project>/_workitems/edit/7102940/).",
    )
    fw.add_argument(
        "--org",
        default=None,
        help=f"Override organization URL (default: parsed from URL or {DEFAULT_ORG}).",
    )
    fw.add_argument(
        "--project",
        default=None,
        help=f"Override project (default: parsed from URL or {DEFAULT_PROJECT}).",
    )
    fw.add_argument(
        "--out-dir",
        default="reports",
        help="Output directory for the markdown file (default: reports).",
    )
    fw.add_argument(
        "--out",
        default=None,
        help="Explicit output file path (overrides --out-dir naming).",
    )
    fw.add_argument(
        "--stdout",
        action="store_true",
        help="Write the markdown to stdout instead of a file.",
    )
    fw.add_argument(
        "--include-child",
        action="store_true",
        help="Also render the direct children (first level only), e.g. a "
        "Feature's child User Stories. Shorthand for --depth 1.",
    )
    fw.add_argument(
        "--depth",
        type=int,
        default=None,
        help="How many hierarchy levels to descend and render. "
        "1 = direct children, 2 = also grandchildren (e.g. an Epic's Features "
        "AND their User Stories), etc. Overrides --include-child.",
    )
    fw.add_argument(
        "--recursive",
        action="store_true",
        help="Descend the entire child hierarchy (Epic -> Feature -> User Story "
        "-> Task ...). Overrides --depth / --include-child.",
    )
    fw.add_argument(
        "--split",
        action="store_true",
        help="Write one file per work item into --out-dir (e.g. EPIC-*.md, "
        "FEATURE-*.md, US-*.md) instead of a single combined document. "
        "Cannot be used with --stdout or --out.",
    )
    fw.set_defaults(func=cmd_fetch_work)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
