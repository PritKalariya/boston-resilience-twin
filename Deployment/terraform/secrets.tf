# Empty slots only. Values are added out of band with
# `gcloud secrets versions add`, never through Terraform.
#
# The slots are in groups because each group has different readers and writers
# (ADR-034). Adding an id to a group never moves another slot, since the
# resource is keyed by the id.
locals {
  # API keys: every team member reads them (iam.tf, team_secrets) and the named
  # secret_writers add versions (ADR-019). Their ids are keys of existing grants,
  # so this list keeps its name and its members.
  secret_ids = [
    "noaa-cdo-token",
    "census-api-key",
    "nws-user-agent",
  ]

  # Airflow and Postgres secrets for airflow-vm (#64). Only airflow-vm-sa reads
  # them. No team member and no secret_writers entry holds a grant on them, so
  # the project owner adds their values.
  airflow_secret_ids = [
    "airflow-admin-password",
    "airflow-postgres-password",
    "airflow-jwt-secret",
    "airflow-api-secret-key",
    "airflow-fernet-key",
  ]

  # Slack incoming webhook for pipeline alerts (#34). Read by airflow-vm-sa only;
  # the named secret_writers add its versions (ADR-034, which closes ADR-019's
  # "decide writers before adding the Slack webhook").
  slack_secret_ids = [
    "slack-alert-webhook",
  ]

  # The slots the boot render treats as optional (startup.sh): with no version it
  # writes the key empty. Telling "no version" from a failed read needs
  # `versions list`, so airflow-vm-sa also gets viewer on these (iam.tf).
  optional_secret_ids = concat(local.secret_ids, local.slack_secret_ids)

  all_secret_ids = concat(local.secret_ids, local.airflow_secret_ids, local.slack_secret_ids)
}

resource "google_secret_manager_secret" "project_1" {
  for_each = toset(local.all_secret_ids)

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.project_1]
}
