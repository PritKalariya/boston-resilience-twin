# Contributing

## Workflow
- Every task is a GitHub issue with one assignee, a due date, a reviewer, and acceptance criteria.
- One issue per branch and PR. Branch name: `<issue-number>-<short-slug>`.
- The PR body includes `Closes #<n>`, or `Part of #<n> (k of m)` when an issue is split across PRs; only the last PR closes the issue.
- The PR body follows `.github/pull_request_template.md`, every section filled, nothing deleted.
- Direct pushes to main follow the exception under Merging.

## Issues
- Work comes in pairs. Part 1: code and tests, plus the ADR if the work makes a decision. Part 2: a sub-issue for its README and runbook section, started after part 1 merges.
- An issue that depends on someone else's work names the fixture or interface it builds against, so it can start on day one.
- The author ticks each acceptance criterion, in the PR body and on the issue, as it is met, with its evidence.
- A criterion changed while a PR is open also gets a comment on the PR tagging the author. No criterion changes after the PR is approved.
- ADR numbers: the next free number in docs/decisions/ at merge time. Rename the file if someone merged that number first.
- Status lives in GitHub: the issue's state and its PRs. There is no status file.

## Reviews
- The reviewer is named in the issue (`Reviewer:` line). The author requests them on the PR.
- Respond within 24 hours of the request. No response in 24 hours: the author pings the group, and anyone can take over the review (rule 5 below).
- Work that arrives after its due date without notice beforehand: the reviewer may hand it to someone else (rule 5).
- The reviewer runs the code: CI is green, and the issue's acceptance commands are run locally or their output is pasted in the PR.
- Approve, or request changes pointing at specific lines. No approval without running it.
- Nobody approves their own PR.

Review rules:
1. One required reviewer: the issue's Reviewer line. Other reviewers are optional and say what they checked.
2. Every finding is a requested change, fixed in the same PR, however small. There are no nits, and nothing is left for a later PR. A finding outside the PR's issue goes to the issue it belongs to.
3. The first review lists every finding. Later rounds check the fixes, any new code, and anything the fixes broke.
4. An open "Request changes" blocks the merge, even if someone else approved. Only the reviewer who raised it approves or dismisses it.
5. After pushing fixes, the author replies on each thread with the fixing commit and clicks Re-request review for everyone who requested changes.
6. Anyone can hand a review to someone else: leave a PR comment tagging the new reviewer and request their review. The new reviewer inherits the open threads.
7. Approve only when no finding is open and every acceptance criterion the PR claims is met.

## Merging
- The required reviewer merges (squash) after approving, once no change request is open, every conversation is resolved and CI is green. If a second reviewer is listed or has commented, wait for their review first.
- Repo owner may push directly to main for docs, templates and process files only (.github/ templates, docs/CONTRIBUTING.md, CLAUDE.md, README wording). Code, tests, CI, Terraform, DAG and pipeline changes always go through a PR.

## What every PR shows
- Evidence for each acceptance box: the command and its output.
- Tests for new code.
- No secrets, keys, or data files. Data goes through DVC.
- The ADR, if the issue has the ADR box.
- For Terraform changes, the `terraform plan` output.
- Docs that cite run ids or dates, never counts that change with every run.

### Changing an accepted ADR
- In place: only the status line, and dated notes (`Note (YYYY-MM-DD): ...`) that correct a fact, clarify, or point elsewhere, changing no decision and adding no duty. The original text stays.
- Anything else (a changed decision, or a new or changed duty or consequence) is a new ADR that amends or supersedes it, and the old ADR's status line points to it.
- Applies from 2026-11-03; earlier in-place edits stand.
