"""Sprint board: per-story merge & deployment state for a given iteration."""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import click
from azure.devops.connection import Connection
from azure.devops.v7_1.work.models import TeamContext
from azure.devops.v7_1.work_item_tracking.models import Wiql
from msrest.authentication import BasicAuthentication

from ado_actions.cliargs import Args
from ado_actions.fetch_test_plan import (
    DEFAULT_ORG,
    DEFAULT_PROJECT,
    DEV_BRANCH,
    REPO_TO_RELEASE_DEF,
    _artifact_source_commit,
    _is_ancestor,
    collect_descendant_pr_refs,
    get_pat,
    slugify,
)


DONE_STATES = {"Closed", "Resolved", "Done"}

# Path segment that follows ``_sprints`` in a sprint URL and is a view name
# rather than the team name.
SPRINT_VIEWS = {"taskboard", "backlog", "capacity", "directory", "analytics"}


def parse_sprint_url(url: str) -> dict[str, str] | None:
    """Parse an ADO sprint URL into its org / project / team / iteration parts.

    Handles e.g.::

        https://dev.azure.com/<org>/<project>/_sprints/taskboard/<team>/<iteration path>
        https://<org>.visualstudio.com/<project>/_sprints/backlog/<team>/<iteration path>

    Segments are URL-decoded and the iteration path is rejoined with the
    backslashes ADO expects. Any query string (taskboard filters such as
    ``?System.State=...``) is ignored. Returns ``None`` when ``url`` is not a
    recognizable sprint URL.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    segments = [urllib.parse.unquote(s) for s in parsed.path.split("/") if s]
    if "_sprints" not in segments:
        return None
    idx = segments.index("_sprints")
    before, after = segments[:idx], segments[idx + 1 :]
    if not before:
        return None
    if parsed.netloc.lower().endswith("visualstudio.com"):
        org = f"{parsed.scheme}://{parsed.netloc}"
        project = before[-1]
    else:
        org = f"{parsed.scheme}://{parsed.netloc}/{before[0]}"
        project = before[-1] if len(before) > 1 else before[0]
    if after and after[0] in SPRINT_VIEWS:
        after = after[1:]
    out = {"org": org, "project": project}
    if after:
        out["team"] = after[0]
    if len(after) > 1:
        out["iteration"] = "\\".join(after[1:])
    return out


def resolve_iteration(work_client, project: str, team: str) -> Any | None:
    tc = TeamContext(project=project, team=team)
    iters = work_client.get_team_iterations(tc, timeframe="current")
    return iters[0] if iters else None


def team_area_clause(work_client, project: str, team: str) -> str | None:
    """WIQL clause restricting results to a team's area paths.

    An iteration path is project-wide: every team planning into the same sprint
    shares it. What makes a sprint *a team's* sprint is the team's area paths,
    which is what the taskboard filters on. Returns ``None`` when the team owns
    the project root (no useful restriction) or when the lookup fails.
    """
    tc = TeamContext(project=project, team=team)
    try:
        tfv = work_client.get_team_field_values(tc)
    except Exception as e:
        print(f"WARN: cannot read team field values for '{team}': {e}", file=sys.stderr)
        return None
    field = getattr(getattr(tfv, "field", None), "reference_name", None) or "System.AreaPath"
    clauses = []
    for v in getattr(tfv, "values", None) or []:
        value = (getattr(v, "value", "") or "").strip()
        if not value or value == project:
            # Team owns the project root; scoping would be a no-op.
            return None
        safe = value.replace("'", "''")
        clauses.append(
            f"[{field}] UNDER '{safe}'" if getattr(v, "include_children", False)
            else f"[{field}] = '{safe}'"
        )
    if not clauses:
        return None
    return "(" + " OR ".join(clauses) + ")"


def get_user_stories(
    wit_client, project: str, iteration_path: str, area_clause: str | None = None
) -> list[dict[str, Any]]:
    safe_path = iteration_path.replace("'", "''")
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{project}' "
        "AND [System.WorkItemType] = 'User Story' "
        f"AND [System.IterationPath] = '{safe_path}' "
        + (f"AND {area_clause} " if area_clause else "")
        + "ORDER BY [System.Id]"
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


PR_STATUS_CODE = {"active": "O", "completed": "M", "abandoned": "A"}
# Rendering order for the status letters of a repo with several PRs.
PR_STATUS_ORDER = "OMA"


def repo_release_state(
    build_client,
    release_client,
    repo: dict[str, Any],
    releases_top: int,
    cache: dict[str, Any],
    out,
) -> dict[str, Any]:
    """Infer the release pipelines that ship ``repo`` and their deployed commits.

    The chain is entirely derived from the repository -- nothing is configured:
    repo -> build definitions consuming it -> release definitions consuming
    those builds -> latest succeeded environment per release definition, with
    the commit its artifact was built from.

    Returns ``{"envs": {release_def_name: {env_name: commit_sha}}, "defs": n}``.
    ``defs`` counts release definitions found even when none has a succeeded
    deployment, so "no pipeline exists" stays distinguishable from "nothing
    deployed yet". Cached per repo id, since a sprint hits the same handful of
    repos over and over.
    """
    key = repo["id"]
    if key in cache:
        return cache[key]

    state: dict[str, dict[str, str]] = {}
    project = repo["project_name"]
    try:
        build_defs = list(
            build_client.get_definitions(
                project=project, repository_id=repo["id"], repository_type="TfsGit"
            )
        )
    except Exception as e:
        out(f"        {repo['name']}: cannot list build definitions ({e})")
        build_defs = []
    out(f"        {repo['name']}: {len(build_defs)} build def(s)")

    release_defs: dict[int, str] = {}
    for bd in build_defs:
        try:
            rds = release_client.get_release_definitions(
                project=project,
                artifact_type="Build",
                artifact_source_id=f"{repo['project_id']}:{bd.id}",
            )
        except Exception as e:
            out(f"        {repo['name']}: build {bd.id} release lookup failed ({e})")
            continue
        for rd in rds or []:
            release_defs[rd.id] = rd.name
    out(
        f"        {repo['name']}: {len(release_defs)} release def(s)"
        + (f" -> {', '.join(sorted(release_defs.values()))}" if release_defs else "")
    )

    for def_id, def_name in release_defs.items():
        try:
            releases = release_client.get_releases(
                project=project,
                definition_id=def_id,
                top=releases_top,
                expand="environments,artifacts",
            )
        except Exception as e:
            out(f"        {def_name}: cannot fetch releases ({e})")
            continue
        env_latest: dict[str, str] = {}
        for rel in releases or []:
            commits = [
                c for c in (_artifact_source_commit(a) for a in (rel.artifacts or [])) if c
            ]
            if not commits:
                continue
            for env in (rel.environments or []):
                if env.status == "succeeded" and env.name not in env_latest:
                    env_latest[env.name] = commits[0]
        if env_latest:
            state[def_name] = env_latest
        out(
            f"        {def_name}: "
            + (", ".join(f"{e}={c[:8]}" for e, c in env_latest.items()) or "no succeeded envs")
        )

    result = {"envs": state, "defs": len(release_defs)}
    cache[key] = result
    return result


def deployed_envs(
    git_client,
    repo: dict[str, Any],
    merge_shas: list[str],
    release_state: dict[str, Any],
    ancestor_cache: dict[tuple[str, str, str], bool],
) -> list[str]:
    """Environments whose latest succeeded deploy contains one of ``merge_shas``."""
    env_maps = (release_state or {}).get("envs") or {}
    if not merge_shas or not env_maps:
        return []
    found: list[str] = []
    for env_map in env_maps.values():
        for env_name, deployed_commit in env_map.items():
            if env_name in found:
                continue
            if any(
                _ancestor_cached(
                    git_client,
                    repo["project_name"],
                    ancestor_cache,
                    repo["id"],
                    sha,
                    deployed_commit,
                )
                for sha in merge_shas
            ):
                found.append(env_name)
    return found


def analyze_story(
    wit_client,
    git_client,
    build_client,
    release_client,
    story_id: int,
    releases_top: int,
    recursive: bool = False,
    repo_cache: dict[str, Any] | None = None,
    ancestor_cache: dict[tuple[str, str, str], bool] | None = None,
    out=None,
) -> dict[str, Any]:
    """Resolve per-repo PR state and deployment state for one story.

    Repos, and the pipelines that ship them, are discovered from the story's own
    PR links -- nothing is pre-configured, so a story is reported against
    whatever repos it actually touches.

    With ``recursive`` the story's descendants (Tasks, Bugs, ...) are walked too
    and their PRs fold up into the story's row; the story stays the unit of
    reporting either way.
    """
    out = out or (lambda _msg: None)
    repo_cache = repo_cache if repo_cache is not None else {}
    ancestor_cache = ancestor_cache if ancestor_cache is not None else {}

    pr_refs, visited = collect_descendant_pr_refs(
        wit_client, story_id, max_depth=4 if recursive else 0
    )
    out(
        f"      {len(pr_refs)} PR ref(s) from {len(visited)} work item(s)"
        f"{' (recursive)' if recursive else ''}"
    )

    repos: dict[str, dict[str, Any]] = {}
    for project_id, _, pr_id in pr_refs:
        try:
            pr = git_client.get_pull_request_by_id(pull_request_id=pr_id, project=project_id)
        except Exception as e:
            out(f"      PR !{pr_id}: cannot fetch ({e})")
            continue
        repository = getattr(pr, "repository", None)
        repo_id = getattr(repository, "id", None)
        repo_name = getattr(repository, "name", None)
        if not repo_id or not repo_name:
            out(f"      PR !{pr_id}: no repository on PR, skipped")
            continue
        repo_project = getattr(repository, "project", None)
        slot = repos.setdefault(
            repo_id,
            {
                "id": repo_id,
                "name": repo_name,
                "project_id": getattr(repo_project, "id", None),
                "project_name": getattr(repo_project, "name", None) or project_id,
                "statuses": set(),
                "merge_shas": [],
            },
        )
        status = pr.status
        code = PR_STATUS_CODE.get(status)
        if code:
            slot["statuses"].add(code)
        else:
            out(f"      PR !{pr_id}: unmapped status {status!r}")
        target = (pr.target_ref_name or "").replace("refs/heads/", "")
        merge_commit = getattr(pr, "last_merge_commit", None)
        merge_sha = getattr(merge_commit, "commit_id", None) if merge_commit else None
        if status == "completed" and merge_sha:
            slot["merge_shas"].append(merge_sha)
        out(
            f"      PR !{pr_id} {repo_name} -> {target or '?'} [{status}]"
            f"{' merge=' + merge_sha[:8] if merge_sha else ''}"
        )

    out_repos: list[dict[str, Any]] = []
    for repo in repos.values():
        release_state = repo_release_state(
            build_client, release_client, repo, releases_top, repo_cache, out
        )
        envs = deployed_envs(git_client, repo, repo["merge_shas"], release_state, ancestor_cache)
        out_repos.append({
            "name": repo["name"],
            "status": "".join(c for c in PR_STATUS_ORDER if c in repo["statuses"]),
            "envs": envs,
            "has_pipeline": bool(release_state.get("defs")),
        })
    out_repos.sort(key=lambda r: r["name"].lower())
    return {"repos": out_repos}


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def repos_cell(analysis: dict[str, Any]) -> str:
    """``repo:STATUS`` per repo the story touches (O=open, M=merged, A=abandoned)."""
    repos = analysis["repos"]
    if not repos:
        return "-"
    return ", ".join(f"{r['name']}:{r['status'] or '?'}" for r in repos)


def deploy_cell(analysis: dict[str, Any]) -> str:
    """Environments the story's merged code reached.

    Env names alone are ambiguous once a story spans repos -- "deployed to QAS"
    says nothing about *which* repo got there -- so the repo is prefixed
    whenever the story touches more than one, even if only one deployed.
    """
    repos = analysis["repos"]
    deployed = [r for r in repos if r["envs"]]
    if not deployed:
        if repos and not any(r["has_pipeline"] for r in repos):
            return "no-pipeline"
        return "-"
    if len(repos) == 1:
        return ",".join(deployed[0]["envs"])
    return ", ".join(f"{r['name']}:{','.join(r['envs'])}" for r in deployed)


def print_table(rows: list[tuple[dict[str, Any], dict[str, Any]]], title_width: int) -> None:
    headers = ["ID", "State", "Title", "Repos / PRs", "Deployed"]
    body: list[list[str]] = []
    for s, a in rows:
        body.append([
            str(s["id"]),
            s["state"],
            truncate(s["title"], title_width),
            repos_cell(a),
            deploy_cell(a),
        ])
    cols = list(zip(*([headers] + body)))
    widths = [max(len(c) for c in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in body:
        print(fmt.format(*r))


def cmd_sprint(args: Args) -> int:
    vprint = (lambda msg: print(msg)) if args.verbose else (lambda _msg: None)

    org = args.org
    project = args.project
    team = args.team
    iteration_path = args.iteration

    if args.target:
        from_url = parse_sprint_url(args.target)
        if from_url:
            org = from_url["org"]
            project = from_url["project"]
            team = from_url.get("team") or team
            iteration_path = from_url.get("iteration") or iteration_path
        elif args.target.lower().startswith(("http://", "https://")):
            print(
                f"ERROR: not a recognizable ADO sprint URL: {args.target}\n"
                "Expected .../<org>/<project>/_sprints/<view>/<team>/<iteration path>.",
                file=sys.stderr,
            )
            return 1
        else:
            iteration_path = args.target

    vprint(f"Org:    {org}")
    vprint(f"Project: {project}")
    vprint(f"Mode:   {'recursive (story + descendants)' if args.recursive else 'story-linked PRs only'}")

    pat = get_pat()
    conn = Connection(base_url=org, creds=BasicAuthentication("", pat))
    wit_client = conn.clients_v7_1.get_work_item_tracking_client()
    git_client = conn.clients_v7_1.get_git_client()
    build_client = conn.clients_v7_1.get_build_client()
    release_client = conn.clients.get_release_client()
    work_client = conn.clients_v7_1.get_work_client()

    if iteration_path:
        iteration_name = iteration_path
    else:
        team = team or project
        try:
            it = resolve_iteration(work_client, project, team)
        except Exception as e:
            print(
                f"ERROR: cannot resolve current iteration for team '{team}': {e}\n"
                "Pass --team <team-name>, --iteration <Project\\Path>, or a sprint URL.",
                file=sys.stderr,
            )
            return 1
        if not it:
            print(f"ERROR: no current iteration for team '{team}'.", file=sys.stderr)
            return 1
        iteration_path = it.path
        iteration_name = it.name

    area_clause = None
    if team and not args.all_teams:
        area_clause = team_area_clause(work_client, project, team)
        if area_clause is None:
            vprint(f"Scope:  whole project (team '{team}' has no area-path restriction)")
        else:
            vprint(f"Scope:  {area_clause}")
    elif args.all_teams:
        vprint("Scope:  whole project (--all-teams)")
    else:
        vprint("Scope:  whole project (no team given)")

    print(f"Sprint: {iteration_name}")
    print(f"Path:   {iteration_path}")
    if team:
        print(f"Team:   {team}" + ("" if area_clause else " (no area-path scope \u2014 whole project)"))
    print()

    stories = get_user_stories(wit_client, project, iteration_path, area_clause)
    if not stories:
        print("No User Stories in this iteration for the selected scope.")
        return 0

    # Repos and their pipelines are discovered per story and memoised here, so
    # each distinct repo costs one build-definition + release-definition lookup
    # for the whole sprint rather than one per story.
    repo_cache: dict[str, Any] = {}
    ancestor_cache: dict[tuple[str, str, str], bool] = {}

    total = len(stories)
    print(f"Analyzing {total} story(ies)...\n")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    started = time.monotonic()
    for idx, s in enumerate(stories, 1):
        t0 = time.monotonic()
        vprint(f"[{idx}/{total}] #{s['id']} [{s['state']}] {truncate(s['title'], args.title_width)}")
        analysis = analyze_story(
            wit_client,
            git_client,
            build_client,
            release_client,
            s["id"],
            args.releases_top,
            recursive=args.recursive,
            repo_cache=repo_cache,
            ancestor_cache=ancestor_cache,
            out=vprint,
        )
        vprint(
            f"      -> {repos_cell(analysis)} | deployed: {deploy_cell(analysis)}"
            f"  ({time.monotonic() - t0:.1f}s)"
        )
        rows.append((s, analysis))
    vprint(f"\nAnalyzed {total} story(ies) in {time.monotonic() - started:.1f}s\n")

    print_table(rows, args.title_width)

    done = sum(1 for s, _ in rows if s["state"] in DONE_STATES)
    total = len(rows)
    remaining = total - done
    print()
    print(f"Summary: {done} done \u00b7 {remaining} remaining \u00b7 {total} total")

    all_repos = sorted({r["name"] for _, a in rows for r in a["repos"]})
    all_envs = sorted({e for _, a in rows for r in a["repos"] for e in r["envs"]})
    print(f"Repos found in sprint ({len(all_repos)}): {', '.join(all_repos) or 'none'}")
    print(f"Envs found in sprint ({len(all_envs)}): {', '.join(all_envs) or 'none'}")
    print()
    print("Legend: Repos / PRs \u2014 one <repo>:<status> per repo the story touches,")
    print("          O = open PR, M = merged (completed), A = abandoned;")
    print("          letters combine when a repo has PRs in several states (e.g. OM).")
    print("        Deployed \u2014 environments whose latest succeeded release was built")
    print("          from a commit containing the story's merge commit (ancestry-checked).")
    print("          Prefixed <repo>:<env> whenever the story touches several repos.")
    print("          'no-pipeline' = the story's repos have no release definition, so")
    print("          deployment state is unknown rather than negative.")
    print("        Repos and pipelines are inferred from each story's linked PRs;")
    print("        nothing is configured. Use --recursive to fold child work items in.")
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


def resolve_depth(args: Args) -> int:
    """How many hierarchy levels the --children/--depth/--recursive flags ask for.

    --recursive wins over --depth, which wins over --children (a shorthand for
    one level). 0 means "the item itself only".
    """
    if getattr(args, "recursive", False):
        return 100
    if getattr(args, "depth", None) is not None:
        return args.depth
    if getattr(args, "children", False):
        return 1
    return 0


def cmd_fetch_work(args: Args) -> int:
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

    max_depth = resolve_depth(args)

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


# ---------------------------------------------------------------------------
# work-status: merge & deployment state for any work item (Task, Story, Epic…)
# ---------------------------------------------------------------------------


def build_release_snapshot(
    release_client, git_client, project: str, releases_top: int
) -> dict[str, dict[str, Any]]:
    """Fetch the recent releases of every configured pipeline, once.

    The expensive calls (definition lookup, release list, repo id) do not
    depend on the work item being inspected, so they are done a single time
    and reused for the root item and every descendant we report on.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for repo_name, (label, def_name) in REPO_TO_RELEASE_DEF.items():
        if not repo_name or not def_name:
            continue
        entry: dict[str, Any] = {
            "label": label,
            "def_name": def_name,
            "repo_id": None,
            "releases": [],
            "error": None,
            "warning": None,
        }
        snapshot[repo_name] = entry
        try:
            defs = release_client.get_release_definitions(project=project, search_text=def_name)
        except Exception as e:
            entry["error"] = f"error fetching definitions: {e}"
            continue
        match = next((d for d in defs if d.name == def_name), None)
        if not match:
            entry["error"] = f"definition '{def_name}' not found"
            continue
        try:
            releases = release_client.get_releases(
                project=project,
                definition_id=match.id,
                top=releases_top,
                expand="environments,artifacts",
            )
        except Exception as e:
            entry["error"] = f"error fetching releases: {e}"
            continue
        try:
            repo = git_client.get_repository(repository_id=repo_name, project=project)
            entry["repo_id"] = getattr(repo, "id", None)
        except Exception as e:
            entry["warning"] = f"could not resolve repo id for ancestry check ({e})"
        for rel in releases or []:
            artifacts = getattr(rel, "artifacts", None) or []
            entry["releases"].append({
                "id": rel.id,
                "name": rel.name,
                "status": rel.status,
                "commits": [c for c in (_artifact_source_commit(a) for a in artifacts) if c],
                "envs": [
                    {
                        "name": env.name,
                        "status": env.status,
                        "deployed_on": (
                            getattr(env, "time_to_deploy", None)
                            or getattr(env, "modified_on", None)
                            or getattr(env, "time_started", None)
                            or getattr(rel, "created_on", None)
                        ),
                    }
                    for env in (getattr(rel, "environments", None) or [])
                ],
            })
    return snapshot


def _ancestor_cached(
    git_client,
    project: str,
    cache: dict[tuple[str, str, str], bool],
    repo_id: str,
    merge_sha: str,
    commit: str,
) -> bool:
    key = (repo_id, merge_sha, commit)
    if key not in cache:
        cache[key] = _is_ancestor(git_client, project, repo_id, merge_sha, commit)
    return cache[key]


def collect_pr_details(
    wit_client,
    git_client,
    org_url: str,
    project: str,
    root_id: int,
    out,
) -> tuple[dict[str, list[dict[str, Any]]], list[tuple[str, str, int]], list[int]]:
    """Resolve every PR linked to ``root_id`` or any of its descendants.

    ``out`` is a printer (see ``_printer``); it only emits in verbose mode.
    """
    pr_refs, visited_ids = collect_descendant_pr_refs(wit_client, root_id)
    out(f"Linked Pull Requests ({len(pr_refs)}) — walked {len(visited_ids)} work item(s)")
    out("-" * 60)
    pr_details_by_repo: dict[str, list[dict[str, Any]]] = {}
    if not pr_refs:
        out("  _none found_")
    for project_id, _, pr_id in pr_refs:
        try:
            pr = git_client.get_pull_request_by_id(pull_request_id=pr_id, project=project_id)
        except Exception as e:
            out(f"  PR {pr_id}: error fetching ({e})")
            continue
        repo_name = getattr(getattr(pr, "repository", None), "name", "?") or "?"
        target = (pr.target_ref_name or "").replace("refs/heads/", "")
        source = (pr.source_ref_name or "").replace("refs/heads/", "")
        status = pr.status
        merge_commit = getattr(pr, "last_merge_commit", None)
        merge_sha = getattr(merge_commit, "commit_id", None) if merge_commit else None
        merged_to_dev = status == "completed" and target == DEV_BRANCH
        marker = "✓ merged→dev" if merged_to_dev else f"{status} → {target}"
        out(f"  [{repo_name}] PR !{pr_id}: {pr.title}")
        out(f"      {source} → {target}  [{marker}]  closed={pr.closed_date}")
        if merge_sha:
            out(f"      merge commit: {merge_sha}")
        out(f"      url: {org_url}/{project}/_git/{repo_name}/pullrequest/{pr_id}")
        pr_details_by_repo.setdefault(repo_name, []).append({
            "pr_id": pr_id,
            "status": status,
            "target": target,
            "merge_sha": merge_sha,
            "merged_to_dev": merged_to_dev,
        })
    out("")
    return pr_details_by_repo, pr_refs, visited_ids


def deployment_summary(
    git_client,
    project: str,
    snapshot: dict[str, dict[str, Any]],
    pr_details_by_repo: dict[str, list[dict[str, Any]]],
    cache: dict[tuple[str, str, str], bool],
    out,
) -> dict[str, dict[str, str]]:
    """Latest succeeded deployment per environment, flagged when it carries this item."""
    out("Release Pipelines")
    out("-" * 60)
    deploy_summary: dict[str, dict[str, str]] = {}
    for repo_name, entry in snapshot.items():
        label = entry["label"]
        out(f"\n  {label} — {entry['def_name']}  (repo: {repo_name})")
        if entry["error"]:
            out(f"    {entry['error']}")
            continue
        if entry["warning"]:
            out(f"    warning: {entry['warning']}")
        if not entry["releases"]:
            out("    _no releases found_")
            continue

        merge_shas = [
            p["merge_sha"] for p in pr_details_by_repo.get(repo_name, []) if p.get("merge_sha")
        ]
        repo_id = entry["repo_id"]

        # Releases come back newest-first, so the first succeeded hit per
        # environment is that environment's current deployment.
        env_latest: dict[str, dict[str, Any]] = {}
        for rel in entry["releases"]:
            includes_item = False
            if repo_id and rel["commits"] and merge_shas:
                includes_item = any(
                    _ancestor_cached(git_client, project, cache, repo_id, m, c)
                    for c in rel["commits"]
                    for m in merge_shas
                )
            env_summary = ", ".join(f"{e['name']}={e['status']}" for e in rel["envs"]) or "_no envs_"
            deployed = "  ← includes this item's PR merge commit (ancestor)" if includes_item else ""
            out(f"    R{rel['id']} {rel['name']}  [{rel['status']}]  {env_summary}{deployed}")
            for c in rel["commits"]:
                out(f"      source: {c}")
            for env in rel["envs"]:
                if env["status"] == "succeeded" and env["name"] not in env_latest:
                    env_latest[env["name"]] = {
                        "release_id": rel["id"],
                        "release_name": rel["name"],
                        "commits": rel["commits"],
                        "includes_item": includes_item,
                        "deployed_on": env["deployed_on"],
                    }

        out("\n    Latest succeeded deployment per environment:")
        if not env_latest:
            out("      _no successful deployments in the releases inspected_")
        env_summary_for_repo: dict[str, str] = {}
        for env_name in sorted(env_latest):
            info = env_latest[env_name]
            mark = "  ✓ includes this item" if info["includes_item"] else ""
            commit_short = (info["commits"][0][:8] + "…") if info["commits"] else "?"
            out(
                f"      {env_name:<6} R{info['release_id']} {info['release_name']}  "
                f"commit={commit_short}  at={info['deployed_on']}{mark}"
            )
            env_summary_for_repo[env_name] = "✓item" if info["includes_item"] else "-"
        deploy_summary[label] = env_summary_for_repo
    return deploy_summary


def _printer(enabled: bool, indent: str):
    """Return a print function that indents and can be switched off."""

    def emit(msg: str = "") -> None:
        if not enabled:
            return
        print(f"{indent}{msg}" if msg else "")

    return emit


def report_work_item_status(
    wit_client,
    git_client,
    snapshot: dict[str, dict[str, Any]],
    wi: Any,
    org_url: str,
    project: str,
    verbose: bool,
    cache: dict[tuple[str, str, str], bool],
    indent: str = "",
) -> None:
    """Print merge + deployment state for one work item and its subtree."""
    f = wi.fields or {}
    title = f.get("System.Title", "")
    wi_type = f.get("System.WorkItemType", "") or "Work Item"
    state = f.get("System.State", "")
    assigned = f.get("System.AssignedTo")
    if isinstance(assigned, dict):
        assigned = assigned.get("displayName")
    wi_url = f"{org_url}/{project}/_workitems/edit/{wi.id}"

    if indent:
        print(f"{indent}{wi_type} {wi.id}: {title}  [{state}]")
    else:
        print("=" * 72)
        print(f"{wi_type} {wi.id}: {title}")
        print("=" * 72)
        print(f"  Type:       {wi_type}")
        print(f"  State:      {state}")
        print(f"  Assigned:   {assigned or '_unassigned_'}")
        print(f"  Iteration:  {f.get('System.IterationPath', '')}")
        print(f"  URL:        {wi_url}")
        print()

    detail = _printer(verbose, indent)
    summary = _printer(True, f"{indent}  " if indent else "")

    pr_details_by_repo, pr_refs, _ = collect_pr_details(
        wit_client, git_client, org_url, project, wi.id, detail
    )
    deploy_summary = deployment_summary(
        git_client, project, snapshot, pr_details_by_repo, cache, detail
    )

    if verbose:
        detail("")
        detail("=" * 60)
        detail("Summary")
        detail("=" * 60)

    if not snapshot:
        summary("  No release pipelines configured "
                "(set AZDO_BACKEND_REPO / AZDO_FRONTEND_REPO and the *_RELEASE_DEF vars).")
    if not pr_refs:
        summary("  Code merged to dev: UNKNOWN (no linked PRs on this item or its descendants)")
    else:
        for repo_name, entry in snapshot.items():
            label = entry["label"]
            prs = pr_details_by_repo.get(repo_name, [])
            if not prs:
                summary(f"  {label} merged to dev: NO PRs FOUND")
                continue
            merged_dev = [p for p in prs if p.get("merged_to_dev")]
            other_completed = [
                p for p in prs if p.get("status") == "completed" and not p.get("merged_to_dev")
            ]
            active = [p for p in prs if p.get("status") == "active"]
            abandoned = [p for p in prs if p.get("status") == "abandoned"]
            parts = [f"{len(merged_dev)} merged→dev"]
            if other_completed:
                targets = ", ".join(sorted({p["target"] for p in other_completed}))
                parts.append(f"{len(other_completed)} merged to other branches ({targets})")
            if active:
                parts.append(f"{len(active)} still active")
            if abandoned:
                parts.append(f"{len(abandoned)} abandoned")
            verdict = "YES" if merged_dev else "NO"
            summary(f"  {label} merged to dev: {verdict} — {len(prs)} PR(s): {'; '.join(parts)}")

    # PRs living in repos outside REPO_TO_RELEASE_DEF would otherwise vanish
    # from the summary — common once you point this at an Epic spanning repos.
    for repo_name, prs in sorted(pr_details_by_repo.items()):
        if repo_name in snapshot:
            continue
        merged = [p for p in prs if p.get("status") == "completed"]
        summary(
            f"  {repo_name} (no release pipeline configured): "
            f"{len(prs)} PR(s), {len(merged)} completed"
        )

    for label, envs in deploy_summary.items():
        if not envs:
            summary(f"  {label} deployments: _no successful deployments seen_")
            continue
        item_envs = [e for e, v in envs.items() if v == "✓item"]
        if item_envs:
            summary(f"  {label} environments containing this item: {', '.join(sorted(item_envs))}")
        else:
            envs_list = ", ".join(sorted(envs))
            summary(
                f"  {label} latest succeeded envs: {envs_list}"
                "  (item PR not detected — see --verbose for the PR list)"
            )
    print()


def cmd_work_status(args: Args) -> int:
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
    git_client = conn.clients_v7_1.get_git_client()
    release_client = conn.clients.get_release_client()

    try:
        wi = wit_client.get_work_item(id=wid, project=project, expand="All")
    except Exception as e:
        print(f"ERROR: cannot fetch work item {wid}: {e}", file=sys.stderr)
        return 1

    max_depth = resolve_depth(args)

    snapshot = build_release_snapshot(release_client, git_client, project, args.releases_top)
    cache: dict[tuple[str, str, str], bool] = {}

    report_work_item_status(
        wit_client, git_client, snapshot, wi, org_url, project, args.verbose, cache
    )

    if max_depth > 0:
        descendants = collect_descendants(wit_client, wi, project, max_depth)
        print(f"Descendants ({len(descendants)}, depth <= {max_depth})")
        print("-" * 72)
        for child, depth in descendants:
            report_work_item_status(
                wit_client,
                git_client,
                snapshot,
                child,
                org_url,
                project,
                args.verbose,
                cache,
                indent="  " * depth,
            )
    return 0


@click.group("board")
def board_cli() -> None:
    """Sprints and work items."""


@board_cli.command("sprint")
@click.argument("target", required=False, default=None)
@click.option("--project", default=DEFAULT_PROJECT, show_default=True)
@click.option("--org", default=DEFAULT_ORG, show_default=True)
@click.option(
    "--team",
    default=None,
    help="Team name (defaults to project name). Used to resolve the current iteration.",
)
@click.option(
    "--iteration",
    default=None,
    help="Explicit iteration path (e.g. 'example-project\\\\PI 2 Sprint 7'). "
    "Skips current-iteration lookup.",
)
@click.option(
    "--all-teams",
    is_flag=True,
    help="Do not restrict to the team's area paths. An iteration is shared by every "
    "team planning into it, so this reports the whole project's stories for the "
    "sprint rather than the team's taskboard.",
)
@click.option(
    "--recursive",
    is_flag=True,
    help="Walk each story's child work items (Tasks, Bugs, ...) when collecting PRs "
    "for the BE/FE merge and env columns. Most PRs hang off children, but this "
    "costs several extra API calls per story.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print per-story progress: PRs found, target branches, resolved envs, timings.",
)
@click.option(
    "--releases-top",
    type=int,
    default=10,
    show_default=True,
    help="Recent releases to inspect per pipeline.",
)
@click.option(
    "--title-width",
    type=int,
    default=50,
    show_default=True,
    help="Truncate story title to this many chars.",
)
def cli_sprint(**kwargs: Any) -> None:
    """Show merge & deployment status for stories in a sprint.

    TARGET is a sprint URL (e.g. https://dev.azure.com/<org>/<project>/_sprints/
    taskboard/<team>/<iteration path>; query-string filters are ignored) or a bare
    iteration path. Org/project/team/iteration parsed from a URL override the
    corresponding flags.
    """
    raise SystemExit(cmd_sprint(Args(**kwargs)))


@board_cli.command("fetch-work")
@click.argument("ref")
@click.option(
    "--org",
    default=None,
    help=f"Override organization URL (default: parsed from URL or {DEFAULT_ORG}).",
)
@click.option(
    "--project",
    default=None,
    help=f"Override project (default: parsed from URL or {DEFAULT_PROJECT}).",
)
@click.option(
    "--out-dir",
    default="reports",
    show_default=True,
    help="Output directory for the markdown file.",
)
@click.option(
    "--out",
    default=None,
    help="Explicit output file path (overrides --out-dir naming).",
)
@click.option(
    "--stdout",
    "stdout",
    is_flag=True,
    help="Write the markdown to stdout instead of a file.",
)
@click.option(
    "--children",
    "--include-child",
    "children",
    is_flag=True,
    help="Also render the direct children (first level only), e.g. a "
    "Feature's child User Stories. Shorthand for --depth 1.",
)
@click.option(
    "--depth",
    type=int,
    default=None,
    help="How many hierarchy levels to descend and render. "
    "1 = direct children, 2 = also grandchildren (e.g. an Epic's Features "
    "AND their User Stories), etc. Overrides --children.",
)
@click.option(
    "--recursive",
    is_flag=True,
    help="Descend the entire child hierarchy (Epic -> Feature -> User Story "
    "-> Task ...). Overrides --depth / --children.",
)
@click.option(
    "--split",
    is_flag=True,
    help="Write one file per work item into --out-dir (e.g. EPIC-*.md, "
    "FEATURE-*.md, US-*.md) instead of a single combined document. "
    "Cannot be used with --stdout or --out.",
)
def cli_fetch_work(**kwargs: Any) -> None:
    """Download a single work item as Markdown.

    REF is a work item ID (e.g. 7102940) or a full URL
    (e.g. https://dev.azure.com/<org>/<project>/_workitems/edit/7102940/).
    """
    raise SystemExit(cmd_fetch_work(Args(**kwargs)))


@board_cli.command("work-status")
@click.argument("ref")
@click.option(
    "--org",
    default=None,
    help=f"Override organization URL (default: parsed from URL or {DEFAULT_ORG}).",
)
@click.option(
    "--project",
    default=None,
    help=f"Override project (default: parsed from URL or {DEFAULT_PROJECT}).",
)
@click.option(
    "--children",
    "--include-child",
    "children",
    is_flag=True,
    help="Also report each direct child separately. Shorthand for --depth 1.",
)
@click.option(
    "--depth",
    type=int,
    default=None,
    help="How many hierarchy levels to report on individually "
    "(1 = direct children, 2 = also grandchildren). Overrides --children.",
)
@click.option(
    "--recursive",
    is_flag=True,
    help="Report on the entire child hierarchy. Overrides --depth / --children.",
)
@click.option(
    "--releases-top",
    type=int,
    default=5,
    show_default=True,
    help="How many recent releases to inspect per pipeline.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Show the full PR list and per-release detail. Default is a summary per repo.",
)
def cli_work_status(**kwargs: Any) -> None:
    """Show merge & deployment status for any work item (Task, Story, Feature, Epic...).

    REF is a work item ID (e.g. 7102940) or a full URL
    (e.g. https://dev.azure.com/<org>/<project>/_workitems/edit/7102940).
    """
    raise SystemExit(cmd_work_status(Args(**kwargs)))
