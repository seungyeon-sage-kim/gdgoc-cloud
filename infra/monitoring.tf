# Knowing it works without watching it: an uptime check, two SLOs for web,
# alerts that email someone, and one dashboard.
#
#   SLI  what we measure       share of web requests that are not 5xx;
#                              share of web requests answered within 500 ms
#   SLO  the promise           99% available, 95% under 500 ms, over 28 days
#   Alert                      the error budget burns too fast, /health stops
#                              answering, tickets or emails back up in Pub/Sub,
#                              a message lands in the dead letter, or the
#                              housekeeping job stops succeeding
#   Business metrics           signups, waitlist length, promotions, check-ins:
#                              counted from the services' JSON logs
#                              (shared/runtime.py event())
#
# Cloud Run runs the machines, so there is no CPU or disk of our own to watch:
# monitoring starts at the service (requests, latency, instances) and the work.

locals {
  web_host = trimprefix(try(google_cloud_run_v2_service.svc["web"].uri, local.web_url), "https://")
  channels = var.production_extras && var.alert_email != "" ? [google_monitoring_notification_channel.email[0].name] : []
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.production_extras && var.alert_email != "" ? 1 : 0
  display_name = "gdgoc-tickets alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

# ---------- uptime: is /health answering from outside Google? ----------

resource "google_monitoring_uptime_check_config" "web" {
  count        = var.production_extras ? 1 : 0
  display_name = "web /health"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type   = "uptime_url"
    labels = { project_id = var.project_id, host = local.web_host }
  }

  depends_on = [google_project_service.apis]

  # An alert refers to this check, and Google will not delete a check that is in use
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_monitoring_alert_policy" "web_down" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "web is down (/health failing)"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "uptime check failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.web[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1 # more than one checker location failing
      duration        = "600s"
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
    }
  }
}

# ---------- SLOs for web ----------

resource "google_monitoring_custom_service" "web" {
  count        = var.production_extras ? 1 : 0
  service_id   = "gdgoc-tickets-web"
  display_name = "gdgoc-tickets web"
}

locals {
  web_requests = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\" resource.label.\"service_name\"=\"${var.web_service_name}\""
}

resource "google_monitoring_slo" "availability" {
  count               = var.production_extras ? 1 : 0
  service             = google_monitoring_custom_service.web[0].service_id
  slo_id              = "availability"
  display_name        = "99% of web requests are not server errors (28 days)"
  goal                = 0.99
  rolling_period_days = 28

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "${local.web_requests} metric.label.\"response_code_class\"=\"5xx\""
      total_service_filter = local.web_requests
    }
  }
}

resource "google_monitoring_slo" "latency" {
  count               = var.production_extras ? 1 : 0
  service             = google_monitoring_custom_service.web[0].service_id
  slo_id              = "latency"
  display_name        = "95% of web requests answer within 500 ms (28 days)"
  goal                = 0.95
  rolling_period_days = 28

  request_based_sli {
    distribution_cut {
      distribution_filter = "metric.type=\"run.googleapis.com/request_latencies\" resource.type=\"cloud_run_revision\" resource.label.\"service_name\"=\"${var.web_service_name}\""
      range {
        max = 500 # milliseconds
      }
    }
  }
}

# Burn rate 10 = spending the 28-day error budget ten times too fast
resource "google_monitoring_alert_policy" "availability_burn" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "web errors are using up the SLO budget"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "1-hour burn rate above 10"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"3600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      duration        = "0s"
    }
  }
}

# ---------- the async path ----------

resource "google_monitoring_alert_policy" "dead_letters" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "a ticket message failed every retry (dead letter)"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "messages waiting in dead-letter"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"dead-letter\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}

# Tickets waiting: the worker is down, slow, or at max instances
resource "google_monitoring_alert_policy" "ticket_backlog" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "tickets are waiting in Pub/Sub for over 5 minutes"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "oldest unacknowledged registration message older than 5 min"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"ticket-worker\""
      comparison      = "COMPARISON_GT"
      threshold_value = 300 # seconds
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}

# Emails waiting: the provider is down or over its limit. Retries go on for hours, so this comes first.
resource "google_monitoring_alert_policy" "mail_backlog" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "emails are waiting in Pub/Sub for over 15 minutes"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "oldest unacknowledged email message older than 15 min"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"mailer\""
      comparison      = "COMPARISON_GT"
      threshold_value = 900 # seconds
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}

# Reminders, releases, pre-warm and repairs all stop with this job, and none of it
# shows up as a web error. It reports zeros between runs, so "no successful run" is a
# sum below 1; the second condition covers the metric going silent altogether.
locals {
  job_succeeded = "metric.type=\"run.googleapis.com/job/completed_task_attempt_count\" resource.type=\"cloud_run_job\" resource.label.\"job_name\"=\"housekeeping\" metric.label.\"result\"=\"succeeded\""
}

resource "google_monitoring_alert_policy" "job_stopped" {
  count                 = var.production_extras ? 1 : 0
  display_name          = "housekeeping has not succeeded for 15 minutes"
  combiner              = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "no successful run in 15 minutes"
    condition_threshold {
      filter          = local.job_succeeded
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "0s"
      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  conditions {
    display_name = "the job is not reporting at all"
    condition_absent {
      filter   = local.job_succeeded
      duration = "900s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

# ---------- business metrics, counted from the JSON logs ----------

locals {
  run_logs = "resource.type=\"cloud_run_revision\" OR resource.type=\"cloud_run_job\""
  log_counters = {
    signups        = { event = "signup", label = "result", description = "Signup attempts by result (pending, waitlisted, full, duplicate, rate_limited, bot_suspected)" }
    checkins       = { event = "checkin", label = "result", description = "Check-ins by result (ok, already, not_issued, cancelled)" }
    tickets_issued = { event = "ticket_issued", label = "", description = "Tickets issued by ticket-worker" }
    promotions     = { event = "promotion", label = "", description = "Freed seats handled by waitlist-promoter" }
    releases       = { event = "release", label = "", description = "Housekeeping runs that released unconfirmed seats" }
  }
}

resource "google_logging_metric" "counter" {
  for_each    = var.production_extras ? local.log_counters : {}
  name        = "gdgoc_tickets/${each.key}"
  description = each.value.description
  filter      = "(${local.run_logs}) AND jsonPayload.event=\"${each.value.event}\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    dynamic "labels" {
      for_each = each.value.label == "" ? [] : [each.value.label]
      content {
        key        = labels.value
        value_type = "STRING"
      }
    }
  }
  label_extractors = each.value.label == "" ? {} : { (each.value.label) = "EXTRACT(jsonPayload.${each.value.label})" }
}

# Waitlist length, as reported after every signup and promotion
resource "google_logging_metric" "waitlist_length" {
  count           = var.production_extras ? 1 : 0
  name            = "gdgoc_tickets/waitlist_length"
  description     = "Waitlist length after each signup or promotion"
  filter          = "(${local.run_logs}) AND jsonPayload.waitlisted>=0"
  value_extractor = "EXTRACT(jsonPayload.waitlisted)"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
  }
  bucket_options {
    linear_buckets {
      num_finite_buckets = 50
      width              = 2
      offset             = 0
    }
  }
}

# ---------- one dashboard ----------

resource "google_monitoring_dashboard" "main" {
  count = var.production_extras ? 1 : 0
  dashboard_json = jsonencode({
    displayName = "gdgoc-tickets"
    mosaicLayout = {
      columns = 12
      tiles = [
        for i, chart in [
          {
            title  = "web requests by response class (error rate)"
            filter = local.web_requests
            group  = ["metric.label.\"response_code_class\""]
            align  = "ALIGN_RATE"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "web latency, 95th percentile (ms)"
            filter = "metric.type=\"run.googleapis.com/request_latencies\" resource.type=\"cloud_run_revision\" resource.label.\"service_name\"=\"${var.web_service_name}\""
            group  = []
            align  = "ALIGN_PERCENTILE_95"
            reduce = "REDUCE_MAX"
          },
          {
            title  = "instances per service"
            filter = "metric.type=\"run.googleapis.com/container/instance_count\" resource.type=\"cloud_run_revision\""
            group  = ["resource.label.\"service_name\""]
            align  = "ALIGN_MAX"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "cold starts: container startup time, 95th percentile (ms)"
            filter = "metric.type=\"run.googleapis.com/container/startup_latencies\" resource.type=\"cloud_run_revision\""
            group  = ["resource.label.\"service_name\""]
            align  = "ALIGN_PERCENTILE_95"
            reduce = "REDUCE_MAX"
          },
          {
            title  = "Pub/Sub messages waiting (ticket-worker, dead letter)"
            filter = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" resource.type=\"pubsub_subscription\""
            group  = ["resource.label.\"subscription_id\""]
            align  = "ALIGN_MAX"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "signups per minute by result"
            filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.counter["signups"].name}\""
            group  = ["metric.label.\"result\""]
            align  = "ALIGN_RATE"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "waitlist length"
            filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.waitlist_length[0].name}\""
            group  = []
            align  = "ALIGN_PERCENTILE_99"
            reduce = "REDUCE_MAX"
          },
          {
            title  = "tickets issued per minute"
            filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.counter["tickets_issued"].name}\""
            group  = []
            align  = "ALIGN_RATE"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "waitlist promotions per minute"
            filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.counter["promotions"].name}\""
            group  = []
            align  = "ALIGN_RATE"
            reduce = "REDUCE_SUM"
          },
          {
            title  = "check-ins per minute by result"
            filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.counter["checkins"].name}\""
            group  = ["metric.label.\"result\""]
            align  = "ALIGN_RATE"
            reduce = "REDUCE_SUM"
          },
          # Written the way the API stores it (no zero positions, no empty lists, the axis
          # spelled out), so that a plan shows no difference here
          ] : merge({ for k, v in { xPos = (i % 2) * 6, yPos = floor(i / 2) * 4 } : k => v if v > 0 }, {
            width  = 6
            height = 4
            widget = {
              title = chart.title
              xyChart = {
                dataSets = [{
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = chart.filter
                      aggregation = merge({
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = chart.align
                        crossSeriesReducer = chart.reduce
                      }, { for k, v in { groupByFields = chart.group } : k => v if length(v) > 0 })
                    }
                  }
                }]
              }
            }
        })
      ]
    }
  })

  depends_on = [google_project_service.apis]
}
