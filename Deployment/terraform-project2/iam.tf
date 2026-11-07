# --- CI writer: least privilege ---------------------------------------------

# No predefined role is limited to registering models: roles/aiplatform.user
# also deletes models and runs training, endpoints and Gemini. This role holds
# only what #111 needs: upload a version, read versions, move aliases
# (champion and challenger), and follow the upload operation.
resource "google_project_iam_custom_role" "model_registrar" {
  project     = var.project_id
  role_id     = "modelRegistrar"
  title       = "Model registrar"
  description = "Upload model versions to the Vertex AI Model Registry and move their aliases. No delete, no deploy (ADR-030)."
  permissions = [
    "aiplatform.models.get",
    "aiplatform.models.list",
    "aiplatform.models.update",
    "aiplatform.models.upload",
    "aiplatform.operations.list",
  ]

  depends_on = [google_project_service.project_2]
}

resource "google_project_iam_member" "ci_writer_registrar" {
  project = var.project_id
  role    = google_project_iam_custom_role.model_registrar.id
  member  = "serviceAccount:${google_service_account.ci_writer.email}"
}

# Push to the one repository, not project wide.
resource "google_artifact_registry_repository_iam_member" "ci_writer_images" {
  project    = google_artifact_registry_repository.images.project
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci_writer.email}"
}

# Create and read model files. objectCreator cannot delete, so it cannot
# overwrite an existing object either: each version's files stay intact.
# Reading is for re-scoring the champion before a comparison (#111).
resource "google_storage_bucket_iam_member" "ci_writer_models_create" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ci_writer.email}"
}

resource "google_storage_bucket_iam_member" "ci_writer_models_read" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ci_writer.email}"
}

# --- Teammates who review plans and debug CI --------------------------------

# roles/owner cannot be granted through the API to an account outside the
# project's organization (it needs a console invitation, which is click-ops),
# so Terraform grants the nearest pair: everything Editor allows, plus IAM.
locals {
  admin_roles = [
    "roles/editor",
    "roles/resourcemanager.projectIamAdmin",
  ]
  admin_grants = {
    for pair in setproduct(var.project_admins, local.admin_roles) :
    "${pair[0]}|${pair[1]}" => {
      member = pair[0]
      role   = pair[1]
    }
  }
}

resource "google_project_iam_member" "admins" {
  for_each = local.admin_grants

  project = var.project_id
  role    = each.value.role
  member  = each.value.member

  depends_on = [google_project_service.project_2]
}
