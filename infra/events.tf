# The ways work starts without a person clicking:
#   Pub/Sub   web announces "registered"               → ticket-worker /
#   Pub/Sub   anyone asks for an email                 → ticket-worker /mail
#   Eventarc  Firestore reports a changed registration → waitlist-promoter
#   Scheduler every 5 minutes                          → housekeeping (job)

# ---------- Pub/Sub: registrations ----------

resource "google_pubsub_topic" "registrations" {
  count      = local.on.topics ? 1 : 0
  name       = "registrations"
  depends_on = [google_project_service.apis]
}

# Messages that failed every retry land here instead of retrying forever, from
# both topics: the source subscription is on each message (shared/gcp.py)
resource "google_pubsub_topic" "dead_letter" {
  count      = local.on.topics ? 1 : 0
  name       = "dead-letter"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "dead_letter" {
  count                      = local.on.topics ? 1 : 0
  name                       = "dead-letter"
  topic                      = google_pubsub_topic.dead_letter[0].id
  message_retention_duration = "604800s" # keep a week to inspect
  # A pull that is not acked hides the message for this long, so a Console "Pull" without
  # ack would hide it from scripts/replay-dead-letters.sh. That script acks within seconds.
  ack_deadline_seconds = 60

  # By default Pub/Sub deletes a subscription after 31 days without activity,
  # and messages published after that are lost with no error
  expiration_policy {
    ttl = ""
  }
}

# The lab creates it as Pull (step 4): messages wait in it until someone takes them.
# Step 8 switches it to Push, and Pub/Sub delivers each one to ticket-worker.
resource "google_pubsub_subscription" "ticket_worker" {
  count                = local.on.topics ? 1 : 0
  name                 = "ticket-worker"
  topic                = google_pubsub_topic.registrations[0].id
  ack_deadline_seconds = 60

  # By default Pub/Sub deletes a subscription after 31 days without activity,
  # and messages published after that are lost with no error
  expiration_policy {
    ttl = ""
  }

  dynamic "push_config" {
    for_each = local.on.push ? [1] : []
    content {
      push_endpoint = try(google_cloud_run_v2_service.svc["ticket-worker"].uri, local.run_url["ticket-worker"])
      # Pub/Sub signs each push with this identity's token; Cloud Run checks it
      oidc_token {
        service_account_email = local.sa_email["pubsub-push"]
      }
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter[0].id
    max_delivery_attempts = 5
  }

  depends_on = [google_cloud_run_v2_service_iam_member.pubsub_invokes_worker, google_service_account.sa]
}

# ---------- Pub/Sub: emails ----------
# web, ticket-worker and housekeeping ask for emails here; ticket-worker's /mail
# sends them (shared/mail.py). A slow mail server delays a message, never a signup.

resource "google_pubsub_topic" "emails" {
  count      = local.on.topics ? 1 : 0
  name       = "emails"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "mailer" {
  count                = local.on.workers ? 1 : 0
  name                 = "mailer"
  topic                = try(google_pubsub_topic.emails[0].id, local.topic_id["emails"])
  ack_deadline_seconds = 60

  # By default Pub/Sub deletes a subscription after 31 days without activity,
  # and messages published after that are lost with no error
  expiration_policy {
    ttl = ""
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.svc["ticket-worker"].uri}/mail"
    oidc_token {
      service_account_email = try(google_service_account.sa["pubsub-push"].email, local.sa_email["pubsub-push"])
      audience              = google_cloud_run_v2_service.svc["ticket-worker"].uri # the service, not the path
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  # About 4 hours of retries: a mail provider's outage should delay a ticket email,
  # not lose it. Nothing else sends an email again (scripts/replay-dead-letters.sh does, by hand)
  dead_letter_policy {
    dead_letter_topic     = try(google_pubsub_topic.dead_letter[0].id, local.topic_id["dead-letter"])
    max_delivery_attempts = 50
  }

  depends_on = [google_cloud_run_v2_service_iam_member.pubsub_invokes_worker]
}

# ---------- Eventarc: a registration changed (Firestore) ----------
# Every update of a registrations/ document arrives at waitlist-promoter; it acts
# only when a seat-holding registration became cancelled.

resource "google_eventarc_trigger" "registration_changed" {
  count    = local.on.eventarc ? 1 : 0
  name     = "registration-changed"
  location = var.region # the Firestore database's location

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.firestore.document.v1.updated"
  }
  matching_criteria {
    attribute = "database"
    value     = "(default)"
  }
  matching_criteria {
    attribute = "document"
    value     = "registrations/{registration}"
    operator  = "match-path-pattern"
  }
  event_data_content_type = "application/protobuf" # the only format for Firestore events

  destination {
    cloud_run_service {
      service = "waitlist-promoter"
      region  = var.region
    }
  }

  service_account = local.sa_email["eventarc"]

  # The roles that broke the ARC104 lab when they were missing
  depends_on = [
    google_firestore_database.main,
    google_cloud_run_v2_service.svc,
    google_project_iam_member.eventarc_receiver,
    google_cloud_run_v2_service_iam_member.eventarc_invokes_promoter,
  ]
}

# ---------- Cloud Scheduler: housekeeping every 5 minutes ----------
# One recurring job instead of one Scheduler job per date: with many events
# there is no single opening time or deadline (jobs/housekeeping.py).

resource "google_cloud_scheduler_job" "housekeeping" {
  count     = local.on.schedule ? 1 : 0
  name      = "housekeeping"
  region    = var.region
  schedule  = "*/5 * * * *"
  time_zone = "Asia/Seoul"

  # The address the Console's "Add scheduler trigger" uses, so the job page's Triggers tab lists it
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.housekeeping[0].name}:run"
    oauth_token {
      service_account_email = local.sa_email["scheduler"]
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_housekeeping, google_service_account.sa]
}
