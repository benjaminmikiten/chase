output "evidence_bucket" {
  description = "Set as gcs_bucket in the OptiPlex's config/chase.yaml."
  value       = google_storage_bucket.evidence.name
}

output "listener_service_account" {
  description = "Create its key with infra/bootstrap.sh (never in Terraform)."
  value       = google_service_account.listener.email
}

output "report_service_url" {
  value = google_cloud_run_v2_service.report.uri
}

output "workload_identity_provider" {
  description = "GitHub repo variable GCP_WORKLOAD_IDENTITY_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_service_account" {
  description = "GitHub repo variable GCP_SERVICE_ACCOUNT."
  value       = google_service_account.ci.email
}

output "artifact_registry_url" {
  description = "GitHub repo variable GCP_ARTIFACT_REGISTRY_URL."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "region" {
  description = "GitHub repo variable GCP_REGION."
  value       = var.region
}
