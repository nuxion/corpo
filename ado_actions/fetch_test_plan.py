"""Fetch all test case definitions for an Azure DevOps Test Plan.

Writes a JSON dump and one Markdown file per unique test case under
`tests/plan-<planId>/<id>-<slug>.md`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication


# Org/project and repo/pipeline names are environment-specific and MUST NOT
# be hard-coded. Set them in .envrc (gitignored) alongside AZDO_PAT.
DEFAULT_ORG = os.environ.get("AZDO_ORG", "https://dev.azure.com/<org>")
DEFAULT_PROJECT = os.environ.get("AZDO_PROJECT", "<project>")

BACKEND_REPO = os.environ.get("AZDO_BACKEND_REPO", "")
FRONTEND_REPO = os.environ.get("AZDO_FRONTEND_REPO", "")
BACKEND_RELEASE_DEF = os.environ.get("AZDO_BACKEND_RELEASE_DEF", "")
FRONTEND_RELEASE_DEF = os.environ.get("AZDO_FRONTEND_RELEASE_DEF", "")
DEV_BRANCH = "dev"

REPO_TO_RELEASE_DEF: dict[str, tuple[str, str]] = {
    BACKEND_REPO: ("Backend", BACKEND_RELEASE_DEF),
    FRONTEND_REPO: ("Frontend", FRONTEND_RELEASE_DEF),
}


def slugify(text: str, max_len: int = 80) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "untitled"


def get_pat() -> str:
    pat = os.environ.get("AZDO_PAT") or os.environ.get("PAT")
    if not pat:
        sys.exit("ERROR: set AZDO_PAT or PAT in the environment")
    return pat


def get_clients(org_url: str, pat: str):
    conn = Connection(base_url=org_url, creds=BasicAuthentication("", pat))
    test_plan_client = conn.clients_v7_1.get_test_plan_client()
    wit_client = conn.clients_v7_1.get_work_item_tracking_client()
    return conn, test_plan_client, wit_client


PARENT_REL = "Microsoft.VSTS.Common.TestedBy-Reverse"


def fetch_parent_map(wit_client, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Return {test_case_id: {"parent_id": int|None, "parent_title": str|None}}."""
    out: dict[int, dict[str, Any]] = {}
    parent_ids: set[int] = set()
    # Step 1: pull each test case with relations to find parent ids.
    for i in range(0, len(ids), 200):
        batch = ids[i : i + 200]
        items = wit_client.get_work_items(ids=batch, expand="Relations")
        for wi in items:
            parent_id: int | None = None
            for rel in wi.relations or []:
                if rel.rel == PARENT_REL:
                    # rel.url ends with /_apis/wit/workItems/<id>
                    try:
                        parent_id = int(rel.url.rstrip("/").split("/")[-1])
                        break
                    except ValueError:
                        continue
            out[wi.id] = {"parent_id": parent_id, "parent_title": None}
            if parent_id is not None:
                parent_ids.add(parent_id)
    # Step 2: fetch titles for parents.
    parent_ids_list = sorted(parent_ids)
    titles: dict[int, str] = {}
    for i in range(0, len(parent_ids_list), 200):
        batch = parent_ids_list[i : i + 200]
        items = wit_client.get_work_items(ids=batch, fields=["System.Title", "System.WorkItemType"])
        for wi in items:
            titles[wi.id] = wi.fields.get("System.Title", "") or ""
    for info in out.values():
        pid = info["parent_id"]
        if pid is not None:
            info["parent_title"] = titles.get(pid)
    return out


def list_suites(client, project: str, plan_id: int) -> list[Any]:
    suites: list[Any] = []
    token: str | None = None
    while True:
        page = client.get_test_suites_for_plan(
            project=project,
            plan_id=plan_id,
            continuation_token=token,
        )
        suites.extend(page)
        token = getattr(page, "continuation_token", None)
        if not token:
            break
    return suites


def list_test_cases(client, project: str, plan_id: int, suite_id: int) -> list[Any]:
    cases: list[Any] = []
    token: str | None = None
    while True:
        page = client.get_test_case_list(
            project=project,
            plan_id=plan_id,
            suite_id=suite_id,
            continuation_token=token,
        )
        cases.extend(page)
        token = getattr(page, "continuation_token", None)
        if not token:
            break
    return cases


def field(work_item_fields: list[dict] | None, name: str) -> Any:
    if not work_item_fields:
        return None
    for entry in work_item_fields:
        if name in entry:
            return entry[name]
    return None


def normalize_case(tc: Any, suite_id: int, suite_name: str) -> dict[str, Any]:
    wi = getattr(tc, "work_item", None)
    wi_id = getattr(wi, "id", None) if wi else None
    wi_name = getattr(wi, "name", None) if wi else None
    fields = getattr(wi, "work_item_fields", None) if wi else None
    return {
        "id": wi_id,
        "title": wi_name,
        "state": field(fields, "System.State"),
        "priority": field(fields, "Microsoft.VSTS.Common.Priority"),
        "area_path": field(fields, "System.AreaPath"),
        "tags": field(fields, "System.Tags"),
        "assigned_to": field(fields, "System.AssignedTo"),
        "steps": field(fields, "Microsoft.VSTS.TCM.Steps"),
        "suite_ids": [suite_id],
        "suites": [{"id": suite_id, "name": suite_name}],
    }


def merge_case(existing: dict[str, Any], new: dict[str, Any]) -> None:
    for sid in new["suite_ids"]:
        if sid not in existing["suite_ids"]:
            existing["suite_ids"].append(sid)
    seen = {s["id"] for s in existing["suites"]}
    for s in new["suites"]:
        if s["id"] not in seen:
            existing["suites"].append(s)


STEPS_TAG_RE = re.compile(r"<[^>]+>")


def render_steps(raw: str | None) -> str:
    if not raw:
        return "_Steps not provided._"
    # Steps come as XML with <step><parameterizedString>... pairs.
    # Best-effort: pull text from each <step> in order, alternating action/expected.
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(raw)
    except Exception:
        return f"```\n{raw}\n```"

    lines: list[str] = []
    for idx, step in enumerate(root.findall(".//step"), start=1):
        pstrings = step.findall("parameterizedString")
        action = STEPS_TAG_RE.sub("", pstrings[0].text or "").strip() if len(pstrings) > 0 and pstrings[0].text else ""
        expected = STEPS_TAG_RE.sub("", pstrings[1].text or "").strip() if len(pstrings) > 1 and pstrings[1].text else ""
        lines.append(f"{idx}. **Action:** {action or '_n/a_'}")
        if expected:
            lines.append(f"   **Expected:** {expected}")
    return "\n".join(lines) if lines else "_No steps parsed._"


def yaml_escape(value: Any) -> str:
    if value is None:
        return "null"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def render_md(case: dict[str, Any], org_url: str, project: str) -> str:
    wi_url = f"{org_url}/{project}/_workitems/edit/{case['id']}"
    fm_lines = [
        "---",
        f"id: {case['id']}",
        f"title: {yaml_escape(case['title'])}",
        f"state: {yaml_escape(case['state'])}",
        f"priority: {yaml_escape(case['priority'])}",
        f"area_path: {yaml_escape(case['area_path'])}",
        f"tags: {yaml_escape(case['tags'])}",
        f"suite_ids: {json.dumps(case['suite_ids'])}",
        f"work_item_url: {yaml_escape(wi_url)}",
        "---",
        "",
        f"# {case['title'] or case['id']}",
        "",
        "## Metadata",
        "",
        f"- **ID:** {case['id']}",
        f"- **State:** {case['state']}",
        f"- **Priority:** {case['priority']}",
        f"- **Area Path:** {case['area_path']}",
        f"- **Tags:** {case['tags'] or '_none_'}",
        f"- **Assigned To:** {case['assigned_to'] or '_unassigned_'}",
        f"- **Work Item:** [{case['id']}]({wi_url})",
        "",
        "## Steps",
        "",
        render_steps(case.get("steps")),
        "",
        "## Suites",
        "",
    ]
    for s in case["suites"]:
        fm_lines.append(f"- {s['id']} — {s['name']}")
    fm_lines.append("")
    return "\n".join(fm_lines)


STORY_FIELDS = [
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.IterationPath",
    "System.Tags",
    "System.Description",
    "Microsoft.VSTS.Common.AcceptanceCriteria",
    "Microsoft.VSTS.Scheduling.StoryPoints",
    "System.CreatedDate",
    "System.ChangedDate",
]


def fetch_story_definitions(wit_client, story_ids: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    ids = sorted(set(story_ids))
    for i in range(0, len(ids), 200):
        batch = ids[i : i + 200]
        items = wit_client.get_work_items(ids=batch, fields=STORY_FIELDS)
        for wi in items:
            out[wi.id] = dict(wi.fields or {})
    return out


def render_story_md(
    story_id: int, fields: dict[str, Any], cases: list[dict[str, Any]], org_url: str, project: str
) -> str:
    title = fields.get("System.Title", "") or ""
    wi_url = f"{org_url}/{project}/_workitems/edit/{story_id}"
    assigned = fields.get("System.AssignedTo")
    if isinstance(assigned, dict):
        assigned = assigned.get("displayName")

    lines = [
        "---",
        f"id: {story_id}",
        f"type: {yaml_escape(fields.get('System.WorkItemType'))}",
        f"title: {yaml_escape(title)}",
        f"state: {yaml_escape(fields.get('System.State'))}",
        f"area_path: {yaml_escape(fields.get('System.AreaPath'))}",
        f"iteration_path: {yaml_escape(fields.get('System.IterationPath'))}",
        f"tags: {yaml_escape(fields.get('System.Tags'))}",
        f"story_points: {yaml_escape(fields.get('Microsoft.VSTS.Scheduling.StoryPoints'))}",
        f"work_item_url: {yaml_escape(wi_url)}",
        "---",
        "",
        f"# {story_id} — {title}",
        "",
        "## Metadata",
        "",
        f"- **Type:** {fields.get('System.WorkItemType')}",
        f"- **State:** {fields.get('System.State')}",
        f"- **Assigned To:** {assigned or '_unassigned_'}",
        f"- **Area Path:** {fields.get('System.AreaPath')}",
        f"- **Iteration Path:** {fields.get('System.IterationPath')}",
        f"- **Tags:** {fields.get('System.Tags') or '_none_'}",
        f"- **Story Points:** {fields.get('Microsoft.VSTS.Scheduling.StoryPoints') or '_n/a_'}",
        f"- **Created:** {fields.get('System.CreatedDate')}",
        f"- **Changed:** {fields.get('System.ChangedDate')}",
        f"- **Work Item:** [{story_id}]({wi_url})",
        "",
        "## Description",
        "",
        (fields.get("System.Description") or "_(no description)_").strip(),
        "",
        "## Acceptance Criteria",
        "",
        (fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "_(none)_").strip(),
        "",
        f"## Test Cases ({len(cases)})",
        "",
    ]
    for c in sorted(cases, key=lambda c: c["id"]):
        slug = slugify(c["title"] or "")
        lines.append(f"- [{c['id']} — {c['title']}](./{c['id']}-{slug}.md)")
    lines.append("")
    return "\n".join(lines)


def _suite_parent_id(suite: Any) -> int | None:
    parent = getattr(suite, "parent_suite", None)
    if parent is None:
        return None
    pid = getattr(parent, "id", None)
    if pid is None and isinstance(parent, dict):
        pid = parent.get("id")
    try:
        return int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return None


def _build_suite_tree(suites: list[Any]) -> tuple[dict[int, list[Any]], list[Any]]:
    """Return (children_by_parent_id, roots). Roots are suites with no parent in the set."""
    by_id = {s.id: s for s in suites}
    children: dict[int, list[Any]] = {}
    roots: list[Any] = []
    for s in suites:
        pid = _suite_parent_id(s)
        if pid is None or pid not in by_id:
            roots.append(s)
        else:
            children.setdefault(pid, []).append(s)
    for lst in children.values():
        lst.sort(key=lambda x: (getattr(x, "name", "") or "").lower())
    roots.sort(key=lambda x: (getattr(x, "name", "") or "").lower())
    return children, roots


def _print_suite_tree(
    suite: Any,
    children: dict[int, list[Any]],
    prefix: str,
    is_last: bool,
    is_root: bool,
) -> None:
    stype = getattr(suite, "suite_type", "") or ""
    marker = "[F]" if "static" in stype.lower() else "[S]"
    name = getattr(suite, "name", "") or ""
    if is_root:
        print(f"{marker} {suite.id}  {name}  ({stype})")
        new_prefix = ""
    else:
        connector = "└── " if is_last else "├── "
        print(f"{prefix}{connector}{marker} {suite.id}  {name}  ({stype})")
        new_prefix = prefix + ("    " if is_last else "│   ")
    kids = children.get(suite.id, [])
    for i, kid in enumerate(kids):
        _print_suite_tree(kid, children, new_prefix, i == len(kids) - 1, False)


def cmd_show_plan(args: argparse.Namespace) -> int:
    pat = get_pat()
    _, client, _ = get_clients(args.org, pat)

    print(f"Fetching suites for plan {args.plan_id} in project {args.project}...")
    suites = list_suites(client, args.project, args.plan_id)
    print(f"  {len(suites)} suites found\n")

    children, roots = _build_suite_tree(suites)

    if args.folder_id:
        by_id = {s.id: s for s in suites}
        start = by_id.get(args.folder_id)
        if start is None:
            print(f"ERROR: folder/suite id {args.folder_id} not found in plan {args.plan_id}", file=sys.stderr)
            return 1
        _print_suite_tree(start, children, "", True, True)
    else:
        for i, root in enumerate(roots):
            _print_suite_tree(root, children, "", i == len(roots) - 1, True)

    print()
    print("Legend: [F] folder/static suite · [S] other suite type")
    return 0


def cmd_show_story(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.json).read_text())
    matches = [c for c in data if c.get("parent_id") == args.story_id]
    if not matches:
        print(f"No test cases found for story {args.story_id} in {args.json}")
        return 1
    parent_title = matches[0].get("parent_title") or ""
    print(f"Story {args.story_id}: {parent_title}")
    print(f"{len(matches)} test case(s):")
    for c in sorted(matches, key=lambda c: c["id"]):
        print(f"  {c['id']:>6}  [{c.get('state') or '-':<10}] {c['title']}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:

    out_json = Path(args.out_json or f"test_cases_plan_{args.plan_id}.json")
    md_dir = Path(args.md_dir or f"tests/plan-{args.plan_id}")

    pat = get_pat()
    _, client, wit_client = get_clients(args.org, pat)

    print(f"Fetching suites for plan {args.plan_id} in project {args.project}...")
    suites = list_suites(client, args.project, args.plan_id)
    print(f"  {len(suites)} suites found")

    cases_by_id: dict[int, dict[str, Any]] = {}
    for suite in suites:
        sid = suite.id
        sname = getattr(suite, "name", "") or ""
        try:
            tcs = list_test_cases(client, args.project, args.plan_id, sid)
        except Exception as e:
            print(f"  suite {sid} ({sname}): error {e}", file=sys.stderr)
            continue
        for tc in tcs:
            norm = normalize_case(tc, sid, sname)
            if norm["id"] is None:
                continue
            if norm["id"] in cases_by_id:
                merge_case(cases_by_id[norm["id"]], norm)
            else:
                cases_by_id[norm["id"]] = norm
        print(f"  suite {sid} {sname!r}: {len(tcs)} cases")

    cases = sorted(cases_by_id.values(), key=lambda c: c["id"])
    print(f"Total unique test cases: {len(cases)}")

    if args.group_by_story:
        print("Resolving parent stories via TestedBy relation...")
        parent_map = fetch_parent_map(wit_client, [c["id"] for c in cases])
        for c in cases:
            info = parent_map.get(c["id"], {})
            c["parent_id"] = info.get("parent_id")
            c["parent_title"] = info.get("parent_title")

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(cases, indent=2, ensure_ascii=False))
    print(f"Wrote {out_json}")

    md_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        slug = slugify(case["title"] or "")
        if args.group_by_story:
            pid = case.get("parent_id")
            sub = f"story-{pid}" if pid else "story-none"
            target_dir = md_dir / sub
        else:
            target_dir = md_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{case['id']}-{slug}.md"
        path.write_text(render_md(case, args.org, args.project))
    print(f"Wrote {len(cases)} markdown files under {md_dir}/")

    if args.group_by_story:
        write_story_index(md_dir, cases, args.org, args.project, args.plan_id)
        print(f"Wrote {md_dir}/story-index.md")

    if args.group_by_story and args.include_story:
        story_ids = [c["parent_id"] for c in cases if c.get("parent_id")]
        if story_ids:
            print(f"Fetching {len(set(story_ids))} story definitions...")
            story_fields = fetch_story_definitions(wit_client, story_ids)
            cases_by_story: dict[int, list[dict[str, Any]]] = {}
            for c in cases:
                pid = c.get("parent_id")
                if pid:
                    cases_by_story.setdefault(pid, []).append(c)
            written = 0
            for pid, fields in story_fields.items():
                folder = md_dir / f"story-{pid}"
                folder.mkdir(parents=True, exist_ok=True)
                slug = slugify(fields.get("System.Title", "") or "")
                path = folder / f"story-{pid}-{slug}.md"
                path.write_text(
                    render_story_md(pid, fields, cases_by_story.get(pid, []), args.org, args.project)
                )
                written += 1
            print(f"Wrote {written} story definition files")

    return 0


def write_story_index(
    md_dir: Path, cases: list[dict[str, Any]], org_url: str, project: str, plan_id: int
) -> None:
    groups: dict[Any, list[dict[str, Any]]] = {}
    titles: dict[Any, str | None] = {}
    for c in cases:
        pid = c.get("parent_id")
        groups.setdefault(pid, []).append(c)
        titles.setdefault(pid, c.get("parent_title"))

    def sort_key(pid):
        return (pid is None, pid or 0)

    lines = [
        f"# Story Index — Plan {plan_id}",
        "",
        f"Total stories: **{len(groups)}** · Total test cases: **{len(cases)}**",
        "",
        "| Story | Title | Cases | Folder |",
        "| ---: | --- | ---: | --- |",
    ]
    for pid in sorted(groups.keys(), key=sort_key):
        items = groups[pid]
        if pid is None:
            label = "—"
            title = "_no TestedBy parent_"
            folder = "story-none/"
        else:
            url = f"{org_url}/{project}/_workitems/edit/{pid}"
            label = f"[{pid}]({url})"
            title = (titles.get(pid) or "").replace("|", "\\|") or "_(no title)_"
            folder = f"story-{pid}/"
        lines.append(f"| {label} | {title} | {len(items)} | [`{folder}`]({folder}) |")
    lines.append("")
    (md_dir / "story-index.md").write_text("\n".join(lines))


CHILD_REL = "System.LinkTypes.Hierarchy-Forward"


def collect_descendant_pr_refs(
    wit_client, root_id: int, max_depth: int = 4
) -> tuple[list[tuple[str, str, int]], list[int]]:
    """Walk the story and its descendant work items, collecting PR ArtifactLinks.

    Returns (pr_refs, visited_ids). Most PRs are linked to child Tasks/Bugs
    rather than the User Story itself, so we have to traverse the hierarchy.
    """
    visited: set[int] = set()
    pr_refs: list[tuple[str, str, int]] = []
    seen_refs: set[tuple[str, str, int]] = set()
    frontier = [root_id]
    depth = 0
    while frontier and depth <= max_depth:
        batch = [i for i in frontier if i not in visited]
        for i in batch:
            visited.add(i)
        frontier = []
        if not batch:
            break
        # ADO caps get_work_items at 200 ids per call.
        for i in range(0, len(batch), 200):
            chunk = batch[i : i + 200]
            try:
                items = wit_client.get_work_items(ids=chunk, expand="Relations")
            except Exception:
                continue
            for wi in items:
                for rel in wi.relations or []:
                    if rel.rel == CHILD_REL:
                        try:
                            cid = int((rel.url or "").rstrip("/").split("/")[-1])
                            if cid not in visited:
                                frontier.append(cid)
                        except ValueError:
                            pass
                    elif rel.rel == "ArtifactLink":
                        parsed = parse_pr_artifact_url(rel.url or "")
                        if parsed and parsed not in seen_refs:
                            seen_refs.add(parsed)
                            pr_refs.append(parsed)
        depth += 1
    return pr_refs, sorted(visited)


def parse_pr_artifact_url(url: str) -> tuple[str, str, int] | None:
    # vstfs:///Git/PullRequestId/{projectId}%2F{repoId}%2F{prId}
    if not url or "PullRequestId" not in url:
        return None
    tail = url.split("PullRequestId/", 1)[1]
    decoded = urllib.parse.unquote(tail)
    parts = decoded.split("/")
    if len(parts) < 3:
        return None
    try:
        return parts[0], parts[1], int(parts[2])
    except ValueError:
        return None


def _is_ancestor(git_client, project: str, repo_id: str, ancestor_sha: str, descendant_sha: str) -> bool:
    """True if ancestor_sha is reachable from descendant_sha via merge-base."""
    if not ancestor_sha or not descendant_sha:
        return False
    if ancestor_sha[:8] == descendant_sha[:8]:
        return True
    try:
        bases = git_client.get_merge_bases(
            repository_name_or_id=repo_id,
            commit_id=descendant_sha,
            other_commit_id=ancestor_sha,
            project=project,
        )
    except Exception:
        return False
    for b in bases or []:
        bid = getattr(b, "commit_id", None) or (b.get("commitId") if isinstance(b, dict) else None)
        if bid and bid[:8] == ancestor_sha[:8]:
            return True
    return False


def _artifact_source_commit(artifact: Any) -> str | None:
    dr = getattr(artifact, "definition_reference", None)
    if not dr or not isinstance(dr, dict):
        return None
    src = dr.get("sourceVersion")
    if src is None:
        return None
    cid = getattr(src, "id", None)
    if cid is None and isinstance(src, dict):
        cid = src.get("id")
    return cid or None


def main() -> int:
    p = argparse.ArgumentParser(prog="fetch_test_plan")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Fetch a test plan and write JSON + per-case markdown.")
    f.add_argument("--plan-id", type=int, required=True)
    f.add_argument("--project", default=DEFAULT_PROJECT)
    f.add_argument("--org", default=DEFAULT_ORG)
    f.add_argument("--out-json", default=None)
    f.add_argument("--md-dir", default=None)
    f.add_argument(
        "--group-by-story",
        action="store_true",
        help="Place markdown files in story-<parentId>/ subdirs based on TestedBy relation.",
    )
    f.add_argument(
        "--include-story",
        action="store_true",
        help="Also write story-<id>-<title>.md (story definition) inside each story folder. Requires --group-by-story.",
    )
    f.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("show-plan", help="Show plan suite hierarchy (folders and test suites).")
    sp.add_argument("--plan-id", type=int, required=True)
    sp.add_argument("--project", default=DEFAULT_PROJECT)
    sp.add_argument("--org", default=DEFAULT_ORG)
    sp.add_argument(
        "--folder-id",
        type=int,
        default=None,
        help="Show only the subtree rooted at this folder/suite id.",
    )
    sp.set_defaults(func=cmd_show_plan)

    s = sub.add_parser("show-story", help="List test cases for one user story from a JSON dump.")
    s.add_argument("story_id", type=int)
    s.add_argument("--json", default="test_cases_plan_1001.json")
    s.set_defaults(func=cmd_show_story)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
