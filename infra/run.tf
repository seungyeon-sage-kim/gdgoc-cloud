# The three Cloud Run services and the job.
# Terraform owns their configuration; CI owns the image. That is why the image is
# in ignore_changes: a deploy from GitHub Actions is not "drift".
#
# In the lab, students deploy web (step 5) and the job (step 12) by hand; ./lab workers
# deploys the two workers (step 6). ./lab env prints the settings to paste.

locals {
  # web's address, known before it exists: https://SERVICE-PROJECT_NUMBER.REGION.run.app
  # Emails link to tickets with it.
  web_url = "https://${var.web_service_name}-${data.google_project.this.number}.${var.region}.run.app"

  # Settings the services and the job all read (shared/config.py). Events are not
  # settings: organizers create them on the admin pages and they live in Firestore.
  common_env = {
    BACKEND         = "gcp"
    GCP_PROJECT     = var.project_id
    ASSETS_BUCKET   = try(google_storage_bucket.assets[0].name, "${var.project_id}-assets")
    PUBLIC_BASE_URL = local.web_url
    ADMIN_EMAILS    = join(",", var.admin_emails)
    STAFF_EMAILS    = join(",", var.staff_emails)
    RETENTION_DAYS  = tostring(var.retention_days)
    # smtp: the email provider sends. log: nothing is sent yet (shared/mail.py)
    MAIL_BACKEND = var.mail_enabled ? "smtp" : "log"
  }

  # Each service gets settings that match its work, and only the settings it reads:
  #   web: mostly waits on the database, so one instance handles many requests. Its handlers are
  #        plain functions that FastAPI runs on a pool of 40 threads; past 40, requests would
  #        wait inside the instance, where Cloud Run can't see them, so 40 is the limit
  #   ticket-worker: 2 × 5 instances = at most 10 tickets and emails at once, which protects
  #                  the mail provider and Firestore. Its work is waiting; a QR takes about 5 ms
  #   waitlist-promoter: 2 × 3 instances = at most 6 at once; few seats free up at a time
  services = {
    web = {
      name        = var.web_service_name
      min         = var.web_min_instances
      ingress     = "INGRESS_TRAFFIC_ALL"
      public      = var.allow_public_web
      concurrency = 40
      max         = 20
      timeout     = "300s"
      memory      = "512Mi"
      env = merge({
        CONTACT_EMAIL             = var.contact_email    # shown in the privacy policy, only if set
        GOOGLE_CLIENT_ID          = var.google_client_id # Sign in with Google: a public value
        DEAD_LETTER_SUBSCRIPTION  = "dead-letter"        # the admin page shows what waits here
        SIGNUPS_PER_MINUTE_PER_IP = tostring(var.signups_per_minute_per_ip)
      }, var.production_extras ? { RECAPTCHA_SITE_KEY = google_recaptcha_enterprise_key.signup[0].name } : {})
      secrets = { TICKET_SECRET = "ticket-secret", SESSION_SECRET = "session-secret" }
      latest  = {}
      part    = "web"
    }
    ticket-worker = {
      name        = "ticket-worker"
      min         = 0
      ingress     = "INGRESS_TRAFFIC_INTERNAL_ONLY" # not reachable from the internet; Pub/Sub holds the run.invoker on it (iam.tf)
      public      = false
      concurrency = 2
      max         = 5
      timeout     = "60s" # Pub/Sub stops waiting at 60 s and delivers again (events.tf)
      memory      = "512Mi"
      env         = { SMTP_HOST = var.smtp_host, SMTP_USER = var.smtp_user, WORKER_DELAY_MS = tostring(var.worker_delay_ms) }
      secrets     = { TICKET_SECRET = "ticket-secret" }
      # Only this service sends email, so only it can read the provider's key and the from-address.
      # A person adds those values (data.tf), so it reads "latest" at instance start.
      latest = var.mail_enabled ? {
        SMTP_PASSWORD = try(google_secret_manager_secret.smtp_password[0].secret_id, "smtp-password")
        MAIL_FROM     = try(google_secret_manager_secret.mail_from[0].secret_id, "mail-from")
      } : {}
      part = "workers"
    }
    waitlist-promoter = {
      name        = "waitlist-promoter"
      min         = 0
      ingress     = "INGRESS_TRAFFIC_INTERNAL_ONLY" # not reachable from the internet; Eventarc holds the run.invoker on it (iam.tf)
      public      = false
      concurrency = 2
      max         = 3
      timeout     = "60s"
      memory      = "512Mi"
      env         = {}
      secrets     = {}
      latest      = {}
      part        = "workers"
    }
  }

  # The lab's first images come from Cloud Build (./lab start); otherwise a placeholder
  # until CI deploys
  first_image = { for name in ["web", "ticket-worker", "waitlist-promoter", "housekeeping"] :
    name => var.lab_image_tag != "" ? "${local.registry}/${name}:${var.lab_image_tag}" : var.initial_image
  }
}

resource "google_cloud_run_v2_service" "svc" {
  for_each            = { for k, v in local.services : k => v if local.on[v.part] }
  name                = each.value.name
  location            = var.region
  ingress             = each.value.ingress
  deletion_protection = false

  # Public: Cloud Run skips its invoker check, so nobody needs a Google identity. Google's
  # recommended way, and allowed where an org policy refuses an allUsers grant
  invoker_iam_disabled = each.value.public

  # At the service level, as Google recommends, not per revision. The minimum is only a
  # starting value: from then on the housekeeping job sets it (jobs/housekeeping.py).
  scaling {
    min_instance_count = each.value.min
    max_instance_count = each.value.max
  }

  template {
    service_account                  = google_service_account.sa[each.key].email
    max_instance_request_concurrency = each.value.concurrency
    timeout                          = each.value.timeout

    containers {
      image = local.first_image[each.key]

      resources {
        limits            = { cpu = "1", memory = each.value.memory }
        cpu_idle          = true # CPU only while handling a request, and no work runs after the reply
        startup_cpu_boost = true
      }

      # Cloud Run only sends traffic once /health answers (default: just an open port)
      startup_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 2
        failure_threshold = 30
      }

      dynamic "env" {
        for_each = merge(local.common_env, each.value.env)
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secret Manager values arrive as ordinary environment variables
      dynamic "env" {
        for_each = each.value.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret = google_secret_manager_secret.secrets[env.value].secret_id
              # A fixed version: env vars are read once at instance start, so
              # "latest" would make instances disagree after a rotation
              version = google_secret_manager_secret_version.secrets[env.value].version
            }
          }
        }
      }

      dynamic "env" {
        for_each = each.value.latest
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # image: CI deploys it. The minimum: the housekeeping job pre-warms web.
    # Not template[0].revision, even though gcloud sets it on every deploy. Ignored, Terraform
    # sends the old name with new settings, and Cloud Run refuses that with a 409.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling[0].min_instance_count]
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_secret_manager_secret_version.secrets]
}

# Every 5 minutes (events.tf): pre-warm web around openings, remind people to
# confirm, release unconfirmed seats after the deadline (jobs/housekeeping.py)
resource "google_cloud_run_v2_job" "housekeeping" {
  count               = local.on.schedule ? 1 : 0
  name                = "housekeeping"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = try(google_service_account.sa["housekeeping"].email, local.sa_email["housekeeping"])
      max_retries     = 0 # the next run in 5 minutes is the retry
      timeout         = "240s"

      containers {
        image = var.lab_image_tag != "" ? local.first_image["housekeeping"] : var.initial_job_image

        dynamic "env" {
          for_each = merge(local.common_env, {
            WEB_SERVICE            = try(google_cloud_run_v2_service.svc["web"].id, "projects/${var.project_id}/locations/${var.region}/services/${var.web_service_name}")
            WEB_MIN_INSTANCES      = tostring(var.web_min_instances)
            PREWARM_INSTANCES      = tostring(var.prewarm.instances)
            PREWARM_MINUTES_BEFORE = tostring(var.prewarm.minutes_before)
            PREWARM_MINUTES_AFTER  = tostring(var.prewarm.minutes_after)
          })
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
}
