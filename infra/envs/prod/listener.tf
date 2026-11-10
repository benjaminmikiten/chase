# The OptiPlex's identity (ADR 0004). It can CREATE objects under episodes/ and violations/ and
# nothing else: no read, no list, no delete, nothing under evidence/. Its key is created by hand
# with gcloud in infra/bootstrap.sh, so the private key never enters Terraform state.
resource "google_service_account" "listener" {
  project      = var.project_id
  account_id   = "chase-listener"
  display_name = "chase listener (OptiPlex)"
  description  = "Uploads episodes and violation manifests from the OptiPlex. Create-only, two prefixes."

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "listener_create" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.listener.email}"

  condition {
    title       = "episodes-and-violations-only"
    description = "Create objects only under episodes/ and violations/."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/episodes/\") || resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.evidence.name}/objects/violations/\")"
  }
}
