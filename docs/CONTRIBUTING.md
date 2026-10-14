# Contributing

## Workflow
- Every task is a GitHub issue with one assignee, a due date, a reviewer, and acceptance criteria.
- One issue per branch and PR. Branch name: `<issue-number>-<short-slug>`.
- The PR body includes `Closes #<n>`.
- No direct pushes to main.

## Reviews
- The reviewer is named in the issue (`Reviewer:` line). The author requests them on the PR.
- Respond within 24 hours of the request. No response in 24 hours: the author pings the group. After 48 hours, any teammate may approve.
- The reviewer runs the code: CI is green, and the issue's acceptance commands are run locally or their output is pasted in the PR.
- Approve, or request changes pointing at specific lines. No approval without running it.
- Nobody approves their own PR.

## What every PR shows
- Evidence for each acceptance box: the command and its output.
- Tests for new code.
- No secrets, keys, or data files. Data goes through DVC.
- The ADR, if the issue has the ADR box.
- For Terraform changes, the `terraform plan` output.
