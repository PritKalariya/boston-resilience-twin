# ADR-012: Public repo with a well-structured docs/ folder

- Status: accepted
- Date: 2026-10-04
- Owner: kalariya.p
- Phase: setup

## Context

The course requires the project repo to be public. The repo is also what graders read: the scoping template asks for its folder structure and a README with installation and usage. Six people add docs to it over the term, and without a structure they scatter.

## Decision

- The repo is public at github.com/PritKalariya/boston-resilience-twin.
- docs/ holds the project docs, kept well structured:
  - one topic per file, with a name that says what it covers
  - related docs grouped in subfolders, such as decisions/ and data/
- The scoping PDF stays out of the repo; docs/project-spec.md carries the scope.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Public repo, structured docs/ (chosen)** | $0 | A little care per PR | Anything committed is public, history included |
| Public repo, docs wherever each author puts them | $0 | None | Graders and teammates can't find things; copies drift apart |
| Private repo | $0 | Invites for graders | Not allowed by the course |

## Consequences

- No secrets in the repo or its history. A secret that reaches history must be rotated; deleting the commit is not enough.
- Raw and processed data stay in Cloud Storage, not in the repo.
- A new doc goes in the folder that fits it, or in a new subfolder if none does.

## Revisit when

- The course changes its repo requirement.
- docs/ gets hard to navigate.
