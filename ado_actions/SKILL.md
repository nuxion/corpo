---
name: ado
description: >
  Use the `ado` CLI to work with Azure DevOps: fetch test plans and test
  cases, inspect sprints and work items with their PR/release status,
  drive builds/releases/deployments, and review pull requests with an AI
  agent. Trigger whenever the user asks about ADO test plans, work items,
  sprints, builds, releases, deployments, or PR review in this repo.
metadata:
  author: nuxion
  version: "1.0"
---

`ado` is this repo's console script (`ado_actions.cli:cli`, installed via
`pyproject.toml`). Run it with `uv run ado <...>` or, after `uv sync`,
directly as `ado`.

For full background and flag references, see:

- [`README.md`](../README.md) — setup and quick usage
- [`ado.spec.md`](../ado.spec.md) — full CLI specification
- [`test-suite.md`](../test-suite.md) — Plan / Suite / Test Case / User Story ID model
- [`RELEASE.md`](../RELEASE.md) — release process for the `ado` binary

## Setup required before running anything

`ado` reads Azure DevOps config from the environment (no org/project/repo
names are committed). Confirm these are set before invoking commands —
`.envrc` (gitignored) is the intended place for them:

| Variable | Required | Notes |
|----------|----------|-------|
| `AZDO_PAT` (or `PAT`) | yes | Personal Access Token — needs Test Management: Read, Work Items: Read |
| `AZDO_ORG` | yes | Org URL, e.g. `https://dev.azure.com/<org>` |
| `AZDO_PROJECT` | yes | ADO project name |
| `AZDO_BACKEND_REPO` / `AZDO_FRONTEND_REPO` | for `ado repo` cmds | Git repo names |
| `AZDO_BACKEND_RELEASE_DEF` / `AZDO_FRONTEND_RELEASE_DEF` | for `ado pipeline release`/`deploy` | Release pipeline names |

If a variable is missing, ask the user rather than guessing org/project
names.

## Command groups

`ado` groups commands by area; every level supports `--help`.

| Group | Commands | Covers |
|-------|----------|--------|
| `ado testing` | `fetch`, `show-plan`, `show-story` | Test plans, suites, test cases |
| `ado board` | `sprint`, `fetch-work`, `work-status` | Sprints and work items |
| `ado pipeline` | `build`, `release`, `deploy`, `watch` | Builds, releases, deployments |
| `ado repo` | `review` | AI-driven pull request review |

### `ado testing` — test plans

```fish
uv run ado testing fetch --plan-id 1001
uv run ado testing fetch --plan-id 1001 --md-dir tests2/plan-1001 --group-by-story --include-story
uv run ado testing show-plan --plan-id 1001 [--folder-id 2004]
uv run ado testing show-story 11336 --json test_cases_plan_1001.json
```

`show-plan` prefixes each line `[F]` (folder/static suite) or `[S]`
(other suite type) — useful for finding a `--folder-id` before a scoped
`fetch`. `show-story` queries a previously-fetched JSON dump without
hitting the API again.

### `ado board` — sprints and work items

```fish
uv run ado board fetch-work 7102940
uv run ado board sprint
uv run ado board work-status 7638343 --recursive
uv run ado board work-status https://dev.azure.com/<org>/<project>/_workitems/edit/7638343
```

`work-status` accepts either a bare id or a full work-item URL (org/project
inferred from the URL), and works on Task, Bug, User Story, Feature or
Epic. `sprint` and `work-status` both merge PR and release data to show
which environments already carry a piece of work.

### `ado pipeline` — builds, releases, deployments

```fish
uv run ado pipeline build Nexus-FrontEnd-CI --branch dev --watch
uv run ado pipeline release EXAMPLE-API-CD --manual
uv run ado pipeline deploy EXAMPLE-API-CD --env QA --watch
uv run ado pipeline watch <build-or-release-id>
```

Release/deploy commands require `AZDO_BACKEND_RELEASE_DEF` /
`AZDO_FRONTEND_RELEASE_DEF`.

### `ado repo` — PR review

```fish
uv run ado repo review https://dev.azure.com/<org>/<proj>/_git/<repo>/pullrequest/<id>
```

Runs from a disposable git worktree of the PR's branch. Use `--copilot`
instead of the default `claude` CLI, or `--repo`/`--pr` instead of a URL.

## Working conventions

- Prefer `uv run ado ...` over calling the module directly — it uses the
  locked environment from `uv sync --locked`.
- Don't invent org/project/repo/pipeline names; read them from the
  environment or ask the user.
- For anything beyond the examples above (less common flags, edge cases),
  check `ado <group> <command> --help` or [`ado.spec.md`](../ado.spec.md)
  rather than guessing.
