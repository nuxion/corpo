# ado_actions

Tools for working with Azure DevOps: fetching Test Plans, reporting on
work items and their PR/release status, and driving builds, releases
and deployments — all from one `ado` CLI.

For background on Plan / Suite / Test Case / User Story IDs and how
they relate, see [`test-suite.md`](./test-suite.md). For the full
command reference (every `board` and `pipeline` flag, with examples),
see [`ado.spec.md`](./ado.spec.md).

-----

## Setup

### 1. Install dependencies

```fish
uv sync --locked
```

### 2. Set your Azure DevOps environment

No organization, project, repo, or pipeline names are committed to this
repo. Supply them through the environment — `.envrc` is gitignored and is
the intended place for them.

The script reads the PAT from `AZDO_PAT` (preferred) or `PAT`. The
token needs at least:

- **Test Management — Read**
- **Work Items — Read**

```fish
set -x AZDO_PAT '<your-pat>'
set -x AZDO_ORG 'https://dev.azure.com/<your-org>'
set -x AZDO_PROJECT '<your-project>'

# Optional — only needed for the repo/release commands
set -x AZDO_BACKEND_REPO '<backend-repo-name>'
set -x AZDO_FRONTEND_REPO '<frontend-repo-name>'
set -x AZDO_BACKEND_RELEASE_DEF '<backend-release-definition>'
set -x AZDO_FRONTEND_RELEASE_DEF '<frontend-release-definition>'
```

| Variable | Required | Notes |
|----------|----------|-------|
| `AZDO_PAT` (or `PAT`) | yes | Personal Access Token |
| `AZDO_ORG` | yes | Org URL; default is the `<org>` placeholder |
| `AZDO_PROJECT` | yes | Project name |
| `AZDO_BACKEND_REPO` / `AZDO_FRONTEND_REPO` | for repo cmds | Git repo names |
| `AZDO_BACKEND_RELEASE_DEF` / `AZDO_FRONTEND_RELEASE_DEF` | for release cmds | Release pipeline names |

## Running

The package installs an `ado` console script (see
`[project.scripts]` in `pyproject.toml`). Run it with `uv run ado` or,
after `uv sync`, directly as `ado`.

`ado` groups its commands by area:

| Group | Commands | What it covers |
|-------|----------|----------------|
| `ado testing` | `fetch`, `show-plan`, `show-story` | Test plans, suites and test cases |
| `ado board` | `sprint`, `fetch-work`, `work-status` | Sprints and work items |
| `ado pipeline` | `build`, `release`, `deploy`, `watch` | Builds, releases and deployments |
| `ado repo` | `review` | Pull request review via an AI agent |

Every level has `--help`, e.g. `ado testing --help` or
`ado board work-status --help`.

### Test plans (`ado testing`)

Fetch a whole plan — flat output, one markdown per test case:

```fish
uv run ado testing fetch --plan-id 1001
```

Group cases by their parent User Story (uses the `TestedBy` work item
relation), write a `story-index.md`, and pull the User Story
definition into each story folder:

```fish
uv run ado testing fetch \
  --plan-id 1001 \
  --md-dir tests2/plan-1001 \
  --group-by-story \
  --include-story
```

Output layout (with `--group-by-story --include-story`):

```
test_cases_plan_1001.json
tests2/plan-1001/
├── story-index.md
├── story-11336/
│   ├── story-11336-<slug>.md         ← User Story definition
│   ├── 12877-<slug>.md               ← each test case
│   └── …
└── story-none/                        ← cases with no TestedBy parent
```

Flags:

| Flag | Default | Notes |
|------|---------|-------|
| `--plan-id` | _(required)_ | Test Plan ID |
| `--project` | `$AZDO_PROJECT` | ADO project name |
| `--org` | `$AZDO_ORG` | Org URL |
| `--out-json` | `test_cases_plan_<planId>.json` | JSON dump path |
| `--md-dir` | `tests/plan-<planId>` | Root markdown directory |
| `--group-by-story` | off | Subdirs `story-<parentId>/` via TestedBy relation |
| `--include-story` | off | Also write the User Story definition (requires `--group-by-story`) |

Show the suite hierarchy of a plan without fetching test cases —
useful for finding a `--folder-id` to scope a later run, or just
auditing how a plan is organised:

```fish
uv run ado testing show-plan --plan-id 1001
# limit to one subtree:
uv run ado testing show-plan --plan-id 1001 --folder-id 2004
```

Each line is prefixed with `[F]` for folder/static suites and `[S]`
for other suite types.

After running `fetch`, query the resulting JSON for one story without
hitting the API again:

```fish
uv run ado testing show-story 11336 --json test_cases_plan_1001.json
```

Sample output:

```
Story 11336: Export Requests to Excel (model+support)
10 test case(s):
   12877  [Design    ] 11336- Verify Export button is available on Model Requests dashboard
   12878  [Design    ] 11336- Verify Export button is available on Support Requests dashboard
   …
```

### Sprints and work items (`ado board`)

`fetch-work` downloads a single work item (and optionally its
children) as Markdown; `sprint` and `work-status` both merge PR and
release data to show which environments already carry a piece of
work — `sprint` for every story in an iteration, `work-status` for one
work item (Task, Bug, User Story, Feature or Epic) and, optionally,
its subtree:

```fish
uv run ado board fetch-work 7102940
uv run ado board sprint
uv run ado board work-status 7638343 --recursive
```

`work-status` takes an id or a full work-item URL (org and project are
inferred from the URL):

```fish
uv run ado board work-status https://dev.azure.com/<org>/<project>/_workitems/edit/7638343
```

(`work-status` replaces the old `ado story-status`, which was
story-only.) See `ado board <command> --help` or
[`ado.spec.md`](./ado.spec.md) for the full flag reference and more
examples.

### Builds, releases and deployments (`ado pipeline`)

```fish
uv run ado pipeline build Nexus-FrontEnd-CI --branch dev --watch
uv run ado pipeline release EXAMPLE-API-CD --manual
uv run ado pipeline deploy EXAMPLE-API-CD --env QA --watch
uv run ado pipeline watch <build-or-release-id>
```

Deployment/release commands rely on `AZDO_BACKEND_RELEASE_DEF` /
`AZDO_FRONTEND_RELEASE_DEF` (see [Setup](#2-set-your-azure-devops-environment)).
See `ado pipeline <command> --help` or [`ado.spec.md`](./ado.spec.md)
for the full flag reference and more examples.

### Pull request review (`ado repo`)

Review an ADO pull request with an AI agent, run from a disposable git
worktree of the PR's branch:

```fish
uv run ado repo review https://dev.azure.com/<org>/<proj>/_git/<repo>/pullrequest/<id>
```

Use `--copilot` instead of the default `claude` CLI, or pass `--repo`
/ `--pr` in place of a URL. See `ado repo review --help` for the rest
of the flags.

## Development

- `make` — see `make help` for the available tasks (build zipapps,
  clean build artifacts, etc.).
- `pytest` for tests, `ruff check` for linting.
- `.gitignore` covers python, node, emacs, vim and vscode (generated
  with the [toptal gitignore tool](https://www.toptal.com/developers/gitignore)).

Dependencies are handled by [uv](https://docs.astral.sh/uv/concepts/projects/dependencies/#changing-dependencies):

```fish
uv add flask          # main dependency
uv add --dev ruff      # dev dependency
uv sync --locked       # fresh install
uv export --format requirements-txt   # export
```

## Releases

`ado` ships as a single-file executable built with
[shiv](https://github.com/linkedin/shiv) and published as a GitHub
Release asset — there is no PyPI package. See
[`RELEASE.md`](./RELEASE.md) for the full release process, or run:

```fish
make release RELEASE_VERSION=x.y.z
```

## References

- [`ado.spec.md`](./ado.spec.md) — full CLI specification
- [`RELEASE.md`](./RELEASE.md) — release process
- https://waylonwalker.com/hatch-version/

## License

`ado_actions` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
