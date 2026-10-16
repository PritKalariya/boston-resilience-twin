variable "project_id" {
  description = "Project 1 (ADR-009): VM, Cloud Storage, monitoring, Secret Manager."
  type        = string
  default     = "boston-resilience-twin"
}

variable "region" {
  description = "Region for every project 1 resource."
  type        = string
  default     = "us-east1"
}

variable "zone" {
  description = "Zone for the Airflow VM."
  type        = string
  default     = "us-east1-b"
}

variable "team_members" {
  description = <<-EOT
    IAM member strings for the six team members, for example
    ["user:name@northeastern.edu"]. No default on purpose: the real
    addresses are supplied through a tfvars file that is never committed.
  EOT
  type        = list(string)
}

variable "secret_writers" {
  description = <<-EOT
    IAM member strings for the members who may add new versions to the
    project 1 secrets, for example ["user:name@northeastern.edu"]. They get
    roles/secretmanager.secretVersionAdder on those secrets only, never
    project wide. No default on purpose: the real addresses are supplied
    through a tfvars file that is never committed.
  EOT
  type        = list(string)
}
