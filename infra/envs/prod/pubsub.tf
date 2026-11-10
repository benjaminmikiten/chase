# Trigger (ADR 0005): a GCS notification for objects under violations/ publishes to Pub/Sub, and a
# push subscription calls the report service with an OIDC token. Explicit 600 s ack deadline,
# exponential retry, and a dead-letter topic after 10 attempts.

resource "google_pubsub_topic" "violations" {
  project    = var.project_id
  name       = "chase-violations"
  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "dead" {
  project    = var.project_id
  name       = "chase-violations-dead"
  depends_on = [google_project_service.required]
}

# Keeps dead letters around (7 days) so they can be inspected; alerting watches its backlog.
resource "google_pubsub_subscription" "dead" {
  project                    = var.project_id
  name                       = "chase-violations-dead"
  topic                      = google_pubsub_topic.dead.id
  message_retention_duration = "604800s"
  expiration_policy {
    ttl = ""
  }
}

data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

resource "google_pubsub_topic_iam_member" "gcs_publishes" {
  project = var.project_id
  topic   = google_pubsub_topic.violations.id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_notification" "violations" {
  bucket             = google_storage_bucket.evidence.name
  payload_format     = "JSON_API_V1"
  topic              = google_pubsub_topic.violations.id
  event_types        = ["OBJECT_FINALIZE"]
  object_name_prefix = "violations/"

  depends_on = [google_pubsub_topic_iam_member.gcs_publishes]
}

# The identity Pub/Sub uses to call the service. It can invoke `report` and nothing else.
resource "google_service_account" "push" {
  project      = var.project_id
  account_id   = "chase-push"
  display_name = "chase Pub/Sub push"
  description  = "Invokes the report service for violation notifications."

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "push_invokes_report" {
  project  = var.project_id
  location = google_cloud_run_v2_service.report.location
  name     = google_cloud_run_v2_service.report.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.push.email}"
}

# Pub/Sub mints the OIDC token as the push account.
resource "google_service_account_iam_member" "pubsub_mints_push_token" {
  service_account_id = google_service_account.push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}

resource "google_pubsub_subscription" "report" {
  project = var.project_id
  name    = "chase-report-push"
  topic   = google_pubsub_topic.violations.id

  ack_deadline_seconds       = 600 # matches the service's 600 s request timeout
  message_retention_duration = "604800s"

  # Until the real report image is deployed, the service is a placeholder that answers 200 to
  # anything, which would ack and silently drop reports. So push stays OFF (a pull subscription
  # that just keeps messages for 7 days) until var.report_push_enabled is set after the first
  # deploy. Turning it on delivers anything retained meanwhile.
  dynamic "push_config" {
    for_each = var.report_push_enabled ? [1] : []
    content {
      push_endpoint = google_cloud_run_v2_service.report.uri
      oidc_token {
        service_account_email = google_service_account.push.email
        audience              = google_cloud_run_v2_service.report.uri
      }
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead.id
    max_delivery_attempts = 10
  }

  expiration_policy {
    ttl = ""
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.push_invokes_report,
    google_service_account_iam_member.pubsub_mints_push_token,
  ]
}

# Dead-lettering needs the Pub/Sub service agent to publish to the dead topic and to ack on the
# source subscription.
resource "google_pubsub_topic_iam_member" "agent_publishes_dead" {
  project = var.project_id
  topic   = google_pubsub_topic.dead.id
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "agent_subscribes_report" {
  project      = var.project_id
  subscription = google_pubsub_subscription.report.id
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
