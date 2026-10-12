# The VM authenticates through this attached service account via the metadata
# server. No key resource here: keys are forbidden by CLAUDE.md.
resource "google_service_account" "airflow_vm" {
  project      = var.project_id
  account_id   = "airflow-vm-sa"
  display_name = "Airflow VM service account"
  description  = "Attached to airflow-vm. Reads project 1 secrets and reads/writes the data bucket."

  depends_on = [google_project_service.project_1]
}
