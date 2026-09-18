# `ado` CLI Specification

Language-agnostic specification for the `ado` command-line tool: a set of helpers for
working against Azure DevOps (ADO) boards and pipelines. This file is meant to be fed to
a coding agent (or read by a human) as the authoritative prompt for **implementing** the
CLI in ANY language (Python, Go, Rust, TypeScript, C#, ...). It describes **what** the
tool must do and **how** it must behave, not how to write it in a specific language.

**Scope.** This spec covers the `board` and `pipeline` command groups only. `repo` and
`testing` (test-plan) command groups are intentionally **omitted** and are out of scope
for an initial implementation built from this file.

**Conventions** (RFC 2119-style):

| Keyword | Meaning |
|---|---|
| **MUST** | Required behavior; an implementation is non-conformant without it. |
| **SHOULD** | Strongly recommended; deviate only with a documented reason. |
| **MAY** | Optional behavior an implementation is free to add. |

The [Extension points](#extension-points) section at the bottom is the supported place
for each repository/team to layer incremental or custom behavior **without** editing the
core contract above it.

| | |
|---|---|
| **Name** | `ado` |
| **Version** | `1.0.0` |
| **Status** | stable |

**Audience**
- Engineers building the CLI in a target language.
- Coding agents generating an implementation from this spec.

**Out of scope**
- The `repo` command group (repository operations) — not specified here.
- The `testing` command group (test-plan export) — not specified here.
- Automated test suites for the implementation (unit/integration tests) are not
  specified here; conformance is behavioral, verified by hand or by whatever the
  implementing team sets up separately.

---

## Concepts

Global concepts shared by every command.

### CLI shape

A single binary/entry point `ado` exposing command **groups**, each with
**subcommands** — `ado board <subcommand> [args]`, `ado pipeline <subcommand> [args]`.
`--version` **MUST** print the tool's version and exit 0.

### Organization

The ADO organization, identified by its base URL. Two URL shapes exist in the wild and
both **MUST** be supported everywhere an org URL is parsed or accepted:

- Modern: `https://dev.azure.com/<org>`
- Legacy: `https://<org>.visualstudio.com`

The resolved value **MUST** be a full base URL (scheme + host [+ `/<org>`]), because the
ADO REST/client libraries connect to that base URL.

### Project

The ADO Team Project that a command operates against.

### Authentication

- **Method:** Personal Access Token (PAT) via HTTP Basic auth.
- **Scheme:** Basic auth with an EMPTY username and the PAT as the password (username is
  `""`, password is the PAT). This matches the ADO convention.

**Token source.** PAT **MUST** be read from the environment, never hard-coded.

| Env var | Role |
|---|---|
| `AZDO_PAT` | primary |
| `PAT` | fallback, checked if `AZDO_PAT` is unset |

On missing: exit with a non-zero code and a clear message instructing the user to set
`AZDO_PAT` or `PAT`. **MUST NOT** prompt interactively by default.

**Scopes required**
- Work Items (Read & Write)
- Code (Read)
- Build (Read & Execute)
- Release (Read, Write, Execute & Manage)
- Agent Pools (Read)

### API

- **Client:** Use the official ADO client library for the target language when one
  exists (e.g. `azure-devops` for Python, `azure-devops-node-api` for TypeScript,
  `go-azuredevops` or raw REST for Go). Otherwise call the REST API directly.
- **REST version:** `7.1`

### Configuration

Defaults for org/project (and, for board sprint, repo-to-release-pipeline mappings)
**MUST** be overridable via environment variables so the same binary works across
organizations and projects without recompiling. Implementations **SHOULD** read a local
`.env`-style file (e.g. `.envrc`, gitignored) during development, but **MUST NOT**
require one at runtime — plain process environment variables **MUST** always work.

| Env var | Used by | Description |
|---|---|---|
| `AZDO_ORG` | board, pipeline | Default organization base URL. Example: `https://dev.azure.com/example-org` |
| `AZDO_ORG_URL` | pipeline | Alternate default-organization variable consulted by pipeline commands specifically, checked after `--org`/`--url` but before `AZDO_ORG`. Implementations **MAY** unify this with `AZDO_ORG` as long as both names are still honored for backward compatibility, in that precedence order. |
| `AZDO_PROJECT` | board, pipeline | Default project. Example: `example-project` |
| `AZDO_BACKEND_REPO` | board | Git repository name for the "Backend" component, used by `board sprint` and `board work-status` to discover the release pipeline that ships it. Optional — when unset, backend deployment reporting is simply skipped (not an error). |
| `AZDO_FRONTEND_REPO` | board | Git repository name for the "Frontend" component. Same semantics as `AZDO_BACKEND_REPO`. |
| `AZDO_BACKEND_RELEASE_DEF` | board | Release-pipeline (release definition) name that deploys `AZDO_BACKEND_REPO`. |
| `AZDO_FRONTEND_RELEASE_DEF` | board | Release-pipeline name that deploys `AZDO_FRONTEND_REPO`. |

### Work item

A board entity addressed by a numeric ID. Types include (non-exhaustive) Task, "User
Story", Bug, Feature, Epic. Work-item commands **MUST** be type-agnostic — they operate
on whatever type the ID resolves to.

### Work item ref

The argument shape accepted by every board subcommand that targets a single work item
(`fetch-work`, `work-status`). Either a bare numeric ID, or a full ADO work-item URL.

**Accepted forms**

| Kind | Example(s) | Effect |
|---|---|---|
| numeric-id | `7102940` | org and project are **not** derived from the ref; they fall back to flags then configured defaults. |
| full-url | `https://dev.azure.com/<org>/<project>/_workitems/edit/7102940`, same with trailing `/`, `https://<org>.visualstudio.com/<project>/_workitems/edit/7102940`, `https://dev.azure.com/<org>/<project>/_workitems/view/7102940` | The org base URL, project, and numeric ID **MUST** all be parsed from the URL. The project segment **MUST** be URL-decoded (e.g. `%20` becomes a space). |

**Resolution order** (highest priority first)
1. Explicit `--org` / `--project` flags.
2. Values parsed from the ref, when it is a URL.
3. Configured default (`AZDO_ORG` / `AZDO_PROJECT`).

**URL parsing**

Reference pattern (PCRE-style, case-insensitive). Implementations may adapt syntax to
their language but **MUST** match the same set of URLs:

```
https?://(?:dev\.azure\.com/(?P<org>[^/]+)/(?P<proj>[^/]+)
  |(?P<orgsub>[^./]+)\.visualstudio\.com/(?P<proj2>[^/]+))
  /_workitems/(?:edit|view)/(?P<id>\d+)
```

- **Precedence:** If the ref is all digits, treat it as a bare ID (no org/project
  parsed). Otherwise attempt to match the work-item URL pattern above.
- **Outputs:**
  - `id` — integer work-item id (required match group).
  - `org_url` — `https://dev.azure.com/<org>` for the modern form, or
    `https://<orgsub>.visualstudio.com` for the legacy form.
  - `project` — URL-decoded project segment.
- **On no match:** Exit non-zero with a message of the form
  `ERROR: cannot parse work item id/url: <ref>`.

### Pipeline context

How pipeline subcommands resolve `(org, project)`. Distinct from `work_item_ref` above
because pipeline commands are not addressed by a work-item id/URL — they take an
explicit `--url` option instead.

**Resolution order** (highest priority first)
1. Explicit `--org` / `--project` flags.
2. Org/project parsed from `--url`, when given.
3. `AZDO_ORG_URL` env var (org only).
4. `AZDO_ORG` / `AZDO_PROJECT` env var defaults.

**`--url` option:** An arbitrary ADO URL (work item, build, release, repo, ...) to infer
org and project from, e.g. `https://dev.azure.com/<org>/<project>/`. Recognizes both URL
shapes from [Organization](#organization). When the path segment right after the org is
a known ADO "collection" segment (`_git`, `_build`, `_release`, `_apis`, `_workitems`,
`_dashboards`), it **MUST NOT** be mistaken for the project — treat the project as
unresolved from the URL in that case.

On parse failure: exit non-zero with a message of the form
`ERROR: cannot parse organization from url: <url>`.

### Identity fields

Person-valued ADO fields (Assigned To, Created By, Changed By) may arrive as objects
with `displayName` / `uniqueName`. Render as `"<displayName> <<uniqueName>>"` when both
are present and differ, else whichever single value is present, else empty.

### Rich text

ADO rich-text fields (Description, Acceptance Criteria, Repro Steps, System Info) arrive
as HTML. They **MUST** be converted to readable Markdown wherever they are rendered.

**Rules**
- Headings (`h1`-`h6`) become `#`...`######` of the matching level.
- `<strong>`/`<b>` become `**bold**`; `<em>`/`<i>` become `*italic*`.
- `<code>` becomes inline code; `<pre>` becomes a fenced code block.
- `<ul>`/`<ol>`/`<li>` become Markdown lists, nested lists indented.
- `<a href=...>` becomes `[text](href)`.
- `<hr>` becomes a `---` line.
- `<br>`, `<p>`, `<div>` become line or paragraph breaks.
- Table cells (`<td>`/`<th>`) become `" | "`-separated text (best-effort; a full
  Markdown table is not required).

**Post-processing:** Collapse 3+ consecutive blank lines to exactly one blank line and
trim leading/trailing whitespace from the result. If conversion fails for any reason,
fall back to emitting the raw field content rather than crashing.

### Slugify

Shared helper for turning arbitrary text (e.g. a work-item title) into a
filesystem-safe slug, used by output file naming.

**Algorithm**
1. Lowercase the input.
2. Replace every run of characters that are not `[a-z0-9]` with a single hyphen.
3. Trim leading and trailing hyphens.
4. Truncate to 80 characters, then trim any trailing hyphen left by truncation.
5. If the result is empty, return `untitled`.

### Hierarchy

Work-item parent/child relations, used by `board fetch-work` and `board work-status` to
walk a subtree.

| Relation | ADO link type |
|---|---|
| child | `System.LinkTypes.Hierarchy-Forward` |
| parent | `System.LinkTypes.Hierarchy-Reverse` |

**ID extraction:** Each relation's url ends in the numeric work-item id
(`.../_apis/wit/workItems/<id>`); parse the trailing id from it.

**Depth flags.** Both `fetch-work` and `work-status` accept the same three flags to
control how many hierarchy levels to include, with this precedence (highest wins):
`--recursive`, then `--depth <N>`, then `--children`.

| Flag | Effect |
|---|---|
| `--children` / `--include-child` | Shorthand for `--depth 1` (direct children only). |
| `--depth <int>` | Descend exactly this many levels (1 = direct children, 2 = also grandchildren, etc). 0 or omitted (with no other depth flag) means the item itself only. |
| `--recursive` | Descend the entire child hierarchy (unbounded depth). |

**Traversal:** Pre-order depth-first — each parent is immediately followed by its own
descendants in the ordering used for rendering. Cycle-safe (track visited ids). A child
that fails to fetch **MUST NOT** abort the whole command — warn to stderr (a line such
as `warn: cannot fetch child work item <id>: <detail>`) and continue with the remaining
children.

### Exit code conventions

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Recoverable/expected failure (bad input, fetch/update/queue failed, missing PAT). |
| other | **MAY** be used for distinct categories if a command documents them (see per-command `exit_codes`). |

### Message discipline

All error and diagnostic/progress messages go to stderr; only the command's actual
result/confirmation goes to stdout, except where a command's own spec below says
otherwise (e.g. `--json` output always goes to stdout, and its progress lines move to
stderr to keep stdout parseable).

### Secrets

**MUST NOT** log, echo, or write the PAT anywhere.

### CLI conformance

- **MUST** provide `--help` for the program, each command group, and each subcommand.
- Unknown groups/subcommands/flags **MUST** fail with a non-zero exit and usage text.

---

## Command group: `board`

Sprints and work items.

### `board fetch-work`

```
ado board fetch-work <ref> [options]
```

Fetch one work item (Task, User Story, Bug, ...) from ADO, and optionally its
descendants, rendering each as a self-contained Markdown document.

**Arguments**

- **`ref`** (position 1, required, string) — See [Work item ref](#work-item-ref).

**Options**

- **`--org`** (string, default: parsed from ref, else `AZDO_ORG`)
- **`--project`** (string, default: parsed from ref, else `AZDO_PROJECT`)
- **`--out-dir`** (string, default: `reports`) — Directory to write Markdown file(s)
  into when no explicit `--out` is given, or always when `--split` is used. Created if
  missing.
- **`--out`** (string, default: null) — Explicit output file path for the single
  combined document. Overrides `--out-dir` auto-naming. Parent directories **MUST** be
  created if missing. Mutually exclusive with `--split`.
- **`--stdout`** (boolean, default: false) — Write the combined Markdown to standard
  output instead of a file. Ignores `--out`/`--out-dir`; nothing is written to disk.
  Mutually exclusive with `--split`.
- **`--children`** (alias `--include-child`, boolean, default: false) — See
  [Depth flags](#hierarchy).
- **`--depth`** (integer, default: null) — See [Depth flags](#hierarchy).
- **`--recursive`** (boolean, default: false) — See [Depth flags](#hierarchy).
- **`--split`** (boolean, default: false) — Write one file per work item into
  `--out-dir` instead of a single combined document (see
  [file naming](#file-naming)). **MUST** error if combined with `--stdout` or `--out`.

**Behavior**

1. Parse ref per [Work item ref](#work-item-ref); resolve org/project (flag over parsed
   over default); read the PAT ([Authentication](#authentication)).
2. Fetch the work item by id within the project, expanding ALL fields and relations
   (REST `$expand=all`, or the equivalent `expand="All"` library option).
3. Resolve the requested depth ([Depth flags](#hierarchy)). If depth is greater than 0,
   walk descendants pre-order up to that depth.
4. Build an id-to-(type, title) map covering every fetched item plus every parent/child
   id referenced by any fetched item, even ones outside the fetched set (e.g. the root's
   own parent, or children beyond the requested depth), fetching just `System.Title` and
   `System.WorkItemType` for those extras in batches of at most 200 ids, so the
   Hierarchy section can show readable labels for everything it links to.
5. Render each item to Markdown (see [Markdown contract](#markdown-contract)), flagging
   any parent/child reference whose id is also being rendered in this run as
   "(included in this file)".
6. Emit per `--split`/`--stdout`/`--out`/`--out-dir` (see [Output](#fetch-work-output)).

**Output** {#fetch-work-output}

**Markdown contract** {#markdown-contract}

The rendered Markdown for one work item **MUST** be a single, human-readable,
self-contained document. Field labels below are canonical; values come straight from
the work item. Empty fields **SHOULD** be omitted rather than shown blank.

Structure, top to bottom:
1. An H1 title line: `# [<Type> <id>] <Title>` (fallback title `Work Item <id>` if
   none).
2. A URL line giving the canonical work-item edit URL —
   `<org>/<url-encoded-project>/_workitems/edit/<id>`, prefixed with a bold "URL" label.
3. A 2-column Markdown metadata table (Field, Value) including, when present: ID, Type,
   State, Reason, Assigned To, Area Path, Iteration Path, Priority, Severity, Tags,
   Created (rendered as `"<date> by <author>"`), Changed (same shape).
4. A "Hierarchy" section, present only if the item has a parent and/or children — a bold
   "Parent" line linking to the parent, and/or a bold "Children (\<n\>)" line followed by
   one indented list item per direct child, same link shape. Every such link is tagged
   "(included in this file)" when that id is also rendered in the current document.
5. A Description section rendered from `System.Description`.
6. An Acceptance Criteria section rendered from
   `Microsoft.VSTS.Common.AcceptanceCriteria`.
7. A Repro Steps section rendered from `Microsoft.VSTS.TCM.ReproSteps` (Bugs).
8. A System Info section rendered from `Microsoft.VSTS.TCM.SystemInfo` (Bugs).
9. A Relations section listing every relation as a bullet showing the relation type and
   its URL, with an optional comment suffix taken from the relation's
   `attributes.comment`. Omit the whole section if there are no relations.

Rich text: see [Rich text](#rich-text). Identity fields: see
[Identity fields](#identity-fields).

**Multi-item document.** In combined (non-split) mode, when depth > 0 yields one or more
descendants, the output is a single document — the root rendered first per the Markdown
contract, then each descendant in the same pre-order traversal order, per the same
contract. Each item after the first **MUST** be preceded by a horizontal-rule separator.

**File naming** {#file-naming}

Type codes — short uppercase prefix for a work-item type, used in filenames:

| Type | Code |
|---|---|
| user story | `US` |
| task | `TASK` |
| epic | `EPIC` |
| feature | `FEATURE` |
| bug | `BUG` |
| any other/empty type | uppercase slug of the type name with hyphens removed (via [slugify](#slugify) then uppercase, strip hyphens), or `WORK` if that is empty |

- **Combined mode, explicit `--out`:** use it verbatim; create parent dirs.
- **Combined mode, auto:** write to `<out-dir>/<TYPE-CODE>-<id>-<slug>.md` for the root
  item's type, id and title ([slugify](#slugify) on the title), even when descendants
  are included in the same document.
- **Split mode:** one file per rendered item at
  `<out-dir>/<TYPE-CODE>-<id>-<slug>.md`, using that item's own type, id and title.
  Report a `Wrote <n> file(s) to <out-dir>/` line on success.

**Stdout mode:** writes the combined Markdown to stdout with no extra text (no success
message).

**Success message:** on a file write in combined mode, print `Wrote <path>` to stdout.

**Errors**

| Condition | Action |
|---|---|
| ref cannot be parsed | print `ERROR: cannot parse work item id/url: <ref>` to stderr; exit 1. |
| PAT not set | print guidance to set `AZDO_PAT` or `PAT` to stderr; exit non-zero. |
| work item fetch fails (not found, auth, network) | print `ERROR: cannot fetch work item <id>: <detail>` to stderr; exit 1. |
| `--split` combined with `--stdout` or `--out` | print `ERROR: --split cannot be combined with --stdout or --out` to stderr; exit 1. |

**Examples**

```sh
# Fetch by ID using configured defaults, write to ./reports.
ado board fetch-work 7102940

# Fetch by full URL (org/project parsed from it).
ado board fetch-work https://dev.azure.com/example-org/example-project/_workitems/edit/7102940

# Fetch a Feature together with its direct child User Stories.
ado board fetch-work 7102940 --children

# Fetch an Epic's entire descendant hierarchy, one file per item.
ado board fetch-work 7102940 --recursive --split --out-dir ./epic-7102940

# Stream to stdout.
ado board fetch-work 7102940 --stdout
```

---

### `board work-status`

```
ado board work-status <ref> [options]
```

Report merge (PR) and deployment status for a work item, and optionally its
descendants — which pull requests are linked, which are merged, and which
environments' latest successful release contains that work item's merge commit.

**Arguments**

- **`ref`** (position 1, required, string) — See [Work item ref](#work-item-ref).

**Options**

- **`--org`** (string, default: parsed from ref, else `AZDO_ORG`)
- **`--project`** (string, default: parsed from ref, else `AZDO_PROJECT`)
- **`--children`** (alias `--include-child`, boolean, default: false) — See
  [Depth flags](#hierarchy).
- **`--depth`** (integer, default: null) — See [Depth flags](#hierarchy).
- **`--recursive`** (boolean, default: false) — See [Depth flags](#hierarchy).
- **`--releases-top`** (integer, default: 5) — How many recent releases to inspect per
  configured pipeline.
- **`-v` / `--verbose`** (boolean, default: false) — Show the full linked-PR list and
  per-release detail. Default is a summary per repo/environment only.

**Behavior**

1. Parse ref; resolve org/project; read the PAT; fetch the root work item expanding all
   fields and relations.
2. Build one release "snapshot" once, not per work item — for each non-empty
   (repo, release-definition-name) pair configured via `AZDO_BACKEND_REPO` /
   `AZDO_BACKEND_RELEASE_DEF` and `AZDO_FRONTEND_REPO` / `AZDO_FRONTEND_RELEASE_DEF`,
   resolve the release definition by exact name, fetch its `--releases-top` most recent
   releases (environments and artifacts expanded), and resolve the repo's id (for
   ancestry checks). Record a human label ("Backend" / "Frontend") for each. If a
   repo/def pair is unset, skip it (not an error). Record any per-pair lookup error or
   warning without aborting the whole snapshot.
3. Recursively walk the root's linked work items (via Hierarchy-Forward, unbounded
   depth, cycle-safe) to collect every Pull Request reference attached to the root or
   any descendant, regardless of the `--depth`/`--recursive`/`--children` REPORTING
   flags — PR discovery always looks at the full subtree so merge state is not missed.
4. For each linked PR, fetch it and note its repo, status (active, completed or
   abandoned), source and target branch, and, if completed, its merge commit sha. A PR
   merged to the `dev` branch is specifically flagged as merged to dev.
5. For each configured repo/pipeline in the snapshot, walk its recent releases newest
   first and check whether that release's build-artifact source commit(s) are a
   git-ancestor of, or equal to, any of this work item's PR merge commits (cache
   ancestor checks per repo id, merge sha and commit tuple since the same pair recurs
   across releases and work items). Record, per environment, the most recent succeeded
   release and whether it includes this item.
6. Print a human-readable report per work item — a header (type, id, title, state,
   assignee, iteration, URL) for the root in full, or a one-line header for each
   reported descendant (indented by depth); in `--verbose` mode also the full linked-PR
   list and full per-release detail; always a summary section per configured pipeline
   (whether it merged to dev, yes or no, and the PR counts behind that verdict, or a
   "no PRs found" note if that pipeline's repo has no linked PRs), plus, for any repo
   that has linked PRs but no configured pipeline, a one-line "no release pipeline
   configured" summary; and per-environment deployment lines naming which environments
   contain this item, or, when no PR was matched to a successful deploy, the latest
   succeeded environments with a note to re-run with `--verbose` for the PR list.
7. If a reporting depth was requested (`--children`, `--depth` or `--recursive`), print
   each descendant's report, reusing the same snapshot, in pre-order at increasing
   indentation, preceded by a `Descendants (<n>, depth <= <d>)` header line.

**Idempotent:** yes. **Network:** required.

**Errors**

| Condition | Action |
|---|---|
| ref cannot be parsed | print `ERROR: cannot parse work item id/url: <ref>` to stderr; exit 1. |
| PAT not set | print guidance to set `AZDO_PAT` or `PAT` to stderr; exit non-zero. |
| work item fetch fails | print `ERROR: cannot fetch work item <id>: <detail>` to stderr; exit 1. |

**Examples**

```sh
# Status for a single work item.
ado board work-status 7102940

# Status for a Feature and each of its direct children, verbose.
ado board work-status 7102940 --children -v
```

---

### `board sprint`

```
ado board sprint [target] [options]
```

Report every User Story in a sprint (iteration) with its per-repo PR status and
per-environment deployment status, as a table, using repos and pipelines discovered
entirely from each story's own linked PRs — nothing is pre-configured per story.

**Arguments**

- **`target`** (position 1, optional, string, default: null) — Either a sprint URL, or
  a bare iteration path. When a sprint URL, every part parsed from it (org, project,
  team, iteration) overrides the corresponding flag. When neither a URL nor a bare
  iteration path can be determined, the flags/defaults below and the team's current
  iteration are used instead.

`target` URL shape:
```
https://dev.azure.com/<org>/<project>/_sprints/<view>/<team>/<iteration path>
```
(also the `*.visualstudio.com` legacy host). The `<view>` segment is one of `taskboard`,
`backlog`, `capacity`, `directory` or `analytics` and, if present, **MUST** be skipped
(it is a display mode, not the team name); it **MAY** be absent. Any query string is
ignored. Segments are URL-decoded; the iteration-path segments (everything after the
team) are rejoined with backslashes.

On a URL-shaped but unparseable target: exit non-zero with a message of the form
`ERROR: not a recognizable ADO sprint URL: <target>` plus a one-line hint of the
expected shape.

**Options**

- **`--project`** (string, default: `AZDO_PROJECT`)
- **`--org`** (string, default: `AZDO_ORG`)
- **`--team`** (string, default: null) — Team name, defaulting to the project name
  when unset. Used to resolve the current iteration and to scope results to the team's
  area path(s).
- **`--iteration`** (string, default: null) — Explicit iteration path, e.g.
  `"example-project\PI 2 Sprint 7"`. Skips current-iteration lookup.
- **`--all-teams`** (boolean, default: false) — Do not restrict to the team's area
  path(s) — report the whole project's stories for the iteration rather than just this
  team's, since an iteration is shared by every team planning into it.
- **`--recursive`** (boolean, default: false) — Also walk each story's child work items
  (Tasks, Bugs, ...) when collecting PRs for its repo/deployment columns. Most PRs hang
  off children; this costs extra API calls per story.
- **`-v` / `--verbose`** (boolean, default: false)
- **`--releases-top`** (integer, default: 10)
- **`--title-width`** (integer, default: 50) — Truncate story titles in the table to
  this many characters.

**Behavior**

1. Resolve org, project, team and iteration path per the argument rules above (a URL
   overrides flags where it supplies a value; a bare non-URL target is treated as an
   explicit iteration path).
2. If no iteration path is resolved yet, default the team to the project name if unset,
   then look up that team's current iteration; error if none exists.
3. Unless `--all-teams`, look up the team's area-path field value(s) and, if the team
   owns something narrower than the whole project, build a WIQL clause restricting to
   those area path(s) (an "under" match for values marked include-children, an exact
   match otherwise, OR-combined). If the team owns the project root, or the lookup
   fails, fall back to no restriction (whole project) — a lookup failure is a warning,
   not a fatal error.
4. Query all User Stories in that project and iteration path, plus the area clause if
   any, ordered by id; fetch id, title, state and assignee in batches of at most 200.
5. If there are no stories, print a no-stories message and exit 0.
6. For each story, in ascending id order — collect PR references from the story's own
   links, plus, with `--recursive`, its descendants up to a bounded depth, cycle-safe;
   fetch each PR and group by repository, tracking per repo the set of PR statuses seen
   (open, merged/completed, abandoned) and the merge commit(s) of completed PRs; for
   each distinct repo touched, discover build definitions that consume it, then release
   definitions whose artifact traces back to one of those builds, then that release
   definition's succeeded environments and the commit each was built from (cached per
   repo id across the whole sprint run, since many stories share repos). An environment
   counts as deployed for the story if its latest succeeded release's commit is a
   git-ancestor of, or equal to, one of the story's completed-PR merge commits.
7. Print, per story — id, state, truncated title, a "Repos / PRs" cell of
   repo-to-status-letters pairs (comma-separated, letters ordered open, merged,
   abandoned; a dash if no repos), and a "Deployed" cell (environment names if the story
   touches one repo; repo-to-envs pairs if it touches several; `no-pipeline` if it
   touches repos but none has any release definition; a dash otherwise), as an aligned
   plain-text table with a header and a separator row.
8. After the table, print a one-line summary of how many stories are done versus
   remaining (done meaning state Closed, Resolved or Done), the distinct repos and
   environments seen across the whole sprint, and a short legend explaining the status
   letters and the no-pipeline marker.

**Output**

- Headers: `ID`, `State`, `Title`, `Repos / PRs`, `Deployed`
- Status letters: `O` = open, `M` = merged (completed), `A` = abandoned
- Done states: Closed, Resolved, Done

**Idempotent:** yes. **Network:** required.

**Errors**

| Condition | Action |
|---|---|
| target looks like a URL but does not match the sprint-URL shape | print `ERROR: not a recognizable ADO sprint URL: <target>` to stderr; exit 1. |
| current-iteration lookup fails or returns none for the team | print an error with a hint to pass `--team`, `--iteration`, or a sprint URL, to stderr; exit 1. |

**Examples**

```sh
# Current sprint for the default project/team.
ado board sprint

# A specific iteration path, whole project.
ado board sprint "example-project\PI 2 Sprint 7" --all-teams

# From a taskboard URL, verbose, folding in child work items.
ado board sprint https://dev.azure.com/example-org/example-project/_sprints/taskboard/example-team/example-project/PI%202%20Sprint%207 --recursive -v
```

---

## Command group: `pipeline`

Builds, releases and deployments.

**Shared options.** Every pipeline subcommand accepts these three context options (see
[Pipeline context](#pipeline-context) for resolution order):

- **`--url`** (string, default: null)
- **`--org`** (string, default: `AZDO_ORG_URL`, else `AZDO_ORG`)
- **`--project`** (string, default: `AZDO_PROJECT`)

### `pipeline build`

```
ado pipeline build <pipeline> --branch <branch> [options]
```

Queue a build pipeline on a branch, optionally following it to completion.

**Arguments**

- **`pipeline`** (position 1, required, string) — Build-pipeline (definition) name,
  matched by exact name (case-insensitive) within the project.

**Options**

- **`--branch`** (string, default: null) — Branch to build, e.g. `dev` or
  `release/1.2`, or an explicit ref such as `refs/heads/...` or `refs/tags/...`. Bare
  names **MUST** be normalized by prefixing `refs/heads/`; values already starting with
  `refs/` are used as-is. Required unless `--list-vars`.
- **`--pool`** (string, default: null) — Agent pool/queue name to override the
  pipeline's default, resolved by exact name.
- **`--var`** (`KEY=VALUE`, repeatable) — Queue-time variable override, e.g.
  `--var RunSonarQubeStep=true`.
- **`--list-vars`** (boolean, default: false) — List the pipeline's declared variables
  (name, current value — masked if the variable is secret — and whether it is settable
  at queue time) and exit, without queuing anything.
- **`--watch`** (boolean, default: false) — Follow the build until it reaches a
  terminal state (see [build_watch](#build-watch)).
- **`--json`** (boolean, default: false) — Emit the result as JSON on stdout (see
  [json_shape](#pipeline-build-output)). All progress/queueing lines that would
  otherwise go to stdout move to stderr so stdout stays pure JSON.
- **`--poll`** (integer, default: 5) — Seconds between polls when watching.

**Behavior**

1. Resolve context; require `--branch` unless `--list-vars`; parse the `--var` pairs as
   `KEY=VALUE`, erroring on any entry without an equals sign.
2. Resolve the build definition by exact name (case-insensitive) within the project;
   error listing candidates if zero or multiple non-exact matches exist, or hint that a
   definition-id disambiguation is needed if several exact-name matches exist across
   different folder paths.
3. If `--pool` is given, resolve an agent queue by exact name the same way.
4. If `--list-vars`, print each declared variable and exit 0 without queuing a build.
5. If `--var` is given, verify every requested variable is declared on the definition
   and marked overridable at queue time; error, naming the settable variables,
   otherwise — ADO would silently drop an unrecognized or non-overridable variable
   rather than erroring, so this check **MUST** happen client-side.
6. Normalize the branch; queue the build with the resolved definition, branch, optional
   queue override, and variables (serialized however the target ADO client expects,
   e.g. as a JSON parameters payload).
7. Print the queued build id, its (possibly still-pending) build number, and its web
   URL.
8. If `--watch`, poll the build (see [build_watch](#build-watch)) until terminal; an
   interrupt during watch **MUST NOT** cancel the build — print a note that it
   continues running and exit 0.
9. Else if `--json`, the build number is usually assigned a moment after queueing, so
   poll, up to a bounded timeout such as 60 seconds, until it appears before emitting
   JSON.
10. Exit 0 on success; if `--watch` was used, exit 0 only if the build's final result is
    succeeded, else 1.

**`build_watch`** {#build-watch}

Shared polling behavior for following a build to completion, used by `build --watch`
and by `watch` on a build target.

- **Poll interval default:** 5s.
- **Behavior:** Poll the build and its timeline every N seconds. For each Stage, Job or
  Task timeline record whose id and state/result pair has not been printed before,
  print one line — `[running] <name>` while in progress, or `[<result>] <name>` once
  completed, indented by record type (Stage none, Job two spaces, Task four spaces).
  Stop when the build's own status is completed; print a final line naming the build id
  and its result.

**Output** {#pipeline-build-output}

`--json` shape:

```
build_id: integer
version:  string or null  (build number)
branch:   string          (short form, refs/heads/ stripped)
definition: string
status:   string
result:   string or null
url:      string
```

**Errors**

| Condition | Action |
|---|---|
| `--branch` missing and `--list-vars` not set | print `ERROR: --branch is required` to stderr; exit 1. |
| invalid `--var` syntax | print an ERROR naming the bad `KEY=VALUE` pair to stderr; exit 1. |
| pipeline name not found or ambiguous | print an ERROR naming the lookup failure and any candidates to stderr; exit 1. |
| `--pool` name not found or ambiguous | print a similarly worded lookup ERROR to stderr; exit 1. |
| a requested `--var` is not declared, or not overridable | print an ERROR naming the variable and, when relevant, which variables are settable, to stderr; exit 1. |
| queueing fails (permissions, invalid branch, network) | print an ERROR wrapping the underlying detail to stderr; exit 1. |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Success (queued, and if watched, the build succeeded). |
| 1 | Bad input, resolution failure, or (with `--watch`) the build did not succeed. |

**Examples**

```sh
# Queue a build on dev and wait for it.
ado pipeline build Nexus-FrontEnd-CI --branch dev --watch

# List a pipeline's variables.
ado pipeline build Nexus-FrontEnd-CI --list-vars

# Queue with a variable override, machine-readable output.
ado pipeline build Nexus-FrontEnd-CI --branch release/1.2 --var RunSonarQubeStep=true --json
```

---

### `pipeline release`

```
ado pipeline release <pipeline> [options]
```

Create a release from a release pipeline, pinning each of its build artifacts to a
concrete build.

**Arguments**

- **`pipeline`** (position 1, required, string) — Release-pipeline (release
  definition) name, exact match.

**Options**

- **`--version`** (string, default: null) — Artifact build number to release. Default
  is the newest successful build of each artifact's source build definition (honoring
  `--branch` if given).
- **`--branch`** (string, default: null) — Only consider artifact builds from this
  branch when auto-picking a version (ignored when `--version` is given).
- **`--description`** (string, default: null)
- **`--var`** (`KEY=VALUE`, repeatable) — Release-variable override.
- **`--manual`** (boolean, default: false) — Hold every environment/stage for manual
  deployment (use `pipeline deploy` afterward) instead of letting configured triggers
  fire automatically.
- **`--draft`** (boolean, default: false) — Create the release as a draft (does not
  start deploying).
- **`--json`** (boolean, default: false)

**Behavior**

1. Resolve context; parse `--var`.
2. Resolve the release definition by exact name (with artifacts expanded); error with
   near-match suggestions on zero or ambiguous matches.
3. For each build-type artifact on the definition, skipping any non-build artifact type
   with a stderr warning, pin it to a concrete build — if `--version`, look up that
   exact build number on the artifact's source build definition, erroring if not found;
   otherwise take the newest build with status completed and result succeeded on that
   source build definition, optionally filtered to `--branch`. If neither yields a
   build, fall back to reusing the same artifact version as the release definition's
   most recent previous release, if one exists, warning on stderr that a fallback is
   being used; error only if there is no previous release to fall back to either. Error
   if the artifact has no linked build definition, or if the definition has no build
   artifacts at all.
4. Print each resolved artifact's alias, version and branch as progress.
5. If `--manual`, additionally include every stage name from the definition as manual
   environments, i.e. do not let a stage auto-start.
6. Create the release with the resolved artifact versions, description, draft flag,
   manual-environments list, and variables, with reason set to manual.
7. Print or emit the created release's id, name, primary artifact's version and branch,
   full per-artifact list, and web URL — as text, or as one JSON object (see
   [json_shape](#pipeline-release-output)) on `--json`, with progress moved to stderr.

**Output** {#pipeline-release-output}

`--json` shape:

```
release_id: integer
name:       string
version:    string or null
branch:     string or null
artifacts:  [{alias: string, build_id: string, version: string or null, branch: string or null}]
url:        string
```

**Errors**

| Condition | Action |
|---|---|
| invalid `--var` syntax | print an ERROR naming the bad pair to stderr; exit 1. |
| release-pipeline name not found or ambiguous | print an ERROR with lookup detail to stderr; exit 1. |
| an artifact cannot be resolved to a build and there is no previous release to fall back to | print a descriptive ERROR to stderr; exit 1. |
| release creation fails | print an ERROR wrapping the underlying detail to stderr; exit 1. |

**Examples**

```sh
# Release the newest successful build.
ado pipeline release EXAMPLE-API-CD

# Release a specific build number, held for manual deploy.
ado pipeline release EXAMPLE-API-CD --version 20240115.3 --manual
```

---

### `pipeline deploy`

```
ado pipeline deploy [pipeline] --env <env> [options]
```

Start (or watch) deployment of an existing release to one environment/stage.

**Arguments**

- **`pipeline`** (position 1, optional, string, default: null) — Release-pipeline
  name; its most recent release is targeted unless `--release` is given. Either this or
  `--release` is required.

**Options**

- **`--env`** (string, **required**) — Environment/stage name (case-insensitive exact
  match), e.g. `DEV` or `QA`.
- **`--release`** (string, optional, default: null) — Release id to deploy (default:
  the pipeline's latest release).
- **`--comment`** (string, optional, default: null)
- **`--var`** (`KEY=VALUE`, repeatable, optional) — Stage variable override.
- **`--watch`** (boolean, optional, default: false)
- **`--poll`** (integer, optional, default: 5)

**Behavior**

1. Resolve context; require a pipeline name or `--release`; parse `--var`.
2. Load the target release — by id if `--release` is given, otherwise the pipeline's
   most recent release via exact-name resolution — with environments expanded.
3. Resolve the named stage on that release, case-insensitively; error listing available
   stage names if not found.
4. Start the deployment by updating that environment's status to in-progress with the
   given comment and variables.
5. Print the release name and id, target stage, prior status, then the resulting queued
   status and the release's web URL.
6. If `--watch`, poll the stage (see [deploy_watch](#deploy-watch)) until terminal; an
   interrupt does not cancel the deployment.
7. Exit 0, or, if watched, 0 only when the stage's final status is succeeded.

**`deploy_watch`** {#deploy-watch}

Shared polling behavior for following a release-stage deployment, used by
`deploy --watch` and by `watch` on a release target.

- **Poll interval default:** 5s.
- **Behavior:** Poll the release every N seconds. On the first poll where the stage has
  one or more pending pre-deploy approvals, print one line naming the pending
  approver(s), only once. For each deploy task in the stage's most recent deployment
  attempt whose id and status have not been printed before, print one line naming the
  phase and task, tagged running while in progress or with its terminal status once it
  reaches one (succeeded, failed, skipped, or partially succeeded). Stop when the
  stage's own status reaches a terminal value (succeeded, partially succeeded,
  rejected, or canceled); print a final line naming the stage and its finishing status.

**Errors**

| Condition | Action |
|---|---|
| neither pipeline nor `--release` given | print `ERROR: provide a release pipeline name or --release <id>` to stderr; exit 1. |
| invalid `--var` syntax | print an ERROR naming the bad pair to stderr; exit 1. |
| no releases exist for the resolved pipeline | print an ERROR naming the pipeline to stderr; exit 1. |
| named stage not found on the release | print an ERROR listing the available stage names to stderr; exit 1. |
| starting the deployment fails | print an ERROR wrapping the underlying detail to stderr; exit 1. |

**Examples**

```sh
# Deploy the latest release of a pipeline to QA.
ado pipeline deploy EXAMPLE-API-CD --env QA --watch

# Deploy a specific release id.
ado pipeline deploy --release 4821 --env DEV --comment 'hotfix verification'
```

---

### `pipeline watch`

```
ado pipeline watch <target> [options]
```

Follow an already-running (or already-finished) build or release deployment by id or by
pasting an ADO results URL.

**Arguments**

- **`target`** (position 1, required, string) — A bare numeric id, or an ADO URL
  containing a `buildId` query parameter (a build results URL) or a `releaseId`
  parameter (optionally also `environmentId`, a release/deployment URL).

**Target parsing**
- All-digits target — a bare id with kind left unresolved (see `--kind`).
- Otherwise the URL **MUST** be parsed for the query parameters `buildId`, `releaseId`
  and `environmentId`; whichever of `buildId` or `releaseId` is present determines the
  kind and id. If the URL also encodes org/project (see
  [Pipeline context](#pipeline-context)), use it as the default `--url` for context
  resolution unless `--url` was explicitly given.
- If neither `buildId` nor `releaseId` is present in a URL-shaped target, error with a
  message naming the target.

**Options**

- **`--kind`** (enum: `build` \| `release`, default: null) — Disambiguate a bare
  numeric id. Default is whatever the target URL implied; if that is unknown, release
  when `--env` is also given, else build.
- **`--env`** (string, default: null) — Stage to follow on a release. Only valid when
  watching a release (error if given while watching a build). Default is the stage
  named by an `environmentId` parsed from the target URL, else whichever stage is
  currently in progress, queued or scheduled, else the most recently started (highest
  rank among non-not-started) stage; error, listing stage names, if none has started
  and `--env` was not given.
- **`--poll`** (integer, default: 5)

**Behavior**

1. Parse the target; resolve context, using a URL-derived org/project when present and
   `--url` was not given; resolve kind.
2. For kind **build** — fetch the build; print its id, definition name, build number,
   and short branch, plus its web URL. If already completed, print the final result and
   exit, 0 if succeeded else 1, without polling. Otherwise poll per
   [build_watch](#build-watch) until terminal, then exit accordingly.
3. For kind **release** — fetch the release with environments expanded; resolve the
   target stage per the `--env` rule above; print the release name and id, stage name,
   and current status, plus its web URL. If the stage's status is already terminal,
   print that and exit, 0 if succeeded else 1, without polling. Otherwise poll per
   [deploy_watch](#deploy-watch) until terminal, then exit accordingly.
4. An interrupt while polling **MUST NOT** cancel the underlying build/deployment; print
   a note that it continues and exit 0.

**Errors**

| Condition | Action |
|---|---|
| target is neither all-digits nor a URL with a scheme | print an ERROR naming the bad target to stderr; exit 1. |
| URL-shaped target has neither `buildId` nor `releaseId` | print an ERROR naming the target to stderr; exit 1. |
| `--env` given while kind is build | print `ERROR: --env only applies when watching a release` to stderr; exit 1. |
| build/release fetch fails | print an ERROR wrapping the underlying detail to stderr; exit 1. |
| named or derived stage not found, or no stage has started and `--env` absent | print a descriptive lookup ERROR to stderr; exit 1. |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Terminal state reached (or already reached) with a succeeded result, or the user interrupted watching. |
| 1 | Terminal state reached with a non-succeeded result, or a resolution/fetch error. |

**Examples**

```sh
# Watch a build by id.
ado pipeline watch 88213

# Watch a specific stage of a release by pasted URL.
ado pipeline watch 'https://dev.azure.com/example-org/example-project/_release?releaseId=4821&environmentId=3'

# Watch a release, letting the tool pick the active stage.
ado pipeline watch 4821 --kind release
```

---

## Extension points

This section is intentionally open. Each repository/team that adopts the CLI can add
incremental or custom behavior **here** without altering the core contract above. An
implementation **MUST** treat everything below as additive: absence of any extension
**MUST NOT** change core behavior.

**Philosophy.** The core spec defines the smallest stable contract. Customizations
layer on top via the hooks below. Downstream specs **SHOULD** copy this file and append
to extensions rather than editing the commands contracts, so they can pull in upstream
updates cleanly.

**Versioning.** Downstream files **SHOULD** record an `extends: ado.spec.md@<version>`
field so a tool/agent can detect when the base spec moved ahead of a customization.

### Hooks

#### `field_map`

Applies to: `board fetch-work`, `board work-status`.

Override or extend which ADO fields map to which Markdown labels or sections, or which
environment-variable pairs feed `board sprint` / `board work-status` deployment
discovery.

```yaml
add_metadata_rows:
  - { label: "Story Points", field: "Microsoft.VSTS.Scheduling.StoryPoints" }
add_sections:
  - { heading: "Custom Notes", field: "Custom.Notes", rich_text: true }
hide_rows: ["Severity"]
release_pipelines:
  - { label: "Payments", repo_env: "AZDO_PAYMENTS_REPO", release_def_env: "AZDO_PAYMENTS_RELEASE_DEF" }
```

#### `defaults_profile`

Applies to: `*`.

Named profiles bundling org, project and team so users can switch contexts, e.g.
`ado board fetch-work 123 --profile example`.

```yaml
profiles:
  example:
    org: "https://dev.azure.com/example-org"
    project: "example-project"
    team: "example-team"
```

#### `naming_scheme`

Applies to: `board fetch-work`.

Customize auto file naming and output directory layout.

```yaml
pattern: "{type-code}-{id}-{slug}.md"   # tokens: {id} {type} {type-code} {slug} {project} {state}
subdir_by: null                          # e.g. "type" -> reports/User Story/...
```

### Future commands

New commands/subcommands **MUST** follow the same conformance rules (help, exit codes,
stderr/stdout discipline, PAT handling) and **SHOULD** reuse the org/project resolution
and URL-parsing concepts defined above.

**Candidates**

- `repo` group — out of scope for this spec version; see [Out of scope](#ado-cli-specification).
- `testing` group — out of scope for this spec version; see [Out of scope](#ado-cli-specification).
- `board update <ref> [field-options]` — update fields of a work item and/or add a
  discussion comment. Not yet specified here.
- `board create --type <type> --title <title> [--field k=v ...]` — not yet specified
  here.
