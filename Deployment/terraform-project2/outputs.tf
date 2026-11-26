output "project_number" {
  description = "Project 2's number, part of the Workload Identity provider name."
  value       = data.google_project.this.number
}

output "workload_identity_provider" {
  description = "Full provider name for google-github-actions/auth (workload_identity_provider)."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_reader_email" {
  description = "Service account for runs on any branch. Prit grants it storage.objectViewer on the project 1 data bucket."
  value       = google_service_account.ci_reader.email
}

output "ci_writer_email" {
  description = "Service account for runs on main. Prit grants it storage.objectViewer on the project 1 data bucket too."
  value       = google_service_account.ci_writer.email
}

output "image_repository" {
  description = "Docker repository path for docker push."
  value       = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "model_bucket" {
  description = "Bucket for model files, one path per registered version."
  value       = google_storage_bucket.models.name
}

output "serving_runtime_email" {
  description = "The runtime service account of brt-model and brt-web. Project 1's Terraform grants it read access to forecasts/ in the data bucket."
  value       = google_service_account.brt_serving.email
}

output "serving_service_urls" {
  description = "The Cloud Run URL of each service, by role."
  value       = { for key, service in google_cloud_run_v2_service.serving : key => service.uri }
}
