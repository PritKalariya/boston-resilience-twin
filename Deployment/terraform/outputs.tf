output "vm_name" {
  description = "Name of the Airflow VM."
  value       = google_compute_instance.airflow.name
}

output "vm_zone" {
  description = "Zone of the Airflow VM, needed by the stop and start commands."
  value       = google_compute_instance.airflow.zone
}

output "data_bucket_name" {
  description = "Bucket holding raw pulls and the DVC remote."
  value       = google_storage_bucket.data.name
}

output "service_account_email" {
  description = "Service account attached to the Airflow VM."
  value       = google_service_account.airflow_vm.email
}
