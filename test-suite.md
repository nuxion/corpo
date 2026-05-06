# Azure DevOps Test Plan — IDs & Entities

A short reference for how IDs and entities relate when working with Azure
DevOps Test Plans (specifically what the `fetch_test_plan` tool produces).

## The two domains

Test artifacts in ADO live across two domains that are linked but
queried with different APIs:

1. **Test Plan domain** — plans, suites, test points, configurations, runs.
2. **Work Item domain** — every test case is *also* a work item, and is
   linked to user stories / requirements via work item relations.

Confusing the two is the root of most "where does this ID come from?"
questions.

## Hierarchy (Test Plan domain)

```
Test Plan          (id: 1001)
└── Test Suite     (id: 2001, 2002, …)   ← a plan has many suites
    └── Test Point (suite_id × test_case × configuration)
        └── refs → Test Case (id: 12877, 12878, …)
```

- **Test Plan** — top-level container with name, iteration, owner. It
  does **not** own test cases directly.
- **Test Suite** — the container that actually holds test cases. Three
  flavours:
  - *Static* — manually picked cases.
  - *Requirement-based* — auto-bound to a User Story; every test case
    linked to that story via `Tested By` shows up here. Suites named
    like `"11336 : Export Requests to Excel"` are this kind — the
    leading number is the bound User Story's ID.
  - *Query-based* — driven by a WIQL query.
- **Test Point** — the (suite, case, configuration) tuple that holds
  execution state (Pass/Fail/Blocked). Adding a case to another suite
  creates another point, **not** another case.
- **Test Case** — a Work Item with its own ID. The same case can appear
  in many suites and many plans without being copied.

## Hierarchy (Work Item domain)

```
User Story (id: 11336)
   │
   │  Microsoft.VSTS.Common.TestedBy   (forward)
   ▼
Test Case (id: 12877, 12878, …)
   │
   │  Microsoft.VSTS.Common.TestedBy-Reverse
   ▲
User Story (id: 11336)
```

- The authoritative link from a test case to the story it verifies is
  the **`Microsoft.VSTS.Common.TestedBy`** work item relation.
- `…-Forward` lives on the User Story (points at the case).
- `…-Reverse` lives on the Test Case (points at the story).
- The `fetch_test_plan` tool reads the *reverse* link from each test
  case to determine the parent story — that is what populates
  `parent_id` / `parent_title` in the JSON, and the `story-<id>/`
  folder layout under `tests2/plan-<planId>/`.

> Title prefixes like `11336- Verify Export button…` are a **team
> convention** for human readability. They are not parsed by ADO. Do
> not rely on titles to determine the parent story.

## The IDs you will encounter

| ID | Entity | Where it comes from |
|----|--------|---------------------|
| `1001` | **Test Plan** | `planId` in the URL `/_testPlans/execute?planId=1001` |
| `2001`, `2002`, … | **Test Suite** | `suiteId` in the URL; one plan has many |
| `12877`–`12886`, … | **Test Case** (work item) | First number in the markdown filename `<id>-<slug>.md` |
| `11336` | **User Story** (work item) | Suite name prefix *and* the `TestedBy` relation target |

A test case ID is unique across the whole project (it's a work item
ID). A suite ID is unique within the plan tree. A test point ID is
unique within the suite.

## How the tool maps these to disk

For `--md-dir tests2/plan-1001 --group-by-story --include-story`:

```
tests2/plan-1001/
├── story-index.md                              ← all parent stories
├── story-11336/
│   ├── story-11336-export-requests-to-excel-model-support.md   ← story def
│   ├── 12877-<slug>.md                         ← each test case
│   ├── 12878-<slug>.md
│   └── …
├── story-3001/
│   └── …
└── story-none/                                 ← cases without TestedBy
    └── …
```

- The folder is keyed by **User Story ID** (parent), discovered via
  the `TestedBy` reverse relation, *not* the suite id.
- The same test case can be referenced by multiple suites; it appears
  **once** on disk (deduplicated by work item id), with all suite ids
  recorded in the case's frontmatter and `## Suites` section.
- `story-none/` collects test cases that have no `TestedBy` link — for
  example, ad-hoc cases added directly to a static suite.

## Quick reference — finding the X for a given Y

| You have | You want | How |
|----------|----------|-----|
| Plan ID | All suites | `TestPlanClient.get_test_suites_for_plan` |
| Suite ID | Test cases in it | `TestPlanClient.get_test_case_list` |
| Test case ID | Parent story | Work item relation `Microsoft.VSTS.Common.TestedBy-Reverse` |
| Story ID | All test cases | Story's `Microsoft.VSTS.Common.TestedBy` (forward) relations |
| Story ID | Suite (if requirement-based) | Suite is named `"<storyId> : <storyTitle>"` and has `requirementId == storyId` |
