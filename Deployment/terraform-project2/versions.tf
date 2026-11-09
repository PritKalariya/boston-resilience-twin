terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # The state bucket lives in project 2 and is created by hand once, before
  # `terraform init` (ADR-030, "Bootstrap"). Terraform never manages or
  # destroys it.
  backend "gcs" {
    bucket = "boston-resilience-twin-p2-tfstate"
    prefix = "project2"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# The Billing Budgets API needs a quota project when called with user
# credentials (gcloud auth application-default login). Only the budget uses
# this alias, so every other call keeps the default behavior.
provider "google" {
  alias                 = "billing"
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}
