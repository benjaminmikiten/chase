# Keyless deploys from GitHub Actions (same pattern as tower): workflows exchange their GitHub OIDC
# token for short-lived Google credentials. Only this repo's main branch can deploy.
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "Identities from GitHub Actions OIDC tokens."

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"           = "assertion.sub"
    "attribute.repository"     = "assertion.repository"
    "attribute.ref"            = "assertion.ref"
    "attribute.repository_ref" = "assertion.repository + '@' + assertion.ref"
  }

  # Reject tokens from any other repository before they reach IAM.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "chase-ci"
  display_name = "chase CI (GitHub Actions)"
  description  = "Pushes the report image and deploys the report service, from main only."

  depends_on = [google_project_service.required]
}

resource "google_service_account_iam_member" "ci_wif" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_ref/${var.github_repository}@${var.ci_branch}"
}

resource "google_artifact_registry_repository_iam_member" "ci_pushes" {
  project    = google_artifact_registry_repository.images.project
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_cloud_run_v2_service_iam_member" "ci_deploys_report" {
  project  = var.project_id
  location = google_cloud_run_v2_service.report.location
  name     = google_cloud_run_v2_service.report.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.ci.email}"
}

# Deploying a revision that runs as chase-report requires acting as it.
resource "google_service_account_iam_member" "ci_acts_as_report" {
  service_account_id = google_service_account.report.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.ci.email}"
}
