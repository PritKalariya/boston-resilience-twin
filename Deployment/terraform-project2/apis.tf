# Only the APIs project 2 needs now. Cloud Run and Gemini are enabled in
# Deployment, when their services exist.
locals {
  project_2_apis = [
    "aiplatform.googleapis.com",       # Vertex AI Model Registry
    "artifactregistry.googleapis.com", # Docker images
    "billingbudgets.googleapis.com",   # the budget alert
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # short-lived tokens for the CI service accounts
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com", # Workload Identity Federation token exchange
  ]
}

resource "google_project_service" "project_2" {
  for_each = toset(local.project_2_apis)

  project = var.project_id
  service = each.value

  # Leaving the APIs enabled on destroy keeps a teardown from breaking
  # anything else that happens to live in the project.
  disable_on_destroy         = false
  disable_dependent_services = false
}

data "google_project" "this" {
  project_id = var.project_id
}
