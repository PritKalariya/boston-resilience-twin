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

# --- Project 2 CI identities ------------------------------------------------

# #112, ADR-030: CI reads the DVC remote in this project. The identities live in
# project 2 (#131), so the grant is made here, on the bucket only, read-only.
locals {
  ci_reader_emails = [
    "ci-reader@boston-resilience-twin-p2.iam.gserviceaccount.com",
    "ci-writer@boston-resilience-twin-p2.iam.gserviceaccount.com",
  ]
}

resource "google_storage_bucket_iam_member" "ci_data_bucket_reader" {
  for_each = toset(local.ci_reader_emails)

  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value}"
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

# --- VM admins (sudo) -------------------------------------------------------

# roles/compute.osLogin above is SSH without sudo. osAdminLogin is SSH with
# sudo, and it is bound to the airflow-vm instance rather than the project, so
# a future VM in project 1 does not inherit it.
resource "google_compute_instance_iam_member" "vm_admins" {
  for_each = toset(var.vm_admins)

  project       = google_compute_instance.airflow.project
  zone          = google_compute_instance.airflow.zone
  instance_name = google_compute_instance.airflow.name
  role          = "roles/compute.osAdminLogin"
  member        = each.value

  # Deleting an instance deletes its instance-level grants, but state still
  # lists them. instance_id is the numeric id and changes only when the VM is
  # recreated, so in-place updates such as metadata leave the grants alone.
  lifecycle {
    replace_triggered_by = [google_compute_instance.airflow.instance_id]
  }
}

# --- Secret writers ---------------------------------------------------------

# Adding a secret value needs more than accessor. The grant is per secret, so a
# writer can add versions to these slots and to nothing else in the project.
locals {
  secret_writer_grants = {
    for pair in setproduct(var.secret_writers, local.secret_ids) :
    "${pair[0]}|${pair[1]}" => {
      member    = pair[0]
      secret_id = pair[1]
    }
  }
}

resource "google_secret_manager_secret_iam_member" "secret_writers" {
  for_each = local.secret_writer_grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.project_1[each.value.secret_id].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = each.value.member
}
