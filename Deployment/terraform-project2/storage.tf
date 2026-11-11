# Model files for the Vertex AI Model Registry (ADR-006). Same region as the
# registry, which Google requires. Each registered version gets its own path;
# the CI writer can create objects but not delete or overwrite them
# (iam.tf), so a rollback target keeps its files.
resource "google_storage_bucket" "models" {
  project  = var.project_id
  name     = "${var.project_id}-models"
  location = upper(var.region)

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Versioning keeps an earlier copy if anything with delete rights (a team
  # member) ever replaces an object by mistake.
  versioning {
    enabled = true
  }

  # Default. Teardown of a bucket holding objects is a deliberate manual step.
  force_destroy = false

  depends_on = [google_project_service.project_2]
}
