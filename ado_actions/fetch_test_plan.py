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
from pathlib import Path
from typing import Any

from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication


DEFAULT_ORG = "https://dev.azure.com/example-org"
DEFAULT_PROJECT = "example-project"


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

    s = sub.add_parser("show-story", help="List test cases for one user story from a JSON dump.")
    s.add_argument("story_id", type=int)
    s.add_argument("--json", default="test_cases_plan_1001.json")
    s.set_defaults(func=cmd_show_story)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
