---
name: ado
description: >
  How to drive the `ado` CLI (Azure DevOps) as an agent: pick the right
  command for a user's ask about test plans/cases, sprints, work items,
  builds, releases, deployments or PR review; know which commands read
  vs. mutate ADO; know what each writes to disk. Trigger whenever the
  user mentions ADO/Azure DevOps test plans, test cases, sprints, work
  items (IDs or `_workitems/edit/<id>` URLs), boards, pipelines, builds,
  releases, deployments, or reviewing an ADO pull request.
metadata:
  author: nuxion
  version: "2.0"
---

`ado` is a CLI for Azure DevOps. Invoke it directly (`ado ...`) if it is on
PATH; inside this repo prefer `uv run ado ...` so the locked environment is
used. Every level has `--help`; when a flag is not listed below, read
`ado <group> <command> --help` instead of guessing.

## Before the first call

`ado` takes org/project/repo/pipeline names from the environment, never from
hardcoded defaults. Check them once per session:

```bash
ado testing show-plan --help   # the [default: ...] values echo the env
```

| Variable | Needed for |
|----------|-----------|
| `AZDO_PAT` (or `PAT`) | everything — needs Test Management: Read, Work Items: Read |
| `AZDO_ORG` | everything (org URL, `https://dev.azure.com/<org>`) |
| `AZDO_PROJECT` | everything |
| `AZDO_BACKEND_REPO` / `AZDO_FRONTEND_REPO` | `board sprint`, `board work-status` repo columns |
| `AZDO_BACKEND_RELEASE_DEF` / `AZDO_FRONTEND_RELEASE_DEF` | `pipeline release`/`deploy`, env columns |

If a default renders as `<org>`/`<project>`, the env is not loaded (`.envrc`
is the intended, gitignored home for it) — ask the user rather than inventing
an org, project, repo or pipeline name. Never pass a guessed `--org`/
`--project`.

## Pick the command

| The user wants | Use |
|----------------|-----|
| "what's in test plan N" / suite tree | `ado testing show-plan --plan-id N` |
| test cases as files | `ado testing fetch --plan-id N` |
| test cases for one story, already fetched | `ado testing show-story <story-id> --json <dump>` |
| a work item's content as Markdown | `ado board fetch-work <id-or-url>` |
| "is this merged / where is it deployed" | `ado board work-status <id-or-url>` |
| same, for a whole sprint | `ado board sprint [url-or-iteration]` |
| run CI on a branch | `ado pipeline build <name> --branch <b>` |
| cut a release | `ado pipeline release <name>` |
| push a release to an env | `ado pipeline deploy <name> --env QA` |
| follow something already running | `ado pipeline watch <id-or-url>` |
| review a PR | `ado repo review <pr-url>` |

## Read-only vs. mutating

Safe to run unprompted: all of `ado testing`, all of `ado board`,
`ado pipeline watch`, and `ado pipeline build --list-vars`.

Ask the user first — these act on real ADO infrastructure and are not
undoable: `ado pipeline build` (queues a build), `ado pipeline release`
(creates a release), `ado pipeline deploy` (deploys to an environment).
Confirm pipeline name, branch/version and environment before firing, and do
not re-run one "to check" after it already succeeded.

`ado repo review` is safe against ADO (it only reads the PR) but it creates a
git worktree under `--worktree-dir` and shells out to the `claude` or
`copilot` CLI, so it is slow and costs tokens — run it only when asked.

## `ado testing`

```bash
ado testing show-plan --plan-id 1001 [--folder-id 2004]
ado testing fetch --plan-id 1001
ado testing fetch --plan-id 1001 --md-dir tests2/plan-1001 --group-by-story --include-story
ado testing show-story 11336 --json test_cases_plan_1001.json
```

- `show-plan` prints the suite tree, one line per node, prefixed `[F]` for a
  folder/static suite and `[S]` for any other suite type. Run it first to get
  a `--folder-id` when the user only cares about part of a plan.
- `fetch` hits the API and **writes files**: `test_cases_plan_<id>.json`
  (override with `--out-json`) plus one Markdown file per case under
  `tests/plan-<id>/` (override with `--md-dir`). `--group-by-story` nests
  them in `story-<parentId>/` via the TestedBy relation and also writes
  `story-index.md`; `--include-story` (requires `--group-by-story`) adds the
  story definition inside each folder. Tell the user where the files landed,
  and read them with Read/Grep rather than re-fetching.
- `show-story` reads a previous dump from disk — no API call. Prefer it over
  a second `fetch` when the JSON already exists (default `--json` is
  `test_cases_plan_1001.json`, so pass the real path explicitly).

Plan / Suite / Test Case / User Story ID semantics are documented in
[`test-suite.md`](../test-suite.md); read it before reasoning about which ID
a user means.

## `ado board`

```bash
ado board fetch-work 7102940 --stdout
ado board fetch-work 7102940 --recursive --split --out-dir reports
ado board work-status 7638343 --recursive
ado board work-status https://dev.azure.com/<org>/<project>/_workitems/edit/7638343
ado board sprint
ado board sprint --iteration 'example-project\PI 2 Sprint 7' --recursive
```

Both commands accept a bare ID or a full work-item URL; a URL also supplies
org/project, so prefer pasting the user's URL verbatim over splitting it into
flags. They work on Task, Bug, User Story, Feature and Epic alike.

- `fetch-work` writes Markdown to `reports/` by default. Use `--stdout` when
  you just need the content in context, `--out`/`--out-dir` when the user
  wants a file. Hierarchy: `--children` (= `--depth 1`), `--depth N`,
  `--recursive` (whole tree). `--split` writes one file per item
  (`EPIC-*.md`, `FEATURE-*.md`, `US-*.md`) and is incompatible with
  `--stdout`/`--out`.
- `work-status` merges PR and release data to answer "is it merged, which
  environments have it". Default output is a per-repo summary; `-v` adds the
  full PR list and per-release detail. `--recursive`/`--depth` report each
  descendant separately.
- `sprint` does the same across a sprint. With no argument it resolves the
  team's current iteration; pass a sprint URL or `--iteration`. `--team`
  matters because an iteration is shared — `--all-teams` widens it to the
  whole project.
- `--recursive` on `sprint`/`work-status` is usually what the user means
  (most PRs hang off child Tasks), but it costs several extra API calls per
  story and is noticeably slower. Mention the trade-off rather than silently
  running the slow path over a large sprint.

## `ado pipeline`

```bash
ado pipeline build <CI-name> --list-vars
ado pipeline build <CI-name> --branch dev --var RunSonarQubeStep=true --watch
ado pipeline release <CD-name> --manual
ado pipeline deploy <CD-name> --env QA --watch
ado pipeline watch <build-or-release-id>
ado pipeline watch 'https://dev.azure.com/.../_build/results?buildId=12345'
```

- `--list-vars` is the safe way to discover what `--var KEY=VALUE` accepts
  and which variables are settable at queue time. Run it before inventing a
  variable name.
- `--watch` blocks until the run finishes — that can be many minutes. For
  anything long, either run it in the background and report when it exits, or
  drop `--watch` and follow up with `ado pipeline watch <id>`.
- `--json` on `build`/`release` gives parseable output; prefer it when you
  need the build number or release id programmatically.
- `release --manual` holds every stage so a later `ado pipeline deploy` picks
  the environment — the right pattern when the user wants a release cut now
  but deployed deliberately. `release` defaults to the newest successful
  build; pin with `--version` or narrow with `--branch`.
- `watch` disambiguates a bare id with `--kind build|release`; a URL
  containing `buildId=`/`releaseId=` needs no flag.

## `ado repo review`

```bash
ado repo review https://dev.azure.com/<org>/<proj>/_git/<repo>/pullrequest/<id>
ado repo review --repo <repo> --pr <id> --out review.md
```

Creates a disposable worktree of the PR branch under `--worktree-dir`
(default `.worktrees`, relative to `--repo-path`, default cwd) and runs the
`claude` CLI (`--copilot` switches to Copilot). `--out` captures the review to
a file. Run from inside a clone of the target repo, or point `--repo-path` at
one.

## Handling failures

- `ERROR: set AZDO_PAT or PAT in the environment` → the env is not loaded;
  ask the user to source `.envrc`, do not export a token yourself.
- A 401/403 usually means the PAT lacks a scope (Test Management: Read, Work
  Items: Read) or expired — report it, do not retry in a loop.
- A pipeline/environment name that does not resolve prints the available
  options; surface that list to the user instead of guessing the next name.
- Anything not covered here: `ado <group> <command> --help`, then
  [`ado.spec.md`](../ado.spec.md) for the full specification.
  [`README.md`](../README.md) covers setup and [`RELEASE.md`](../RELEASE.md)
  the release process for `ado` itself.
