# ado_actions

[![PyPI - Version](https://img.shields.io/pypi/v/ado_actions.svg)](https://pypi.org/project/ado_actions)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/ado_actions.svg)](https://pypi.org/project/ado_actions)

-----

## Description

Tools for working with Azure DevOps Test Plans. The main entry point is
`ado_actions.fetch_test_plan`, which downloads every test case from a
Test Plan, dumps them to JSON, and writes one Markdown file per case
(optionally grouped by parent User Story, optionally including the
story definition itself).

For background on Plan / Suite / Test Case / User Story IDs and how
they relate, see [`test-suite.md`](./test-suite.md).

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

The CLI has three subcommands: `fetch`, `show-plan`, and `show-story`.

### Fetch a whole plan

Basic — flat output, one markdown per test case:

```fish
uv run ado fetch --plan-id 1001
```

Group cases by their parent User Story (uses the `TestedBy` work item
relation), write a `story-index.md`, and pull the User Story
definition into each story folder:

```fish
uv run ado fetch \
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

### Show the suite hierarchy of a plan

Print the folder/suite tree of a plan without fetching test cases —
useful for finding a `--folder-id` to scope a later run, or just
auditing how a plan is organised:

```fish
uv run ado show-plan --plan-id 1001
```

Limit the tree to one subtree:

```fish
uv run ado show-plan --plan-id 1001 --folder-id 2004
```

Each line is prefixed with `[F]` for folder/static suites and `[S]`
for other suite types.

### Inspect a single User Story from the JSON

After running `fetch`, query the resulting JSON for one story without
hitting the API again:

```fish
uv run ado show-story 11336 \
  --json test_cases_plan_1001.json
```

Sample output:

```
Story 11336: Export Requests to Excel (model+support)
10 test case(s):
   12877  [Design    ] 11336- Verify Export button is available on Model Requests dashboard
   12878  [Design    ] 11336- Verify Export button is available on Support Requests dashboard
   …
```


### Work item status (`board work-status`)

Merge & deployment state for **any** work item — Task, Bug, User Story,
Feature or Epic. It walks the item's whole subtree for linked Pull
Requests, checks which ones landed on `dev`, and matches their merge
commits against the artifacts of recent releases to tell you which
environments already carry the work.

Takes an id or a full work-item URL (org and project are inferred from
the URL):

```fish
uv run board work-status 7638343
uv run board work-status https://dev.azure.com/<org>/<project>/_workitems/edit/7638343
```

Report on the hierarchy below the item as well — same flags as
`board fetch-work`:

```fish
uv run board work-status 7291258 --children      # each direct child too
uv run board work-status 7291258 --depth 2       # children and grandchildren
uv run board work-status 7291258 --recursive     # the whole tree
```

| Flag | Default | Notes |
|------|---------|-------|
| `ref` | _(required)_ | Work item id or `_workitems/edit/<id>` URL |
| `--org` / `--project` | parsed from the URL, else env | Override the inferred context |
| `--children` | off | Also report each direct child (alias `--include-child`) |
| `--depth N` | — | Report N levels down; overrides `--children` |
| `--recursive` | off | Whole child hierarchy; overrides `--depth` |
| `--releases-top` | 5 | Recent releases inspected per pipeline |
| `-v`, `--verbose` | off | Full PR list and per-release detail |

Deployment lines only appear for the repos named by `AZDO_BACKEND_REPO`
/ `AZDO_FRONTEND_REPO` and their `*_RELEASE_DEF` pipelines. PRs found in
any other repo are still summarised, just without release data.

(This command replaces the old `ado story-status`, which was
story-only.)

## Features

- Makefile for common tasks
- Sphinx 
- pytest
- `.gitignore` for python + nodejs projects + emacs + vim + vscode (using topal gitignore generation tool)
- jupyter as optional dependency + jupytext
- linting tools

## Dependencies

Dependencies are handled by [uv->dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/#changing-dependencies)

Adding a main dependency:

```
uv add flask
```

Add dev dependency

```
uv add --dev ruff
```

Fresh install: 
```
uv sync --locked
```

Export:
```
uv export --format requirements-txt
```

## References

- https://waylonwalker.com/hatch-version/

## License

`ado_actions` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
