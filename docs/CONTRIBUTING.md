# Contributing

## Workflow
- Every task is a GitHub issue with one assignee, a due date, a reviewer, and acceptance criteria.
- One issue per branch and PR. Branch name: `<issue-number>-<short-slug>`.
- The PR body includes `Closes #<n>`.
- Direct pushes to main follow the exception under Merging.

## Issues
- Work comes in pairs. Part 1: code and tests, plus the ADR if the work makes a decision. Part 2: a sub-issue for its README and runbook section, started after part 1 merges.
- ADR numbers: the next free number in docs/decisions/ at merge time. Rename the file if someone merged that number first.
- Status lives in GitHub: the issue's state and its PRs. There is no status file.

## Reviews
- The reviewer is named in the issue (`Reviewer:` line). The author requests them on the PR.
- Respond within 24 hours of the request. No response in 24 hours: the author pings the group, and anyone can take over the review (rule 5 below).
- The reviewer runs the code: CI is green, and the issue's acceptance commands are run locally or their output is pasted in the PR.
- Approve, or request changes pointing at specific lines. No approval without running it.
- Nobody approves their own PR.

Review rules:
1. One required reviewer: the issue's Reviewer line. Other reviewers are optional and say what they checked.
2. A blocking finding is always "Request changes", never a comment. Non-blocking findings start with `nit:`.
3. An open "Request changes" blocks the merge, even if someone else approved. Only the reviewer who raised it approves or dismisses it.
4. After pushing fixes, the author replies on each thread with the fixing commit and clicks Re-request review for everyone who requested changes.
5. Anyone can hand a review to someone else: leave a PR comment tagging the new reviewer and request their review. The new reviewer inherits the open threads.

## Merging
- The author merges, once the required reviewer has approved the latest push, no change request is open, every conversation is resolved and CI is green.
- Repo owner may push directly to main for docs, templates and process files only (.github/ templates, docs/CONTRIBUTING.md, CLAUDE.md, README wording). Code, tests, CI, Terraform, DAG and pipeline changes always go through a PR.

## What every PR shows
- Evidence for each acceptance box: the command and its output.
- Tests for new code.
- No secrets, keys, or data files. Data goes through DVC.
- The ADR, if the issue has the ADR box.
- For Terraform changes, the `terraform plan` output.
