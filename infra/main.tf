# APIs, the image registry, and one service account per job.

locals {
  core_apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "eventarc.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ]
  extra_apis = ["recaptchaenterprise.googleapis.com", "monitoring.googleapis.com"]
  # ./lab check reads the budget through this API
  lab_apis = local.lab ? ["billingbudgets.googleapis.com"] : []
  apis     = concat(local.core_apis, var.production_extras ? local.extra_apis : [], local.lab_apis)

  # Each service runs as its own identity and gets only the roles it needs (iam.tf)
  service_accounts = {
    web               = "web: public signup service"
    ticket-worker     = "ticket-worker: issues tickets from Pub/Sub"
    waitlist-promoter = "waitlist-promoter: gives freed seats to the waitlist"
    housekeeping      = "housekeeping: pre-warm, reminders and releases, every 5 minutes"
    pubsub-push       = "Identity Pub/Sub uses to call ticket-worker"
    eventarc          = "Identity Eventarc uses to call waitlist-promoter"
    scheduler         = "Identity Cloud Scheduler uses to start housekeeping"
    deployer          = "GitHub Actions: push images and deploy"
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  count         = local.on.start ? 1 : 0
  repository_id = "gdgoc-tickets"
  location      = var.region
  format        = "DOCKER"
  description   = "Images built by GitHub Actions"

  # Keep the bill small: delete images older than 30 days, except the 10 newest
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s"
    }
  }
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "sa" {
  for_each     = local.on.base ? local.service_accounts : {}
  account_id   = "tickets-${each.key}"
  display_name = each.value
}
