# The report service (Plan 2): Pub/Sub push in, PDF + email out. Scale to zero, one request at a
# time (exactly-once email, ADR 0005).

resource "google_service_account" "report" {
  project      = var.project_id
  account_id   = "chase-report"
  display_name = "chase report (Cloud Run)"
  description  = "Assembles evidence, renders the report, emails it. Bucket + its two secrets only."

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "report_bucket" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.report.email}"
}

# Signed URLs on Cloud Run are signed through IAM (signBlob) as the service's own account.
resource "google_service_account_iam_member" "report_signs_as_itself" {
  service_account_id = google_service_account.report.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.report.email}"
}

# Secret shells. Real values are added by infra/bootstrap.sh (never in the repo or state). The
# placeholder version lets the first apply create the service; until the real values exist the
# service fails loudly (and alerts) instead of sending anything.
resource "google_secret_manager_secret" "report_config" {
  project   = var.project_id
  secret_id = "chase-report-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "gmail_app_password" {
  project   = var.project_id
  secret_id = "chase-gmail-app-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "report_config_placeholder" {
  secret      = google_secret_manager_secret.report_config.id
  secret_data = "{}"
  lifecycle {
    ignore_changes = [secret_data, enabled]
  }
}

resource "google_secret_manager_secret_version" "gmail_placeholder" {
  secret      = google_secret_manager_secret.gmail_app_password.id
  secret_data = "not-set"
  lifecycle {
    ignore_changes = [secret_data, enabled]
  }
}

resource "google_secret_manager_secret_iam_member" "report_reads_config" {
  secret_id = google_secret_manager_secret.report_config.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.report.email}"
}

resource "google_secret_manager_secret_iam_member" "report_reads_gmail" {
  secret_id = google_secret_manager_secret.gmail_app_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.report.email}"
}

resource "google_cloud_run_v2_service" "report" {
  project  = var.project_id
  name     = "report"
  location = var.region
  ingress  = var.report_ingress

  deletion_protection = false

  template {
    service_account                  = google_service_account.report.email
    timeout                          = "600s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0 # scale to zero: no cost while the dogs are quiet
      max_instance_count = 1 # one report at a time (ADR 0005)
    }

    containers {
      image = var.report_image

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi" # /tmp is memory; long merged episodes are processed one at a time
        }
        cpu_idle = true
      }

      env {
        name  = "REPORT_BUCKET"
        value = google_storage_bucket.evidence.name
      }
      env {
        name  = "ATTACHMENT_LIMIT_MB"
        value = tostring(var.attachment_limit_mb)
      }
      env {
        name = "REPORT_CONFIG"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.report_config.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GMAIL_APP_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gmail_app_password.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # CI deploys new images (report-deploy.yml); Terraform sets only the first one and must not roll
  # a deploy back on the next apply (same pattern as tower's site).
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.report_reads_config,
    google_secret_manager_secret_iam_member.report_reads_gmail,
    google_secret_manager_secret_version.report_config_placeholder,
    google_secret_manager_secret_version.gmail_placeholder,
  ]
}
