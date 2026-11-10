# Alerts that need no Grafana (design §6.1): the report service failing, or messages giving up
# into the dead-letter topic. Free tier.

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "chase alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.required]
}

resource "google_logging_metric" "report_failures" {
  project = var.project_id
  name    = "chase-report-failures"
  filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"report\" AND (jsonPayload.event=\"report_failed\" OR jsonPayload.event=\"report_failed_after_send\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "report_failures" {
  project      = var.project_id
  display_name = "chase: report service failed"
  combiner     = "OR"
  conditions {
    display_name = "report_failed logged"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.report_failures.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  documentation {
    content   = "The chase report service logged a failure. Check Cloud Logging for the report service (jsonPayload.event=report_failed). Pub/Sub retries automatically; repeated failures end up in the chase-violations-dead subscription."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "dead_letters" {
  project      = var.project_id
  display_name = "chase: violation gave up (dead letter)"
  combiner     = "OR"
  conditions {
    display_name = "dead-letter backlog > 0"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.type=\"pubsub_subscription\" AND resource.labels.subscription_id=\"${google_pubsub_subscription.dead.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  documentation {
    content   = "A violation report failed 10 times and was dead-lettered. Inspect the chase-violations-dead subscription and the report service logs, fix, then re-trigger by re-uploading the manifest (see infra/README.md)."
    mime_type = "text/markdown"
  }
}
