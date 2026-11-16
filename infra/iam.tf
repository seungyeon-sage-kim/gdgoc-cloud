# Who may do what. Every grant is as narrow as possible: on one bucket, one
# topic or one service instead of the whole project, wherever Google allows it.
#
# In the lab, students make some of these grants by hand: web's secret access (step 5),
# the dead-letter grants (step 4), the may-call grants for Eventarc and the Scheduler
# (steps 11, 12) and GitHub's trust (step 15). Each is in that step's part (lab.tf), so
# Terraform adds it only when the step is skipped.

locals {
  # As members of a grant. Through the resource when Terraform made it, so a grant waits for its account
  sa = { for k, email in local.sa_email : k => "serviceAccount:${try(google_service_account.sa[k].email, email)}" }

  # Google-managed identities that act on our behalf
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  # Which runtime identity may read which secret, and the lab part that grants it
  secret_access = {
    "web/ticket-secret"           = { sa = "web", secret = "ticket-secret", part = "web" }
    "web/session-secret"          = { sa = "web", secret = "session-secret", part = "web" }
    "ticket-worker/ticket-secret" = { sa = "ticket-worker", secret = "ticket-secret", part = "workers" }
  }
}

# ---------- Firestore: every runtime service reads and writes registrations ----------

resource "google_project_iam_member" "firestore" {
  for_each = local.on.base ? toset(["web", "ticket-worker", "waitlist-promoter", "housekeeping"]) : []
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = local.sa[each.value]
}

# ---------- Secrets ----------

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = { for k, v in local.secret_access : k => v if local.on[v.part] }
  secret_id = google_secret_manager_secret.secrets[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa[each.value.sa]
}

# Only the service that sends email may read the email provider's key
resource "google_secret_manager_secret_iam_member" "worker_reads_smtp_password" {
  count     = local.on.workers ? 1 : 0
  secret_id = try(google_secret_manager_secret.smtp_password[0].id, "projects/${var.project_id}/secrets/smtp-password")
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa["ticket-worker"]
}

resource "google_secret_manager_secret_iam_member" "worker_reads_mail_from" {
  count     = local.on.workers ? 1 : 0
  secret_id = try(google_secret_manager_secret.mail_from[0].id, "projects/${var.project_id}/secrets/mail-from")
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa["ticket-worker"]
}

# ---------- Buckets ----------

resource "google_storage_bucket_iam_member" "web_uses_assets" {
  count  = local.on.base ? 1 : 0
  bucket = google_storage_bucket.assets[0].name
  role   = "roles/storage.objectUser" # serves QR codes, writes posters
  member = local.sa["web"]
}

resource "google_storage_bucket_iam_member" "worker_writes_tickets" {
  count  = local.on.base ? 1 : 0
  bucket = google_storage_bucket.assets[0].name
  role   = "roles/storage.objectUser" # read, write, overwrite
  member = local.sa["ticket-worker"]
}

# ---------- Pub/Sub ----------

resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = local.on.workers ? toset(["web", "waitlist-promoter", "housekeeping"]) : [] # a new signup, a promoted signup, a repair
  topic    = try(google_pubsub_topic.registrations[0].id, local.topic_id["registrations"])
  role     = "roles/pubsub.publisher"
  member   = local.sa[each.value]
}

resource "google_pubsub_topic_iam_member" "email_requesters" {
  for_each = local.on.workers ? toset(["web", "ticket-worker", "housekeeping"]) : [] # sign-in codes, 신청 완료, reminders
  topic    = try(google_pubsub_topic.emails[0].id, local.topic_id["emails"])
  role     = "roles/pubsub.publisher"
  member   = local.sa[each.value]
}

# The admin page peeks at failed messages (and hands them straight back)
resource "google_pubsub_subscription_iam_member" "web_reads_dead_letters" {
  count        = local.on.workers ? 1 : 0
  subscription = try(google_pubsub_subscription.dead_letter[0].id, local.subscription_id["dead-letter"])
  role         = "roles/pubsub.subscriber"
  member       = local.sa["web"]
}

# Pub/Sub's push identity may call ticket-worker. In the lab it comes with the workers
# (step 6), because mailer delivers sign-in codes through it before step 8.
resource "google_cloud_run_v2_service_iam_member" "pubsub_invokes_worker" {
  count    = local.on.workers ? 1 : 0
  name     = google_cloud_run_v2_service.svc["ticket-worker"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = local.sa["pubsub-push"]
}

# Pub/Sub itself moves failed messages to the dead-letter topic
resource "google_pubsub_topic_iam_member" "agent_publishes_dead_letters" {
  count  = local.on.topics ? 1 : 0
  topic  = google_pubsub_topic.dead_letter[0].id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "agent_acks_dead_letters" {
  for_each = merge(
    local.on.topics ? { ticket-worker = try(google_pubsub_subscription.ticket_worker[0].id, local.subscription_id["ticket-worker"]) } : {},
    local.on.workers ? { mailer = try(google_pubsub_subscription.mailer[0].id, local.subscription_id["mailer"]) } : {},
  )
  subscription = each.value
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

# ---------- Eventarc ----------

resource "google_project_iam_member" "eventarc_receiver" {
  count   = local.on.workers ? 1 : 0
  project = var.project_id
  role    = "roles/eventarc.eventReceiver"
  member  = local.sa["eventarc"]
}

resource "google_cloud_run_v2_service_iam_member" "eventarc_invokes_promoter" {
  count    = local.on.eventarc ? 1 : 0
  name     = try(google_cloud_run_v2_service.svc["waitlist-promoter"].name, "waitlist-promoter")
  location = var.region
  role     = "roles/run.invoker"
  member   = local.sa["eventarc"]
}

# ---------- Scheduler ----------

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_housekeeping" {
  count    = local.on.schedule ? 1 : 0
  name     = google_cloud_run_v2_job.housekeeping[0].name
  location = var.region
  role     = "roles/run.invoker" # includes starting a job execution
  member   = local.sa["scheduler"]
}

# Pre-warm: housekeeping changes web's minimum instances. Cloud Run has no permission
# per field, so this is read and update on this one service.
resource "google_project_iam_custom_role" "web_scaler" {
  count       = local.on.warmup ? 1 : 0
  role_id     = "webScaler"
  title       = "Change web's scaling"
  description = "Read and update one Cloud Run service, without deploying as its service account"
  permissions = ["run.services.get", "run.services.update"]
}

resource "google_cloud_run_v2_service_iam_member" "housekeeping_updates_web" {
  count    = local.on.warmup ? 1 : 0
  name     = try(google_cloud_run_v2_service.svc["web"].name, var.web_service_name)
  location = var.region
  role     = google_project_iam_custom_role.web_scaler[0].id
  member   = local.sa["housekeeping"]
}

# A deliberate exception to least privilege. Cloud Run asks for actAs on every service
# update, even one that changes only scaling (tested: docs/cold-start.md), and actAs
# also allows deploying as web. It is limited to this one job and this one account.
resource "google_service_account_iam_member" "housekeeping_acts_as_web" {
  count              = local.on.warmup ? 1 : 0
  service_account_id = try(google_service_account.sa["web"].name, local.sa_name["web"])
  role               = "roles/iam.serviceAccountUser"
  member             = local.sa["housekeeping"]
}

# Renamed grants: tell Terraform they are the same, so it renames them instead of
# creating the new one and then deleting the old one (which is the same grant)
moved {
  from = google_pubsub_topic_iam_member.web_publishes
  to   = google_pubsub_topic_iam_member.publishers["web"]
}

moved {
  from = google_storage_bucket_iam_member.writers_assets["ticket-worker"]
  to   = google_storage_bucket_iam_member.worker_writes_tickets
}

moved {
  from = google_pubsub_subscription_iam_member.agent_acks_dead_letters
  to   = google_pubsub_subscription_iam_member.agent_acks_dead_letters["ticket-worker"]
}
