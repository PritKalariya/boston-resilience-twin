# Data access

How to reach the APIs that need a key or an identifying header, and one request per API to check access. The team's keys live in Secret Manager in project 1 (ADR-015, ADR-019). Pipeline tasks read them from environment variables, which docker-compose.yaml passes from the gitignored local `.env`. Keys never go into this file, the repo, chat, or a ticket.

| API | Used for | Credential | Secret | Without it |
|---|---|---|---|---|
| NOAA Climate Data Online v2 | Logan daily history (GHCN-D, USW00014739) | token in the `token` header | `noaa-cdo-token` | 400, "Token parameter is required." |
| Census Data API | ACS 5-year tables | `key` query parameter | `census-api-key` | 302 to `missing_key.html` |
| NWS API (api.weather.gov) | forecasts and alerts | identifying `User-Agent` header | `nws-user-agent` | 403 |

NOAA tides (CO-OPS) and Open-Meteo need no credential and are not listed here.

## In the pipeline

| Secret | Environment variable |
|---|---|
| `noaa-cdo-token` | `NOAA_CDO_TOKEN` |
| `census-api-key` | `CENSUS_API_KEY` |
| `nws-user-agent` | `NWS_USER_AGENT` |

The variables are set in `.env` (names in `.env.example`) and reach every Airflow container through the shared environment block in `docker-compose.yaml`. A task reads a key with `credentials.require("NOAA_CDO_TOKEN")` (`Data-Pipeline/scripts/credentials.py`) when it runs. An unset or empty key fails that task with `NOAA_CDO_TOKEN is not set. ...`; the stack and the other tasks still run.

- **Team members:** fill `.env` from Secret Manager with the runbook command ("API keys in .env").
- **Anyone else, such as a grader:** request your own NOAA token and Census key (below) and set `NWS_USER_AGENT` to a string with your app name and a contact email. Put each in `.env` in single quotes, for example `NOAA_CDO_TOKEN='...'`.

After changing `.env`, run `docker compose up -d --wait` so the containers pick up the new values.

## Getting a credential

- **NOAA CDO token:** request one at https://www.ncdc.noaa.gov/cdo-web/token. It arrives by email. Limits: 5 requests a second, 10,000 a day per token.
- **Census key:** request one at https://api.census.gov/data/key_signup.html, then activate it from the link in the email.
- **NWS user agent:** no sign-up. NWS asks for a string that identifies the app and a contact, for example `(boston-resilience-twin, contact-email)`. It holds a contact email and the repo is public, so it is stored as a secret too.

Add each value with the command in the runbook ("Adding a secret value"). Only the members in `secret_writers` can add values; every member can read them.

## Checking access

Each command reads the credential from Secret Manager and passes it to curl on standard input, so it does not appear in shell history or the process list. Each prints only the HTTP status code; `200` means access works. Run them after `gcloud auth login`; each names project 1 (`--project=boston-resilience-twin`), so another default gcloud project does not matter.

**NOAA CDO** (list one dataset):

```bash
gcloud secrets versions access latest --secret=noaa-cdo-token --project=boston-resilience-twin | sed 's/^/token: /' \
  | curl -s -o /dev/null -w '%{http_code}\n' -H @- \
    'https://www.ncei.noaa.gov/cdo-web/api/v2/datasets?limit=1'
```

**Census** (2023 ACS 5-year total population, Suffolk County):

```bash
gcloud secrets versions access latest --secret=census-api-key --project=boston-resilience-twin \
  | curl -s -G -o /dev/null -w '%{http_code}\n' --data-urlencode 'key@-' \
    -d 'get=NAME,B01003_001E' -d 'for=county:025' -d 'in=state:25' \
    'https://api.census.gov/data/2023/acs/acs5'
```

A bad key returns 302, not an error code, so anything other than `200` is a failure.

**NWS** (grid point for Boston City Hall; the forecast office is BOX):

```bash
gcloud secrets versions access latest --secret=nws-user-agent --project=boston-resilience-twin | sed 's/^/User-Agent: /' \
  | curl -s -o /dev/null -w '%{http_code}\n' -H @- -H 'Accept: application/geo+json' \
    'https://api.weather.gov/points/42.3601,-71.0589'
```

To see the response body, replace `-o /dev/null -w '%{http_code}\n'` with `| head -c 300` at the end. Do not add `-v`: it prints the request headers, including the token.

## Last check

| API | Date | Status | By |
|---|---|---|---|
| NOAA CDO | 2026-10-20 | 200 | Kirtan |
| Census | 2026-10-20 | 200 | Kirtan |
| NWS | 2026-10-26 | 200 | Kirtan |

## If a request fails

- **NOAA 400 "not valid":** the token is wrong or has a trailing newline. Check with the runbook's trailing-newline command.
- **Census 302 to `invalid_key.html`:** the key is wrong, has a trailing newline, or was never activated from the email.
- **NWS 403:** the user agent is empty or missing. Check that `nws-user-agent` has a version.
- **gcloud `PERMISSION_DENIED`:** your account lacks `secretAccessor` on project 1. Ask the project 1 owner.
- **gcloud `NOT_FOUND`:** the secret slot or its first version does not exist yet.
