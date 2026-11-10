# Optional (var.grafana_enabled): the listener-side alerts from design §6.1, plus the dashboard.
# The rules alert when evidence collection silently stops: no heartbeat, no successful tick,
# a burst of audio errors, or uploads stuck. Each rule routes to its own email contact point, so
# the stack's notification policy is left untouched.

provider "grafana" {
  url  = var.grafana_url != "" ? var.grafana_url : "https://grafana.invalid"
  auth = var.grafana_auth
}

locals {
  grafana_count = var.grafana_enabled ? 1 : 0
  grafana_rules = {
    listener_silent = {
      title   = "chase: listener silent"
      expr    = "time() - max(chase_heartbeat_timestamp_seconds)"
      gt      = 900
      summary = "No audio has been scored for 15+ minutes (container down, mic unplugged, model broken)."
    }
    tick_stalled = {
      title   = "chase: rules/evidence loop stalled"
      expr    = "time() - max(chase_last_tick_success_timestamp_seconds)"
      gt      = 900
      summary = "The listener's tick hasn't succeeded in 15+ minutes: episodes and evidence aren't being written."
    }
    audio_errors = {
      title   = "chase: audio errors"
      expr    = "sum(increase(chase_audio_errors_total[15m]))"
      gt      = 20
      summary = "More than 20 listener errors in 15 minutes. Check `make logs` on the OptiPlex."
    }
    uploads_stuck = {
      title   = "chase: evidence not uploading"
      expr    = "min_over_time(max(chase_outbox_pending)[2h:5m])"
      gt      = 0
      summary = "Evidence has been waiting in the outbox for 2+ hours (network, key, or bucket problem)."
    }
  }
}

resource "grafana_folder" "chase" {
  count = local.grafana_count
  title = "chase"
}

resource "grafana_contact_point" "email" {
  count = local.grafana_count
  name  = "chase email"
  email {
    addresses = [var.alert_email]
  }
}

resource "grafana_rule_group" "chase" {
  count            = local.grafana_count
  name             = "chase listener"
  folder_uid       = grafana_folder.chase[0].uid
  interval_seconds = 60

  dynamic "rule" {
    for_each = local.grafana_rules
    content {
      name           = rule.value.title
      condition      = "C"
      for            = "5m"
      no_data_state  = "Alerting" # no data at all means the listener (or Alloy) is gone
      exec_err_state = "Alerting"
      annotations = {
        summary = rule.value.summary
      }
      labels = {
        app = "chase"
      }
      notification_settings {
        contact_point = grafana_contact_point.email[0].name
      }

      data {
        ref_id         = "A"
        datasource_uid = var.grafana_prometheus_uid
        relative_time_range {
          from = 600
          to   = 0
        }
        model = jsonencode({ refId = "A", expr = rule.value.expr, instant = true })
      }
      data {
        ref_id         = "B"
        datasource_uid = "__expr__"
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode({ refId = "B", type = "reduce", expression = "A", reducer = "last" })
      }
      data {
        ref_id         = "C"
        datasource_uid = "__expr__"
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode({
          refId      = "C"
          type       = "threshold"
          expression = "B"
          conditions = [{ evaluator = { type = "gt", params = [rule.value.gt] } }]
        })
      }
    }
  }
}

resource "grafana_dashboard" "chase" {
  count       = local.grafana_count
  folder      = grafana_folder.chase[0].uid
  config_json = file("${path.module}/../../../grafana/dashboard.json")
  overwrite   = true
}
