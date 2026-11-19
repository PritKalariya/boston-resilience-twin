Closes #
<!-- For an issue split across PRs, use "Part of #<n> (k of m)"; only the last PR says Closes. -->

Assignee: one person, set in the sidebar
Reviewer: the person named in the issue, requested in the sidebar
Due: YYYY-MM-DD
Phase: copy the phase label from the issue

## Acceptance criteria
Copy from the issue. Tick what this PR meets, here and on the issue.
- [ ]

## How I verified
Commands and output for each criterion:

## Checklist
- [ ] CI is green
- [ ] PR body follows this template: every section filled, nothing deleted
- [ ] Tests added or updated for new code
- [ ] ADR included if the issue has the ADR checkbox
- [ ] Terraform changes: `terraform plan` output pasted above
- [ ] No secrets, raw data, Terraform state, or scoping PDF committed
- [ ] Docs cite run ids or dates, not counts that change with every run
- [ ] After review: every finding fixed in this PR, each thread answered with the fixing commit, and review re-requested from everyone who requested changes
