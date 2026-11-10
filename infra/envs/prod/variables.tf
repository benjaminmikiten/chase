variable "project_id" {
  description = "GCP project ID for chase (its own project; not shared)."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, the bucket, Pub/Sub and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to deploy via Workload Identity Federation."
  type        = string
  default     = "benjaminmikiten/chase"
}

variable "ci_branch" {
  description = "Git ref the CI service account may be impersonated from."
  type        = string
  default     = "refs/heads/main"
}

variable "alert_email" {
  description = "Where Cloud Monitoring alerts (report failures, dead letters) are emailed."
  type        = string
}

variable "report_image" {
  description = "Initial image for the report service. CI deploys real images afterwards (Terraform ignores later image changes)."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "report_ingress" {
  description = "Cloud Run ingress for the report service. Internal-only admits Pub/Sub push from this project; if the morning TEST report shows push being rejected, set INGRESS_TRAFFIC_ALL (IAM still allows only the push service account)."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

variable "attachment_limit_mb" {
  description = "Email attachment budget in base64-encoded MB. Over it, audio is linked instead of attached."
  type        = number
  default     = 15
}
