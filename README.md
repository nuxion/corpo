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

### 2. Set your Azure DevOps Personal Access Token

The script reads the PAT from `AZDO_PAT` (preferred) or `PAT`. The
token needs at least:

- **Test Management — Read**
- **Work Items — Read**

```fish
set -x AZDO_PAT '<your-pat>'
```

### 3. PYTHONPATH

The package lives at `ado_actions/ado_actions/`, so the outer
directory must be on `sys.path`:

```fish
set -x PYTHONPATH ado_actions
```

(Or prefix individual commands with `PYTHONPATH=ado_actions`.)

## Running

The CLI uses two subcommands: `fetch` and `show-story`.

### Fetch a whole plan

Basic — flat output, one markdown per test case:

```fish
uv run python -m ado_actions.fetch_test_plan fetch --plan-id 1001
```

Group cases by their parent User Story (uses the `TestedBy` work item
relation), write a `story-index.md`, and pull the User Story
definition into each story folder:

```fish
uv run python -m ado_actions.fetch_test_plan fetch \
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
| `--project` | `example-project` | ADO project name |
| `--org` | `https://dev.azure.com/example-org` | Org URL |
| `--out-json` | `test_cases_plan_<planId>.json` | JSON dump path |
| `--md-dir` | `tests/plan-<planId>` | Root markdown directory |
| `--group-by-story` | off | Subdirs `story-<parentId>/` via TestedBy relation |
| `--include-story` | off | Also write the User Story definition (requires `--group-by-story`) |

### Inspect a single User Story from the JSON

After running `fetch`, query the resulting JSON for one story without
hitting the API again:

```fish
uv run python -m ado_actions.fetch_test_plan show-story 11336 \
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

### One-liner without exporting PYTHONPATH

```fish
PYTHONPATH=ado_actions uv run python -m ado_actions.fetch_test_plan fetch \
  --plan-id 1001 --group-by-story --include-story
```


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
