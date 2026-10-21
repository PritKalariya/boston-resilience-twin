# Empty slots only. Values are added out of band with
# `gcloud secrets versions add`, never through Terraform.
locals {
  secret_ids = [
    "noaa-cdo-token",
    "census-api-key",
  ]
}

resource "google_secret_manager_secret" "project_1" {
  for_each = toset(local.secret_ids)

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.project_1]
}
