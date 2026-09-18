# Corpo

Tools for working with Azure DevOps: fetching Test Plans, reporting on
work items and their PR/release status, and driving builds, releases
and deployments — all from one `ado` CLI.

> **Status: beta.** Commands and flags may still change between
> releases. This project — code, docs and release tooling — is built
> almost entirely with AI assistance; review its output before relying
> on it for anything critical.

-----

## Quickstart (for users)

### 1. Download

Grab the latest single-file binary from the
[GitHub Releases page](https://github.com/nuxion/corpo/releases):

```fish
curl -L https://github.com/nuxion/corpo/releases/latest/download/ado -o ~/.local/bin/ado
chmod +x ~/.local/bin/ado
```

No Python install or virtualenv needed — `ado` ships as a self-contained
[shiv](https://github.com/linkedin/shiv) executable.

### 2. Install

Make sure `~/.local/bin` (or wherever you put the binary) is on your
`PATH`, then confirm it runs:

```fish
ado --version
```

### 3. Configure your Azure DevOps environment

No organization, project, repo, or pipeline names are committed to this
repo — supply them through environment variables. The CLI reads the PAT
from `AZDO_PAT` (preferred) or `PAT`; the token needs at least:

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

A `.envrc` file (gitignored) is the intended place to keep these locally.

### 4. Main commands

`ado` groups its commands by area; every level has `--help`, e.g.
`ado testing --help` or `ado board work-status --help`.

| Group | Commands | What it covers |
|-------|----------|-----------------|
| `ado testing` | `fetch`, `show-plan`, `show-story` | Test plans, suites and test cases |
| `ado board` | `sprint`, `fetch-work`, `work-status` | Sprints and work items |
| `ado pipeline` | `build`, `release`, `deploy`, `watch` | Builds, releases and deployments |
| `ado repo` | `review` | Pull request review via an AI agent |

A few examples:

```fish
# Fetch a test plan as markdown, one file per test case
ado testing fetch --plan-id 1001

# Show which environments already carry a story's work
ado board work-status 7638343 --recursive

# Kick off a build and watch it to completion
ado pipeline build Nexus-FrontEnd-CI --branch dev --watch

# Review an ADO pull request with an AI agent
ado repo review https://dev.azure.com/<org>/<proj>/_git/<repo>/pullrequest/<id>
```

For every flag, output layout and more examples, see
[`ado.spec.md`](./ado.spec.md) (full CLI specification) and
[`test-suite.md`](./test-suite.md) (background on Plan / Suite / Test
Case / User Story IDs and how they relate). A ready-made
[Claude Code skill](./ado_actions/SKILL.md) covers the same ground for AI
assistants.

-----

## Development

Requires [uv](https://docs.astral.sh/uv/concepts/projects/dependencies/).

```fish
uv sync --locked       # install dependencies from the lockfile
uv run ado --help      # run from source instead of the built binary
```

Dependency changes:

```fish
uv add flask          # main dependency
uv add --dev ruff      # dev dependency
uv export --format requirements-txt   # export
```

- `make` — see `make help` for the available tasks (build zipapps,
  clean build artifacts, etc.).
- `pytest` for tests, `ruff check` for linting.
- `.gitignore` covers python, node, emacs, vim and vscode (generated
  with the [toptal gitignore tool](https://www.toptal.com/developers/gitignore)).

-----

## Releases

`ado` ships as a single-file executable built with
[shiv](https://github.com/linkedin/shiv) and published as a GitHub
Release asset — there is no PyPI package.

```fish
make release RELEASE_VERSION=x.y.z
```

- See [`RELEASE.md`](./RELEASE.md) for the full, step-by-step release
  process (version bump, build, smoke test, publish).
- See [`CHANGELOG.md`](./CHANGELOG.md) for what shipped in each release.

## References

- [`ado_actions/SKILL.md`](./ado_actions/SKILL.md) — Claude Code skill for using the `ado` CLI
- [`ado.spec.md`](./ado.spec.md) — full CLI specification
- [`RELEASE.md`](./RELEASE.md) — release process
- [`CHANGELOG.md`](./CHANGELOG.md) — release history
- https://waylonwalker.com/hatch-version/

## License

`ado_actions` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
