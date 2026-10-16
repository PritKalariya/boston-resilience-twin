# Raw pulls plus the DVC remote. One bucket, versioned, private.
resource "google_storage_bucket" "data" {
  project  = var.project_id
  name     = "boston-resilience-twin-data"
  location = upper(var.region)

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Default. Teardown of a bucket holding objects is a deliberate manual step.
  force_destroy = false

  depends_on = [google_project_service.project_1]
}
