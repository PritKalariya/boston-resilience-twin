# --- VM service account -----------------------------------------------------

resource "google_storage_bucket_iam_member" "vm_sa_data_bucket" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.airflow_vm.email}"
}

resource "google_secret_manager_secret_iam_member" "vm_sa_secrets" {
  for_each = google_secret_manager_secret.project_1

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.airflow_vm.email}"
}

# --- Team members -----------------------------------------------------------

locals {
  team = toset(var.team_members)
  team_project_roles = [
    "roles/viewer",
    "roles/compute.instanceAdmin.v1", # start, stop, SSH, reconfigure the VM
    "roles/compute.osLogin",          # SSH through OS Login
  ]
  team_project_grants = {
    for pair in setproduct(var.team_members, local.team_project_roles) :
    "${pair[0]}|${pair[1]}" => {
      member = pair[0]
      role   = pair[1]
    }
  }
  team_secret_grants = {
    for pair in setproduct(var.team_members, local.secret_ids) :
    "${pair[0]}|${pair[1]}" => {
      member    = pair[0]
      secret_id = pair[1]
    }
  }
}

resource "google_project_iam_member" "team" {
  for_each = local.team_project_grants

  project = var.project_id
  role    = each.value.role
  member  = each.value.member
}

resource "google_storage_bucket_iam_member" "team_data_bucket" {
  for_each = local.team

  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = each.value
}

resource "google_secret_manager_secret_iam_member" "team_secrets" {
  for_each = local.team_secret_grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.project_1[each.value.secret_id].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value.member
}

# SSH via OS Login to a VM that has a service account attached also requires permission to act as that service account.
resource "google_service_account_iam_member" "team_act_as_vm_sa" {
  for_each = toset(var.team_members)

  service_account_id = google_service_account.airflow_vm.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
