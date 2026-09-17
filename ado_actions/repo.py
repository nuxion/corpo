"""Repository helpers: PR review with worktree + AI agent."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import click
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication

from ado_actions.cliargs import Args
from ado_actions.fetch_test_plan import DEFAULT_ORG, DEFAULT_PROJECT, get_pat


_PR_URL_RE = re.compile(
    r"https?://(?:dev\.azure\.com/(?P<org>[^/]+)/(?P<proj>[^/]+)|"
    r"(?P<orgsub>[^./]+)\.visualstudio\.com/(?P<proj2>[^/]+))"
    r"/_git/(?P<repo>[^/]+)/pullrequest/(?P<pr>\d+)",
    re.IGNORECASE,
)


def parse_pr_ref(url: str) -> tuple[str, str, str, int]:
    """Return (org_url, project, repo, pr_id) parsed from an ADO PR URL."""
    m = _PR_URL_RE.search(url.strip())
    if not m:
        raise ValueError(f"cannot parse PR url: {url!r}")
    pr_id = int(m.group("pr"))
    repo = urllib.parse.unquote(m.group("repo"))
    if m.group("org"):
        org_url = f"https://dev.azure.com/{m.group('org')}"
        project = urllib.parse.unquote(m.group("proj"))
    else:
        org_url = f"https://{m.group('orgsub')}.visualstudio.com"
        project = urllib.parse.unquote(m.group("proj2"))
    return org_url, project, repo, pr_id


def _branch(ref: str | None) -> str:
    return (ref or "").replace("refs/heads/", "")


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def ensure_worktree(repo_path: Path, branch: str, dest: Path) -> None:
    """Create a git worktree at `dest` checked out to `branch`."""
    if dest.exists():
        print(f"Worktree already exists at {dest}, skipping creation.", file=sys.stderr)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Fetching latest from origin in {repo_path}...")
    _run(["git", "fetch", "origin", branch], cwd=repo_path, check=False)
    print(f"Creating worktree {dest} → {branch}")
    try:
        _run(["git", "worktree", "add", str(dest), branch], cwd=repo_path)
    except subprocess.CalledProcessError:
        _run(["git", "worktree", "add", str(dest), f"origin/{branch}"], cwd=repo_path)


def fetch_work_items(ids: list[int], out_dir: Path, org: str, project: str) -> list[Path]:
    """Invoke `ado board fetch-work` for each id; return written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for wid in ids:
        print(f"Fetching work item {wid}...")
        try:
            r = _run(
                [
                    "ado", "board", "fetch-work", str(wid),
                    "--org", org, "--project", project,
                    "--out-dir", str(out_dir),
                ]
            )
        except subprocess.CalledProcessError as e:
            print(f"  warn: ado board fetch-work {wid} failed: {e.stderr}", file=sys.stderr)
            continue
        m = re.search(r"Wrote (.+)$", r.stdout.strip())
        if m:
            written.append(Path(m.group(1).strip()))
    return written


def build_prompt(pr: Any, work_item_files: list[Path], worktree: Path) -> str:
    title = getattr(pr, "title", "") or ""
    desc = getattr(pr, "description", "") or ""
    src = _branch(getattr(pr, "source_ref_name", ""))
    tgt = _branch(getattr(pr, "target_ref_name", ""))
    parts: list[str] = []
    parts.append("Review the following PR with the associated workitems.")
    parts.append("")
    parts.append(f"PR #{pr.pull_request_id}: {title}")
    parts.append(f"Source: {src} → Target: {tgt}")
    parts.append(f"Worktree (PR checkout): {worktree}")
    if desc:
        parts.append("")
        parts.append("PR Description:")
        parts.append(desc)
    if work_item_files:
        parts.append("")
        parts.append("Associated work items (markdown files):")
        for p in work_item_files:
            parts.append(f"- {p}")
    parts.append("")
    parts.append(
        "Please inspect the diff in the worktree (`git diff <target-branch>...HEAD`), "
        "verify it satisfies the work items' acceptance criteria, and report findings: "
        "correctness, security, tests, edge cases, alignment with requirements."
    )
    return "\n".join(parts)


def run_agent(agent: str, prompt: str, cwd: Path) -> str:
    """Run claude or copilot non-interactively, streaming stdout live; return collected output."""
    if agent == "claude":
        if not shutil.which("claude"):
            raise RuntimeError("`claude` CLI not found in PATH.")
        cmd = ["claude", "-p", prompt]
    elif agent == "copilot":
        if not shutil.which("copilot"):
            raise RuntimeError("`copilot` CLI not found in PATH.")
        cmd = ["copilot", "-p", prompt]
    else:
        raise ValueError(f"unknown agent: {agent}")
    print(f"Running {agent} (non-interactive) in {cwd}...", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
    )
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    rc = proc.wait()
    err = proc.stderr.read() if proc.stderr else ""
    if rc != 0:
        if err:
            sys.stderr.write(err)
        raise RuntimeError(f"{agent} exited with code {rc}")
    return "".join(chunks)


def cmd_review(args: Args) -> int:
    if args.url:
        try:
            org_url, project, repo, pr_id = parse_pr_ref(args.url)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    else:
        if not (args.repo and args.pr):
            print("ERROR: provide a PR URL or both --repo and --pr.", file=sys.stderr)
            return 1
        org_url = args.org or DEFAULT_ORG
        project = args.project or DEFAULT_PROJECT
        repo = args.repo
        pr_id = int(args.pr)
    org_url = args.org or org_url
    project = args.project or project

    pat = get_pat()
    conn = Connection(base_url=org_url, creds=BasicAuthentication("", pat))
    git_client = conn.clients_v7_1.get_git_client()

    print(f"Fetching PR {pr_id} in {project}/{repo}...")
    try:
        pr = git_client.get_pull_request(
            repository_id=repo, pull_request_id=pr_id, project=project
        )
    except Exception as e:
        print(f"ERROR: cannot fetch PR {pr_id}: {e}", file=sys.stderr)
        return 1

    try:
        wi_refs = git_client.get_pull_request_work_item_refs(
            repository_id=repo, pull_request_id=pr_id, project=project
        )
    except Exception as e:
        print(f"  warn: cannot fetch PR work items: {e}", file=sys.stderr)
        wi_refs = []
    wi_ids = [int(getattr(r, "id", 0)) for r in (wi_refs or []) if getattr(r, "id", None)]
    print(f"Linked work items: {wi_ids or '(none)'}")

    repo_path = Path(args.repo_path).resolve() if args.repo_path else Path.cwd()
    if not (repo_path / ".git").exists():
        print(f"ERROR: {repo_path} is not a git repository. Pass --repo-path.", file=sys.stderr)
        return 1

    worktree_dir = Path(args.worktree_dir).resolve()
    dest = worktree_dir / f"{repo}-pr-{pr_id}"
    src_branch = _branch(pr.source_ref_name)
    if not src_branch:
        print("ERROR: could not determine PR source branch.", file=sys.stderr)
        return 1
    try:
        ensure_worktree(repo_path, src_branch, dest)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git worktree failed: {e.stderr}", file=sys.stderr)
        return 1

    wi_dir = dest / ".workitems"
    written = fetch_work_items(wi_ids, wi_dir, org_url, project) if wi_ids else []

    prompt = build_prompt(pr, written, dest)

    try:
        output = run_agent(args.agent, prompt, dest)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not output.endswith("\n"):
        sys.stdout.write("\n")
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"\nWrote review to {out_path}", file=sys.stderr)
    return 0


@click.group("repo")
def repo_cli() -> None:
    """Repository and pull request helpers."""


@repo_cli.command("review")
@click.argument("url", required=False, default=None)
@click.option("--repo", default=None, help="Repository name (when url not supplied).")
@click.option("--pr", default=None, help="Pull request ID (when url not supplied).")
@click.option("--org", default=None, help=f"Org URL (default: {DEFAULT_ORG}).")
@click.option("--project", default=None, help=f"Project (default: {DEFAULT_PROJECT}).")
@click.option(
    "--repo-path",
    default=None,
    help="Local path to the cloned repo to create the worktree from (default: cwd).",
)
@click.option(
    "--worktree-dir",
    default=".worktrees",
    show_default=True,
    help="Directory under which the PR worktree is created.",
)
@click.option(
    "--claude",
    "agent",
    flag_value="claude",
    default=True,
    help="Use the `claude` CLI (default).",
)
@click.option("--copilot", "agent", flag_value="copilot", help="Use the `copilot` CLI.")
@click.option("--out", default=None, help="Optional file to write the review output.")
def cli_review(**kwargs: Any) -> None:
    """Review an ADO pull request via claude/copilot.

    URL is the full PR URL, e.g.
    https://dev.azure.com/<org>/<proj>/_git/<repo>/pullrequest/<id>.
    """
    raise SystemExit(cmd_review(Args(**kwargs)))
