terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # The state bucket already exists and is not managed by this config.
  # It is removed by hand at teardown; see docs/runbook.md.
  backend "gcs" {
    bucket = "boston-resilience-twin-tfstate"
    prefix = "project1"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
