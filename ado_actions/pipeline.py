"""Pipeline operations: queue builds, create releases, deploy environments."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any

from azure.devops.connection import Connection
from azure.devops.v7_1.build.models import AgentPoolQueue, Build, DefinitionReference
from azure.devops.v7_1.release.models import (
    ArtifactMetadata,
    BuildVersion,
    ConfigurationVariableValue,
    ReleaseEnvironmentUpdateMetadata,
    ReleaseStartMetadata,
)
from msrest.authentication import BasicAuthentication

from ado_actions.fetch_test_plan import DEFAULT_ORG, DEFAULT_PROJECT, get_pat


_ORG_PROJECT_RE = re.compile(
    r"https?://(?:dev\.azure\.com/(?P<org>[^/]+)|(?P<orgsub>[^./]+)\.visualstudio\.com)"
    r"(?:/(?P<proj>[^/?#]+))?",
    re.IGNORECASE,
)

_COLLECTION_SEGMENTS = {"_git", "_build", "_release", "_apis", "_workitems", "_dashboards"}

TERMINAL_BUILD_STATES = {"completed", "cancelling"}
TERMINAL_ENV_STATUSES = {"succeeded", "partiallySucceeded", "rejected", "canceled"}
POLL_SECONDS = 5


def parse_org_project_url(url: str) -> tuple[str, str | None]:
    """Return (org_url, project) parsed from an ADO URL. Project may be None."""
    m = _ORG_PROJECT_RE.match(url.strip())
    if not m:
        raise ValueError(f"cannot parse organization from url: {url!r}")
    if m.group("org"):
        org_url = f"https://dev.azure.com/{m.group('org')}"
    else:
        org_url = f"https://{m.group('orgsub')}.visualstudio.com"
    raw_project = m.group("proj")
    if raw_project and raw_project not in _COLLECTION_SEGMENTS:
        project = urllib.parse.unquote(raw_project)
    else:
        project = None
    return org_url, project


def resolve_context(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve (org_url, project) from --org/--project, --url, env, then defaults."""
    url_org: str | None = None
    url_project: str | None = None
    if getattr(args, "url", None):
        url_org, url_project = parse_org_project_url(args.url)

    org_url = (
        args.org
        or url_org
        or os.environ.get("AZDO_ORG_URL")
        or DEFAULT_ORG
    )
    project = (
        args.project
        or url_project
        or os.environ.get("AZDO_PROJECT")
        or DEFAULT_PROJECT
    )
    return org_url.rstrip("/"), project


def get_connection(org_url: str) -> Connection:
    return Connection(base_url=org_url, creds=BasicAuthentication("", get_pat()))


def normalize_branch(branch: str) -> str:
    """Accept `dev`, `refs/heads/dev`, `release/1.2` or a tag ref; return a full ref."""
    b = branch.strip()
    if b.startswith("refs/"):
        return b
    return f"refs/heads/{b}"


def parse_variables(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"invalid --var {pair!r}, expected KEY=VALUE")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def resolve_definition(build_client, project: str, name: str) -> Any:
    """Find a build definition by exact name; fail with candidates when ambiguous."""
    defs = build_client.get_definitions(project=project, name=name) or []
    exact = [d for d in defs if (d.name or "").lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    if not defs:
        raise LookupError(f"no build pipeline matching {name!r} in project {project!r}")
    if not exact:
        names = ", ".join(sorted({d.name for d in defs}))
        raise LookupError(f"no exact match for {name!r}; did you mean: {names}")
    paths = ", ".join(f"{d.path}/{d.name} (id={d.id})" for d in exact)
    raise LookupError(f"{name!r} is ambiguous, pass --definition-id. Candidates: {paths}")


def definition_variables(build_client, project: str, definition_id: int) -> dict[str, Any]:
    """Variables declared on a build definition, keyed by name."""
    full = build_client.get_definition(project=project, definition_id=definition_id)
    return full.variables or {}


def check_variables(declared: dict[str, Any], requested: dict[str, str]) -> None:
    """Fail on overrides ADO would silently drop at queue time."""
    lookup = {k.lower(): (k, v) for k, v in declared.items()}
    settable = sorted(k for k, v in declared.items() if getattr(v, "allow_override", False))
    for name in requested:
        found = lookup.get(name.lower())
        if not found:
            hint = ", ".join(settable) or "none"
            raise LookupError(f"variable {name!r} is not defined on this pipeline; settable: {hint}")
        real, var = found
        if not getattr(var, "allow_override", False):
            raise LookupError(
                f"variable {real!r} is not settable at queue time "
                "(enable 'Let users override this value when running this pipeline')"
            )


def resolve_queue(task_agent_client, project: str, pool_name: str) -> Any:
    """Find an agent queue by name so the build can be retargeted at queue time."""
    queues = task_agent_client.get_agent_queues(project=project, queue_name=pool_name) or []
    exact = [q for q in queues if (q.name or "").lower() == pool_name.lower()]
    if len(exact) == 1:
        return exact[0]
    if not queues:
        raise LookupError(f"no agent pool/queue matching {pool_name!r} in project {project!r}")
    names = ", ".join(sorted({q.name for q in queues}))
    raise LookupError(f"no exact agent pool match for {pool_name!r}; available: {names}")


def build_web_url(build: Any, org_url: str, project: str) -> str:
    links = getattr(build, "_links", None) or {}
    web = links.get("web") if isinstance(links, dict) else None
    if isinstance(web, dict) and web.get("href"):
        return web["href"]
    return f"{org_url}/{urllib.parse.quote(project)}/_build/results?buildId={build.id}&view=results"


def _fmt_result(state: str | None, result: str | None) -> str:
    return result or state or "unknown"


def watch_build(
    build_client, project: str, build_id: int, poll: int = POLL_SECONDS, out=None
) -> Any:
    """Poll a build until it completes, printing timeline records as they finish."""
    out = out or sys.stdout
    seen: dict[str, str] = {}
    print(f"Watching build {build_id} (polling every {poll}s, Ctrl-C to stop watching)...",
          file=out)
    while True:
        build = build_client.get_build(project=project, build_id=build_id)
        try:
            timeline = build_client.get_build_timeline(project=project, build_id=build_id)
        except Exception:
            timeline = None
        for record in getattr(timeline, "records", None) or []:
            if record.type not in ("Stage", "Job", "Task"):
                continue
            key = str(record.id)
            status = f"{record.state}/{record.result}"
            if seen.get(key) == status:
                continue
            seen[key] = status
            indent = {"Stage": "", "Job": "  ", "Task": "    "}[record.type]
            if record.state == "completed":
                print(f"{indent}[{record.result}] {record.name}", file=out)
            elif record.state == "inProgress":
                print(f"{indent}[running] {record.name}", file=out)
        if build.status == "completed":
            print(f"\nBuild {build_id} finished: {_fmt_result(build.status, build.result)}",
                  file=out)
            return build
        time.sleep(poll)


def await_build_number(build_client, project: str, build_id: int, timeout: int = 60) -> Any:
    """Re-fetch a queued build until ADO stamps its build number (or timeout)."""
    build = build_client.get_build(project=project, build_id=build_id)
    waited = 0
    while not build.build_number and waited < timeout:
        time.sleep(2)
        waited += 2
        build = build_client.get_build(project=project, build_id=build_id)
    return build


def cmd_build(args: argparse.Namespace) -> int:
    org_url, project = resolve_context(args)
    if not args.branch and not args.list_vars:
        print("ERROR: --branch is required", file=sys.stderr)
        return 1
    try:
        variables = parse_variables(args.var)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    conn = get_connection(org_url)
    build_client = conn.clients_v7_1.get_build_client()

    try:
        definition = resolve_definition(build_client, project, args.pipeline)
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    queue = None
    if args.pool:
        task_agent_client = conn.clients_v7_1.get_task_agent_client()
        try:
            agent_queue = resolve_queue(task_agent_client, project, args.pool)
        except LookupError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        queue = AgentPoolQueue(id=agent_queue.id, name=agent_queue.name)

    if args.list_vars:
        declared = definition_variables(build_client, project, definition.id)
        for name in sorted(declared):
            var = declared[name]
            flag = "settable" if getattr(var, "allow_override", False) else "fixed"
            value = "***" if getattr(var, "is_secret", False) else (var.value or "")
            print(f"{name} = {value}  [{flag}]")
        return 0

    if variables:
        try:
            check_variables(definition_variables(build_client, project, definition.id), variables)
        except LookupError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    source_branch = normalize_branch(args.branch)
    request = Build(
        definition=DefinitionReference(id=definition.id),
        source_branch=source_branch,
        queue=queue,
        parameters=json.dumps(variables) if variables else None,
    )

    progress = sys.stderr if args.json else sys.stdout
    print(f"Queuing {definition.name} (id={definition.id}) on {source_branch}...", file=progress)
    if queue:
        print(f"  agent pool: {queue.name} (queue id={queue.id})", file=progress)
    for name, value in variables.items():
        print(f"  variable: {name}={value}", file=progress)
    try:
        queued = build_client.queue_build(build=request, project=project)
    except Exception as e:
        print(f"ERROR: cannot queue build: {e}", file=sys.stderr)
        return 1

    url = build_web_url(queued, org_url, project)
    if not args.json:
        print(f"Queued build {queued.id} ({queued.build_number or 'pending number'})")
        print(f"  {url}")

    finished = None
    if args.watch:
        try:
            finished = watch_build(build_client, project, queued.id, args.poll, out=progress)
        except KeyboardInterrupt:
            print("\nStopped watching; build continues running.", file=sys.stderr)
            return 0
        except Exception as e:
            print(f"ERROR: while watching build: {e}", file=sys.stderr)
            return 1
    elif args.json:
        # The build number is usually stamped a moment after queueing, and it is
        # the value callers feed to `pipeline release --version`.
        finished = await_build_number(build_client, project, queued.id)

    latest = finished or queued
    if args.json:
        print(json.dumps({
            "build_id": latest.id,
            "version": latest.build_number,
            "branch": _short_branch(latest.source_branch) or _short_branch(source_branch),
            "definition": definition.name,
            "status": latest.status,
            "result": latest.result,
            "url": url,
        }, indent=2))

    if not args.watch:
        return 0
    return 0 if latest.result == "succeeded" else 1


def _ref(definition_reference: Any, key: str) -> tuple[str | None, str | None]:
    """Read (id, name) out of an artifact's definitionReference[key]."""
    if not isinstance(definition_reference, dict):
        return None, None
    item = definition_reference.get(key)
    if item is None:
        return None, None
    if isinstance(item, dict):
        return item.get("id"), item.get("name")
    return getattr(item, "id", None), getattr(item, "name", None)


def resolve_release_definition(release_client, project: str, name: str) -> Any:
    """Find a release definition by exact name; fail with candidates when ambiguous."""
    defs = release_client.get_release_definitions(
        project=project, search_text=name, is_exact_name_match=True, expand="artifacts"
    ) or []
    exact = [d for d in defs if (d.name or "").lower() == name.lower()]
    if len(exact) == 1:
        return release_client.get_release_definition(project=project, definition_id=exact[0].id)
    if not exact:
        loose = release_client.get_release_definitions(project=project, search_text=name) or []
        if loose:
            names = ", ".join(sorted({d.name for d in loose}))
            raise LookupError(f"no exact match for {name!r}; did you mean: {names}")
        raise LookupError(f"no release pipeline matching {name!r} in project {project!r}")
    paths = ", ".join(f"{d.path}/{d.name} (id={d.id})" for d in exact)
    raise LookupError(f"{name!r} is ambiguous. Candidates: {paths}")


def last_release(release_client, project: str, definition_id: int) -> Any | None:
    """Most recent release for a definition, artifacts expanded."""
    releases = release_client.get_releases(
        project=project, definition_id=definition_id, top=1, expand="artifacts"
    ) or []
    return releases[0] if releases else None


def release_artifact_version(release: Any, alias: str) -> tuple[str | None, str | None, str | None]:
    """Return (build_id, version_name, source_branch) for `alias` in an existing release."""
    for artifact in getattr(release, "artifacts", None) or []:
        if (artifact.alias or "") != alias:
            continue
        dr = getattr(artifact, "definition_reference", None)
        version_id, version_name = _ref(dr, "version")
        branch_ref, branch_name = _ref(dr, "branch")
        return version_id, version_name, branch_ref or branch_name
    return None, None, None


def latest_build(build_client, project: str, definition_id: int, branch: str | None) -> Any | None:
    """Latest successful build of a build definition, optionally pinned to a branch."""
    builds = build_client.get_builds(
        project=project,
        definitions=[definition_id],
        branch_name=normalize_branch(branch) if branch else None,
        status_filter="completed",
        result_filter="succeeded",
        query_order="finishTimeDescending",
        top=1,
    ) or []
    return builds[0] if builds else None


def find_build_by_number(build_client, project: str, definition_id: int, version: str) -> Any | None:
    builds = build_client.get_builds(
        project=project, definitions=[definition_id], build_number=version, top=1
    ) or []
    return builds[0] if builds else None


def resolve_artifacts(
    build_client,
    release_client,
    project: str,
    definition: Any,
    version: str | None,
    branch: str | None,
    out=None,
) -> tuple[list[ArtifactMetadata], list[dict[str, Any]]]:
    """Pin every Build artifact of the release definition to a concrete version.

    With --version, look that build number up. Without it, take the newest
    successful build (honouring --branch); if there is none, fall back to
    whatever the previous release used.
    """
    out = out or sys.stdout
    previous = last_release(release_client, project, definition.id)
    if previous:
        print(f"Last release: {previous.name} (id={previous.id})", file=out)
    else:
        print("Last release: (none yet)", file=out)

    metadata: list[ArtifactMetadata] = []
    summary: list[dict[str, Any]] = []
    for artifact in getattr(definition, "artifacts", None) or []:
        alias = artifact.alias or ""
        if (artifact.type or "").lower() != "build":
            print(f"  warn: skipping non-build artifact {alias!r} (type={artifact.type})",
                  file=sys.stderr)
            continue
        src_def_id, src_def_name = _ref(artifact.definition_reference, "definition")
        _, src_project = _ref(artifact.definition_reference, "project")
        src_project = src_project or project
        if not src_def_id:
            raise LookupError(f"artifact {alias!r} has no linked build definition")

        build = None
        if version:
            build = find_build_by_number(build_client, src_project, int(src_def_id), version)
            if not build:
                raise LookupError(
                    f"no build numbered {version!r} for artifact {alias!r} "
                    f"(pipeline {src_def_name})"
                )
        else:
            build = latest_build(build_client, src_project, int(src_def_id), branch)

        if build:
            build_id = str(build.id)
            build_version = build.build_number
            build_branch = build.source_branch
        else:
            prev_id, prev_version, prev_branch = release_artifact_version(previous, alias)
            if not prev_id:
                where = f" on {branch}" if branch else ""
                raise LookupError(
                    f"no successful build{where} for artifact {alias!r} "
                    f"(pipeline {src_def_name}) and no previous release to fall back to"
                )
            print(f"  warn: no new build for {alias!r}, reusing previous release version",
                  file=sys.stderr)
            build_id, build_version, build_branch = prev_id, prev_version, prev_branch

        metadata.append(
            ArtifactMetadata(
                alias=alias,
                instance_reference=BuildVersion(
                    id=build_id, name=build_version, source_branch=build_branch
                ),
            )
        )
        summary.append({
            "alias": alias,
            "build_id": build_id,
            "version": build_version,
            "branch": _short_branch(build_branch),
        })
    if not metadata:
        raise LookupError("release definition has no build artifacts to pin")
    return metadata, summary


def _short_branch(ref: str | None) -> str | None:
    return (ref or "").replace("refs/heads/", "") or None


def release_web_url(release: Any, org_url: str, project: str) -> str:
    links = getattr(release, "_links", None) or {}
    web = links.get("web") if isinstance(links, dict) else None
    if isinstance(web, dict) and web.get("href"):
        return web["href"]
    return f"{org_url}/{urllib.parse.quote(project)}/_release?releaseId={release.id}&_a=release-summary"


def cmd_release(args: argparse.Namespace) -> int:
    org_url, project = resolve_context(args)
    try:
        variables = parse_variables(args.var)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    progress = sys.stderr if args.json else sys.stdout
    conn = get_connection(org_url)
    release_client = conn.clients_v7_1.get_release_client()
    build_client = conn.clients_v7_1.get_build_client()

    try:
        definition = resolve_release_definition(release_client, project, args.pipeline)
        artifacts, summary = resolve_artifacts(
            build_client, release_client, project, definition, args.version, args.branch,
            out=progress,
        )
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: cannot resolve release artifacts: {e}", file=sys.stderr)
        return 1

    for item in summary:
        print(f"  artifact {item['alias']}: {item['version']} @ {item['branch'] or '?'}",
              file=progress)

    manual_environments = None
    if args.manual:
        manual_environments = [
            env.name for env in (getattr(definition, "environments", None) or []) if env.name
        ]

    metadata = ReleaseStartMetadata(
        definition_id=definition.id,
        artifacts=artifacts,
        description=args.description,
        is_draft=args.draft,
        manual_environments=manual_environments,
        reason="manual",
        variables={
            k: ConfigurationVariableValue(value=v) for k, v in variables.items()
        } or None,
    )

    print(f"Creating release for {definition.name} (id={definition.id})...", file=progress)
    try:
        release = release_client.create_release(release_start_metadata=metadata, project=project)
    except Exception as e:
        print(f"ERROR: cannot create release: {e}", file=sys.stderr)
        return 1

    primary = summary[0]
    result = {
        "release_id": release.id,
        "name": release.name,
        "version": primary["version"],
        "branch": primary["branch"],
        "artifacts": summary,
        "url": release_web_url(release, org_url, project),
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"Created release {release.name} (id={release.id})")
    print(f"  version: {result['version']}")
    print(f"  branch:  {result['branch'] or '?'}")
    print(f"  {result['url']}")
    return 0


def resolve_environment(release: Any, name: str) -> Any:
    """Find a stage on a release by name (case-insensitive)."""
    environments = getattr(release, "environments", None) or []
    for env in environments:
        if (env.name or "").lower() == name.lower():
            return env
    available = ", ".join(e.name for e in environments if e.name) or "(none)"
    raise LookupError(f"no stage named {name!r} on release {release.name}. Available: {available}")


def latest_release(release_client, project: str, definition_id: int) -> Any | None:
    releases = release_client.get_releases(
        project=project, definition_id=definition_id, top=1, expand="environments"
    ) or []
    return releases[0] if releases else None


def _pending_approvers(env: Any) -> list[str]:
    names: list[str] = []
    for approval in getattr(env, "pre_deploy_approvals", None) or []:
        if (getattr(approval, "status", "") or "") != "pending":
            continue
        approver = getattr(approval, "approver", None)
        display = getattr(approver, "display_name", None) if approver else None
        names.append(display or "unknown")
    return names


def _walk_deploy_tasks(env: Any):
    """Yield (phase_name, task) for the most recent deployment attempt."""
    steps = getattr(env, "deploy_steps", None) or []
    if not steps:
        return
    attempt = steps[-1]
    for phase in getattr(attempt, "release_deploy_phases", None) or []:
        for job in getattr(phase, "deployment_jobs", None) or []:
            for task in getattr(job, "tasks", None) or []:
                yield phase.name or "phase", task


def watch_deployment(
    release_client, project: str, release_id: int, env_name: str, poll: int = POLL_SECONDS
) -> Any:
    """Poll a release stage until it reaches a terminal status, printing task results."""
    seen: dict[str, str] = {}
    announced_approval = False
    print(f"Watching {env_name} on release {release_id} "
          f"(polling every {poll}s, Ctrl-C to stop watching)...")
    while True:
        release = release_client.get_release(
            project=project, release_id=release_id, expand="environments"
        )
        env = resolve_environment(release, env_name)

        if not announced_approval:
            approvers = _pending_approvers(env)
            if approvers:
                print(f"  waiting on pre-deploy approval from: {', '.join(approvers)}")
                announced_approval = True

        for phase_name, task in _walk_deploy_tasks(env):
            key = str(task.id)
            status = f"{task.status}"
            if seen.get(key) == status:
                continue
            seen[key] = status
            if task.status in ("succeeded", "failed", "skipped", "partiallySucceeded"):
                print(f"  [{task.status}] {phase_name} / {task.name}")
            elif task.status == "inProgress":
                print(f"  [running] {phase_name} / {task.name}")

        if env.status in TERMINAL_ENV_STATUSES:
            print(f"\nStage {env_name} finished: {env.status}")
            return env
        time.sleep(poll)


def cmd_deploy(args: argparse.Namespace) -> int:
    org_url, project = resolve_context(args)
    if not args.pipeline and not args.release:
        print("ERROR: provide a release pipeline name or --release <id>.", file=sys.stderr)
        return 1
    try:
        variables = parse_variables(args.var)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    conn = get_connection(org_url)
    release_client = conn.clients_v7_1.get_release_client()

    try:
        if args.release:
            release = release_client.get_release(
                project=project, release_id=int(args.release), expand="environments"
            )
        else:
            definition = resolve_release_definition(release_client, project, args.pipeline)
            release = latest_release(release_client, project, definition.id)
            if not release:
                print(f"ERROR: no releases exist for {definition.name!r}", file=sys.stderr)
                return 1
        env = resolve_environment(release, args.env)
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: cannot resolve release: {e}", file=sys.stderr)
        return 1

    print(f"Deploying {release.name} (id={release.id}) to {env.name} "
          f"[current status: {env.status}]...")

    metadata = ReleaseEnvironmentUpdateMetadata(
        status="inProgress",
        comment=args.comment,
        variables={
            k: ConfigurationVariableValue(value=v) for k, v in variables.items()
        } or None,
    )
    try:
        updated = release_client.update_release_environment(
            environment_update_data=metadata,
            project=project,
            release_id=release.id,
            environment_id=env.id,
        )
    except Exception as e:
        print(f"ERROR: cannot start deployment: {e}", file=sys.stderr)
        return 1

    print(f"Deployment queued for {env.name} (status: {getattr(updated, 'status', 'unknown')})")
    print(f"  {release_web_url(release, org_url, project)}")

    if not args.watch:
        return 0

    try:
        finished = watch_deployment(release_client, project, release.id, env.name, args.poll)
    except KeyboardInterrupt:
        print("\nStopped watching; deployment continues.", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"ERROR: while watching deployment: {e}", file=sys.stderr)
        return 1
    return 0 if finished.status == "succeeded" else 1


def parse_watch_target(target: str) -> dict[str, Any]:
    """Parse a build/release id or an ADO results URL into a watch target.

    Recognises `.../_build/results?buildId=N`, `.../_release?releaseId=N`
    (optionally `&environmentId=M`), and bare numeric ids.
    """
    target = target.strip()
    if target.isdigit():
        return {"kind": None, "id": int(target), "environment_id": None, "url": None}

    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme:
        raise ValueError(f"target must be a numeric id or an ADO URL, got {target!r}")
    query = urllib.parse.parse_qs(parsed.query)

    def _int(name: str) -> int | None:
        values = query.get(name) or []
        return int(values[0]) if values and values[0].isdigit() else None

    build_id = _int("buildId")
    if build_id:
        return {"kind": "build", "id": build_id, "environment_id": None, "url": target}
    release_id = _int("releaseId")
    if release_id:
        return {
            "kind": "release",
            "id": release_id,
            "environment_id": _int("environmentId"),
            "url": target,
        }
    raise ValueError(
        f"cannot find buildId or releaseId in url: {target!r}"
    )


def resolve_environment_by_id(release: Any, environment_id: int) -> Any:
    for env in getattr(release, "environments", None) or []:
        if env.id == environment_id:
            return env
    available = ", ".join(
        f"{e.name}({e.id})" for e in (getattr(release, "environments", None) or []) if e.name
    ) or "(none)"
    raise LookupError(
        f"no stage with id {environment_id} on release {release.name}. Available: {available}"
    )


def pick_active_environment(release: Any) -> Any:
    """Choose the stage worth watching: the running one, else the last one touched."""
    environments = getattr(release, "environments", None) or []
    if not environments:
        raise LookupError(f"release {release.name} has no stages")
    for status in ("inProgress", "queued", "scheduled"):
        for env in environments:
            if env.status == status:
                return env
    started = [e for e in environments if e.status != "notStarted"]
    if started:
        return max(started, key=lambda e: e.rank or 0)
    names = ", ".join(e.name for e in environments if e.name)
    raise LookupError(
        f"no stage on release {release.name} has started; pass --env. Stages: {names}"
    )


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        target = parse_watch_target(args.target)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # A results URL carries the org/project, so reuse it for context resolution.
    if target["url"] and not args.url:
        args.url = target["url"]
    org_url, project = resolve_context(args)

    kind = args.kind or target["kind"]
    if kind is None:
        kind = "release" if args.env else "build"

    conn = get_connection(org_url)

    if kind == "build":
        if args.env:
            print("ERROR: --env only applies when watching a release.", file=sys.stderr)
            return 1
        build_client = conn.clients_v7_1.get_build_client()
        try:
            build = build_client.get_build(project=project, build_id=target["id"])
        except Exception as e:
            print(f"ERROR: cannot fetch build {target['id']}: {e}", file=sys.stderr)
            return 1
        definition_name = getattr(getattr(build, "definition", None), "name", "?")
        print(f"Build {build.id} — {definition_name} "
              f"{build.build_number or ''} on {_short_branch(build.source_branch) or '?'}")
        print(f"  {build_web_url(build, org_url, project)}")
        if build.status == "completed":
            print(f"Already finished: {_fmt_result(build.status, build.result)}")
            return 0 if build.result == "succeeded" else 1
        try:
            finished = watch_build(build_client, project, build.id, args.poll)
        except KeyboardInterrupt:
            print("\nStopped watching; build continues running.", file=sys.stderr)
            return 0
        except Exception as e:
            print(f"ERROR: while watching build: {e}", file=sys.stderr)
            return 1
        return 0 if finished.result == "succeeded" else 1

    release_client = conn.clients_v7_1.get_release_client()
    try:
        release = release_client.get_release(
            project=project, release_id=target["id"], expand="environments"
        )
        if args.env:
            env = resolve_environment(release, args.env)
        elif target["environment_id"]:
            env = resolve_environment_by_id(release, target["environment_id"])
        else:
            env = pick_active_environment(release)
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: cannot fetch release {target['id']}: {e}", file=sys.stderr)
        return 1

    print(f"Release {release.name} (id={release.id}) — stage {env.name} [{env.status}]")
    print(f"  {release_web_url(release, org_url, project)}")
    if env.status in TERMINAL_ENV_STATUSES:
        print(f"Already finished: {env.status}")
        return 0 if env.status == "succeeded" else 1
    try:
        finished = watch_deployment(release_client, project, release.id, env.name, args.poll)
    except KeyboardInterrupt:
        print("\nStopped watching; deployment continues.", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"ERROR: while watching deployment: {e}", file=sys.stderr)
        return 1
    return 0 if finished.status == "succeeded" else 1


def add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=None,
        help="ADO URL to infer org/project, e.g. https://dev.azure.com/{org}/{project}/",
    )
    parser.add_argument("--org", default=None, help=f"Org URL (default: {DEFAULT_ORG}).")
    parser.add_argument("--project", default=None, help=f"Project (default: {DEFAULT_PROJECT}).")


def main() -> int:
    p = argparse.ArgumentParser(prog="pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="Queue a build pipeline on a branch.")
    b.add_argument("pipeline", help="Build pipeline name, e.g. Nexus-FrontEnd-CI.")
    b.add_argument(
        "--branch",
        default=None,
        help="Branch to build, e.g. dev or release/1.2. Required unless --list-vars.",
    )
    b.add_argument("--pool", default=None, help="Agent pool/queue name to override the default.")
    b.add_argument(
        "--var",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Queue-time variable override, e.g. --var RunSonarQubeStep=true; repeatable.",
    )
    b.add_argument(
        "--list-vars",
        action="store_true",
        help="List the pipeline's variables and whether they are settable, then exit.",
    )
    b.add_argument("--watch", action="store_true", help="Follow the build until it completes.")
    b.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON (waits for the build number to be assigned).",
    )
    b.add_argument(
        "--poll",
        type=int,
        default=POLL_SECONDS,
        help=f"Seconds between polls when watching (default: {POLL_SECONDS}).",
    )
    add_context_args(b)
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("release", help="Create a release from a release pipeline.")
    r.add_argument("pipeline", help="Release pipeline name, e.g. EXAMPLE-API-CD.")
    r.add_argument(
        "--version",
        default=None,
        help="Artifact build number to release (default: newest successful build).",
    )
    r.add_argument(
        "--branch",
        default=None,
        help="Only consider artifact builds from this branch when picking the version.",
    )
    r.add_argument("--description", default=None, help="Release description.")
    r.add_argument(
        "--var",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Release variable override; repeatable.",
    )
    r.add_argument(
        "--manual",
        action="store_true",
        help="Hold every stage for manual deployment (use `pipeline deploy` after).",
    )
    r.add_argument("--draft", action="store_true", help="Create the release as a draft.")
    r.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    add_context_args(r)
    r.set_defaults(func=cmd_release)

    d = sub.add_parser("deploy", help="Deploy a release to an environment.")
    d.add_argument(
        "pipeline",
        nargs="?",
        default=None,
        help="Release pipeline name; its latest release is used unless --release is given.",
    )
    d.add_argument("--env", required=True, help="Environment/stage name, e.g. DEV or QA.")
    d.add_argument("--release", default=None, help="Release id to deploy (default: latest).")
    d.add_argument("--comment", default=None, help="Deployment comment.")
    d.add_argument(
        "--var",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Stage variable override; repeatable.",
    )
    d.add_argument("--watch", action="store_true", help="Follow the deployment until it finishes.")
    d.add_argument(
        "--poll",
        type=int,
        default=POLL_SECONDS,
        help=f"Seconds between polls when watching (default: {POLL_SECONDS}).",
    )
    add_context_args(d)
    d.set_defaults(func=cmd_deploy)

    w = sub.add_parser("watch", help="Follow a running build or deployment by id or URL.")
    w.add_argument(
        "target",
        help="Build/release id, or an ADO URL containing buildId= or releaseId=.",
    )
    w.add_argument(
        "--kind",
        choices=["build", "release"],
        default=None,
        help="Disambiguate a bare id (default: build, or release when --env is given).",
    )
    w.add_argument(
        "--env",
        default=None,
        help="Stage to follow on a release (default: the running or most recent one).",
    )
    w.add_argument(
        "--poll",
        type=int,
        default=POLL_SECONDS,
        help=f"Seconds between polls (default: {POLL_SECONDS}).",
    )
    add_context_args(w)
    w.set_defaults(func=cmd_watch)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
