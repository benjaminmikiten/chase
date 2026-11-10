# Container images for the report service, pushed by CI.
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "chase"
  description   = "chase container images (report service)."
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

# Evidence: private, uniform access, deleted on a schedule (design §5).
#   episodes/, violations/  uploaded by the listener; 30 days (long enough to become a violation)
#   evidence/               assembled by the report service per violation; 365 days
resource "google_storage_bucket" "evidence" {
  name     = "${var.project_id}-chase-evidence"
  project  = var.project_id
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["episodes/", "violations/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age            = 365
      matches_prefix = ["evidence/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}
