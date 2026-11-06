# GitHub Actions reaches project 2 through Workload Identity Federation: the
# job's OIDC token is exchanged for a short-lived token of one service
# account. No service account key exists anywhere (CLAUDE.md, ADR-012).

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "OIDC tokens from GitHub Actions runs of ${var.github_repository}."

  depends_on = [google_project_service.project_2]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Tokens from any other repository are rejected before any binding is read.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  pool_principal_set = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}"
}

# --- Reader: any run of the repository (pull requests included) -------------

# Holds no project 2 roles. It exists so PR runs can read the DVC remote in
# project 1, which Prit grants there (#112).
resource "google_service_account" "ci_reader" {
  project      = var.project_id
  account_id   = "ci-reader"
  display_name = "CI reader (GitHub Actions, any branch)"
  description  = "Reads the versioned data for model runs on any branch of ${var.github_repository}. No project 2 roles."

  depends_on = [google_project_service.project_2]
}

resource "google_service_account_iam_member" "ci_reader_wif" {
  service_account_id = google_service_account.ci_reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal_set}/attribute.repository/${var.github_repository}"
}

# --- Writer: runs on main only ---------------------------------------------

# The provider condition already limits tokens to the repository, so binding
# on the ref adds the main-only rule. Pull request runs carry
# refs/pull/<n>/merge and cannot use this account.
resource "google_service_account" "ci_writer" {
  project      = var.project_id
  account_id   = "ci-writer"
  display_name = "CI writer (GitHub Actions, main only)"
  description  = "Pushes images and registers models from main of ${var.github_repository}."

  depends_on = [google_project_service.project_2]
}

resource "google_service_account_iam_member" "ci_writer_wif" {
  service_account_id = google_service_account.ci_writer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "${local.pool_principal_set}/attribute.ref/refs/heads/main"
}
