# One Docker repository: the training image now (#112), the Cloud Run images
# in Deployment (ADR-009, ADR-022).
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "brt-images"
  format        = "DOCKER"
  description   = "Boston Resilience Twin images: model training, later Cloud Run services."

  depends_on = [google_project_service.project_2]
}
