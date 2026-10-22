# ADR-009: Hosting split across two GCP projects

- Status: accepted
- Date: 2026-10-04
- Owner: kalariya.p
- Phase: deploy

## Context

GCP credits are $50 per student and can't be pooled across accounts. The system has two kinds of load. The data plane runs all the time: the Airflow VM (ADR-010), Cloud Storage, monitoring, and secrets. The serving plane is bursty: Cloud Run revisions that scale to zero, images in Artifact Registry, models in the Vertex registry, and Gemini calls billed per request. Together they don't fit in one member's $50 with any margin.

## Decision

Two projects, each on one member's credits:

- Project 1 (Prit): the Airflow VM, Cloud Storage (raw pulls, versioned data, DVC remote), Cloud Monitoring, and Secret Manager.
- Project 2 (Kirtan): Cloud Run (forecast API, agent service, dashboard), Artifact Registry, the Vertex AI Model Registry, and Gemini on Vertex AI.

The split follows the load: steady cost in project 1, per-request cost in project 2. Each service sits in the same project as its main caller, so Cloud Run pulls images and calls Gemini without crossing a project boundary.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| One project on one member's credits | The VM alone is about $42 for the term, leaving about $8 for everything else | Lowest | Credits run out during the live run and everything stops at once |
| **Two projects split by load (chosen)** | Each half fits under $50 | Cross-project access between the two | Project 1 down: no new forecasts, and Cloud Run has nothing to serve unless it keeps its own copy. Project 2 down: forecasts still land in Cloud Storage, but nobody can see them |
| One project per area (up to six) | Spread thinnest | Six billing setups and access between every pair | Many small failure points, and no single view of the system |

## Consequences

- Cloud Run, CI/CD, and the Nov 16 live run depend on project 2 being set up first.
- Project 1's billing account has a $50 budget for Sep 1 to Dec 7, 2026 (`brt-50-cap`), with alerts at 50%, 90%, and 100%. It was set up by hand in the console, not in Terraform.

## Revisit when

- One member's credits have to carry both halves.
- Either project's projected spend passes $40 of its $50.
