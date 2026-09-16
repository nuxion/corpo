You have to review code and test cases pulled from Azure Devops Platform - ADO - to find gaps between:
- the User Stories
- the code 
- and test cases created. 

The user will share with you the group of test suite to validate. 

Plan your work in small units, per test suite first and the per test case, cross check with user story to which the test case belongs. 

## Where is the code

The applications is composed of a backend and frontend. The code lives in this two folders, both are symlinks from the real code: 

- frontend/
- backend/

Think on this as snapshots from real repositories.

## Where are the test cases

You have two ways of checking them:
1. Looking at `test_cases_plan_1001.json`
2. Checking in `tests/` folder. Don't look at `tests2`

## Where are the user Stories
You have to pull the user story by id using the following command: 
```
uv run ado show-story
usage: fetch_test_plan show-story [-h] [--json JSON] story_id
```
Check `README.md` if you need more information about the command. 

Download each user story at `stories`. Create the folder if doesn't exist. 

## Output 

Your Output should be a document with the name and id of the folder. 

Example of what the user will share with you:

```
[F] 2002  ExampleArea  (staticTestSuite)
├── [S] 2003  3001 : ExampleArea Reviews Request Details  (requirementTestSuite)
```

The title of report here will be `2002-ExampleArea.md`

The report file, it should a section for each User Story and it should contain the following information
<Report>
- Title
- ID 
- Summary
---
For each <User Story>
- User Story ID and Title. H2. 
- Metadata. H3: 
    - User Story url
    - Tested by 
    - Folder (if exist) 
    - Test Suite 
- Overview. H3: 
    - What it is about? 
- Risks. H3: 
   - Acceptance Criteria no tested from the User story
   - behaviour present in code that isn't covered by any test case for that User Story. Clarification: Based on the User Story description, and endpoints/components identified in the source code. 
- A table showing what is covered from the test cases (with the test case ID) and what it's not. 
    - AC ID | AC summary | Covered? | Test Case IDs | Notes
    - Possible values in Covered: Yes / Partial / No: Partial is common when a test case touches an AC but doesn't exercise all branches
- Potencial Bugs. H3:
    - List here any possible bug that you discover in the process, related to the user story. 
</User Story>
</Report>

Write the output in `reports/`. If doesn't exist, create it. 

## Guidelines

- **Covered?** values: `Yes` / `Partial` / `No`. Use `Partial` when a test case touches an AC but doesn't exercise all branches.
- **Risks — code gaps**: scope to behavior derivable from the User Story description and the endpoints/components identified in the source code. Don't list every conditional in the code; focus on behavior the User Story implies but no test case asserts.
- **User Stories**: download with the `--json` flag to `stories/` (create the folder if it doesn't exist).
- **Test case source**: `test_cases_plan_1001.json` is the primary source of truth. `tests/` may be consulted for additional detail. If a test case is referenced but cannot be located in either, flag it in the report as `MISSING INFORMATION`.
- **Tested by**: refers to the author of the test case.
- **Report filename**: one report per `[F]` folder (e.g. `2002-ExampleArea.md`); each child User Story is an H2 section inside that single report.

