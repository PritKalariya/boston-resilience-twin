variable "project_id" {
  description = "Project 2 (ADR-009): Artifact Registry, Vertex AI Model Registry, model files; later Cloud Run and Gemini."
  type        = string
  default     = "boston-resilience-twin-p2"
}

variable "region" {
  description = "Region for every project 2 resource. The registry, its model bucket and the image repository must share it (ADR-006)."
  type        = string
  default     = "us-east1"
}

variable "github_repository" {
  description = "The only GitHub repository whose Actions runs may authenticate, as owner/name."
  type        = string
  default     = "PritKalariya/boston-resilience-twin"
}

variable "billing_account" {
  description = <<-EOT
    Billing account ID that pays for project 2 (the owner's education
    credits), for example "000000-000000-000000". No default: supplied
    through a tfvars file that is never committed.
  EOT
  type        = string
}

variable "budget_amount_usd" {
  description = "Budget the alert thresholds are measured against: one member's credit (ADR-009)."
  type        = number
  default     = 50
}

variable "project_admins" {
  description = <<-EOT
    IAM member strings for teammates who review plans and debug CI in
    project 2, for example ["user:name@northeastern.edu"]. They get
    roles/editor and roles/resourcemanager.projectIamAdmin (ADR-030 explains
    why not roles/owner). No default: supplied through a tfvars file that is
    never committed.
  EOT
  type        = list(string)
}
