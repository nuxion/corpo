# Plan: Fetch Test Definitions for Test Plan 1001

## Goal
Retrieve all test case definitions belonging to Azure DevOps Test Plan
`planId=1001` (organization `example-org`, project `example-project`) using the
already-installed `azure-devops` Python SDK (`>=7.1.0b4`).

Source URL the user is working from:
`https://dev.azure.com/example-org/example-project/_testPlans/execute?planId=1001&suiteId=2001`

## Inputs
- `organization_url`: `https://dev.azure.com/example-org`
- `project`: `example-project`
- `plan_id`: `1001`
- Auth: Personal Access Token (PAT) read from env var `AZDO_PAT`
  (scopes needed: `Test Management (Read)` and `Work Items (Read)`).

## Approach (azure-devops SDK)
The SDK exposes the Test Plan domain via `TestPlanClient`
(`azure.devops.v7_1.test_plan.test_plan_client.TestPlanClient`). Steps:

1. **Connect**
   - `msrest.authentication.BasicAuthentication('', PAT)`
   - `azure.devops.connection.Connection(base_url=org_url, creds=...)`
   - `client = connection.clients_v7_1.get_test_plan_client()`

2. **Enumerate suites under the plan**
   - `client.get_test_suites_for_plan(project, plan_id)`
   - Paginate via `continuation_token` if returned.

3. **For each suite, enumerate test cases**
   - `client.get_test_case_list(project, plan_id, suite_id)`
   - Each entry exposes `work_item` (id, name, work_item_fields) — that
     is the "test case definition".
   - Paginate per suite using `continuation_token`.

4. **Normalize / deduplicate**
   - A test case can live in multiple suites. Keep a `dict[int, TestCase]`
     keyed by work item id; record which suites reference it.

5. **Output**
   - Print a summary count.
   - Dump full list to `test_cases_plan_1001.json` with fields:
     `id`, `title`, `state`, `priority`, `area_path`, `tags`, `suite_ids`.
   - Additionally, write one Markdown file per test case at
     `tests/plan-<planId>/<id>-<title>.md` (e.g.
     `tests/plan-1001/12345-login-with-valid-user.md`).
     - `<title>` is slugified: lowercase, non-alphanumerics → `-`,
       collapsed/trimmed, truncated to ~80 chars to stay filesystem-safe.
     - Directory is created if missing; existing files are overwritten
       so re-runs stay idempotent.
     - File contents:
       - YAML frontmatter with `id`, `title`, `state`, `priority`,
         `area_path`, `tags`, `suite_ids`, `work_item_url`.
       - `# <title>` heading.
       - Sections: `## Metadata`, `## Steps` (rendered from
         `Microsoft.VSTS.TCM.Steps` if fetched — otherwise a
         `_Steps not fetched_` placeholder), `## Suites` (list of
         suite ids/names the case belongs to).

## Deliverables
- `ado_actions/ado_actions/fetch_test_plan.py` — script with a
  `main()` and CLI args `--plan-id`, `--project`, `--org`, `--out-json`,
  `--md-dir` (default `tests/plan-<planId>`).
- Generated artifacts:
  - `test_cases_plan_1001.json`
  - `tests/plan-1001/<id>-<slug>.md` (one per unique test case)
- Run with: `uv run python -m ado_actions.fetch_test_plan --plan-id 1001`

## Open questions / verify before coding
- Confirm `clients_v7_1.get_test_plan_client` is the correct accessor
  in the installed SDK version (fallback: `clients.get_test_plan_client`).
- Confirm PAT env var name with user (default `AZDO_PAT`).
- Decide whether test step details (action/expected) are needed; if so,
  fetch each work item via `WorkItemTrackingClient.get_work_item` with
  `fields=['Microsoft.VSTS.TCM.Steps']` since the test-plan API does
  not return steps by default.
