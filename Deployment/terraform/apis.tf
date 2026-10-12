# Only the APIs project 1 needs. Cloud Run, Artifact Registry, Vertex, and
# Gemini belong to project 2 (ADR-009) and are not enabled here.
locals {
  project_1_apis = [
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ]
}

resource "google_project_service" "project_1" {
  for_each = toset(local.project_1_apis)

  project = var.project_id
  service = each.value

  # Leaving the APIs enabled on destroy keeps a teardown from breaking
  # anything else that happens to live in the project.
  disable_on_destroy         = false
  disable_dependent_services = false
}
