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
  description = "The GitHub repository whose Actions runs may authenticate, as owner/name. Used in descriptions; access is decided by the numeric ids below."
  type        = string
  default     = "PritKalariya/boston-resilience-twin"
}

# Names can be reclaimed after a rename or deletion; ids cannot.
# gh api repos/PritKalariya/boston-resilience-twin -q '.id, .owner.id'
variable "github_repository_id" {
  description = "Numeric id of the GitHub repository (the OIDC token's repository_id claim)."
  type        = string
  default     = "1404951557"
}

variable "github_repository_owner_id" {
  description = "Numeric id of the repository's owner (the OIDC token's repository_owner_id claim)."
  type        = string
  default     = "71484962"
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
