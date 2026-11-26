# The model service's two Cloud Run services (#161, ADR-038): one image, two services, chosen by
# SERVING_ROUTES. Terraform creates them with a public placeholder image and owns their scaling,
# probes, environment and identity; Serving/scripts/deploy.py puts the real image on them by digest.
# lifecycle.ignore_changes lists exactly what the deploy script changes, so a later apply never
# rolls a deployed revision back.

resource "google_project_service" "run" {
  project = var.project_id
  service = "run.googleapis.com"

  # Leaving the API enabled on destroy, as for the other project 2 APIs (apis.tf).
  disable_on_destroy         = false
  disable_dependent_services = false
}

locals {
  placeholder_image = "us-docker.pkg.dev/cloudrun/container/hello"

  services = {
    model = {
      name          = "brt-model"
      max_instances = 2
      env = {
        SERVING_ROUTES = "model"
      }
    }
    web = {
      name          = "brt-web"
      max_instances = 1
      env = {
        SERVING_ROUTES = "web"
        FORECASTS_ROOT = "gs://${var.data_bucket_name}/forecasts"
      }
    }
  }
}

# One runtime identity for both services. Its only grant is read access to forecasts/ in the project 1
# data bucket (Deployment/terraform/iam.tf).
resource "google_service_account" "brt_serving" {
  project      = var.project_id
  account_id   = "brt-serving"
  display_name = "Model service runtime (Cloud Run)"
  description  = "Runs brt-model and brt-web. Reads forecasts/ in the project 1 data bucket and nothing else (ADR-038)."

  depends_on = [google_project_service.project_2]
}

resource "google_cloud_run_v2_service" "serving" {
  for_each = local.services

  project  = var.project_id
  location = var.region
  name     = each.value.name

  # The teardown must work (runbook): a service is recreated by the deploy script.
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.brt_serving.email
    timeout         = "30s"

    scaling {
      min_instance_count = 0
      max_instance_count = each.value.max_instances
    }

    containers {
      image = local.placeholder_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true # CPU is billed only while a request is served
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = each.value.env
        content {
          name  = env.key
          value = env.value
        }
      }

      # A revision whose model fails its hash or threshold check answers 503 on /health, so it never
      # becomes ready and never gets traffic. Up to 60 s to load the model.
      startup_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 5
        timeout_seconds   = 4
        failure_threshold = 12
      }
    }
  }

  # Latest revision, 100 percent. D12 decides rollouts, and must add `traffic` to ignore_changes
  # before it splits traffic (ADR-038).
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # Exactly what Serving/scripts/deploy.py changes: the image, the template labels (model-version and
  # git-commit) and the revision name Cloud Run gives, plus the client fields the Admin API sets.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].labels,
      template[0].revision,
      client,
      client_version,
    ]
  }

  depends_on = [google_project_service.run]
}

# brt-model is not public: only the Airflow VM's service account (project 1) may call it. Project
# admins hold roles/run.admin (ADR-036), which can invoke.
resource "google_cloud_run_v2_service_iam_member" "model_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.serving["model"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.airflow_vm_sa_email}"
}

# brt-web is public: the map page and the forecast rows are derived from public data (ADR-037).
resource "google_cloud_run_v2_service_iam_member" "web_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.serving["web"].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
