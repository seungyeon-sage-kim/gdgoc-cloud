# The lab's parts. Production builds everything (lab_parts = null). In the lab, students
# build most pieces by hand in the Console, and ./lab adds a part to lab_parts only for a
# shortcut (the paperwork) or a skipped step. A piece a student builds by hand is not in
# Terraform until then, so Terraform never fixes a student's mistake behind their back.
#
# Each part, the lab step it belongs to (docs/lab.ko.md), and who builds it:
#
#   start     1   APIs, the image registry                         ./lab start
#   database  2   the Firestore database                           by hand
#   base      3   expiry rules, index, bucket, service accounts,   ./lab base
#                 secrets, the project and bucket roles
#   topics    4   topics, the ticket-worker subscription (Pull),   by hand
#                 the dead-letter subscription and its grants
#   web       5   the web service, its secret access               by hand
#   workers   6   ticket-worker, waitlist-promoter, mailer,        ./lab workers
#                 the topic and secret grants, Pub/Sub's push identity may call ticket-worker
#   push      8   ticket-worker's subscription switched to Push    by hand
#   eventarc  11  the Eventarc trigger, its may-call grant         by hand
#   schedule  12  the housekeeping job, the Scheduler, its grant   by hand
#   warmup    13  the custom role and act-as for pre-warming       ./lab warmup
#   trust     15  GitHub's keyless sign-in: pool, provider, the    by hand
#                 repository may act as the deployer
#   deployer  15  the deployer's three roles                       ./lab deployer

variable "lab_parts" {
  description = "The parts Terraform builds (infra/lab.tf). null builds everything, as production does; ./lab sets it"
  type        = set(string)
  default     = null

  validation {
    condition     = var.lab_parts == null ? true : length(setsubtract(var.lab_parts, ["start", "database", "base", "topics", "web", "workers", "push", "eventarc", "schedule", "warmup", "trust", "deployer"])) == 0
    error_message = "lab_parts may only name the parts listed in infra/lab.tf."
  }
}

variable "lab_image_tag" {
  description = "Tag of the images ./lab start built with Cloud Build. Empty: services start on initial_image until CI deploys"
  type        = string
  default     = ""
}

locals {
  lab_part_names = ["start", "database", "base", "topics", "web", "workers", "push", "eventarc", "schedule", "warmup", "trust", "deployer"]
  lab            = var.lab_parts != null
  on             = { for part in local.lab_part_names : part => contains(local.lab ? var.lab_parts : local.lab_part_names, part) }

  # Names of pieces a student may have built by hand, so the parts Terraform builds can
  # refer to them without owning them. Each matches what the resource itself reports.
  number   = data.google_project.this.number
  registry = "${var.region}-docker.pkg.dev/${var.project_id}/gdgoc-tickets"
  topic_id = { for name in ["registrations", "emails", "dead-letter"] : name => "projects/${var.project_id}/topics/${name}" }
  subscription_id = {
    for name in ["ticket-worker", "dead-letter", "mailer"] : name => "projects/${var.project_id}/subscriptions/${name}"
  }
  sa_email = { for name in keys(local.service_accounts) : name => "tickets-${name}@${var.project_id}.iam.gserviceaccount.com" }
  sa_name  = { for name, email in local.sa_email : name => "projects/${var.project_id}/serviceAccounts/${email}" }
  run_url  = { for name in ["ticket-worker", "waitlist-promoter"] : name => "https://${name}-${local.number}.${var.region}.run.app" }
  pool     = "projects/${local.number}/locations/global/workloadIdentityPools/github"
}

# These resources became optional parts (count), so their address gained [0]. Without
# these blocks, Terraform would delete production's and create new ones.
moved {
  from = google_artifact_registry_repository.images
  to   = google_artifact_registry_repository.images[0]
}

moved {
  from = google_firestore_database.main
  to   = google_firestore_database.main[0]
}

moved {
  from = google_storage_bucket.assets
  to   = google_storage_bucket.assets[0]
}

moved {
  from = google_secret_manager_secret.smtp_password
  to   = google_secret_manager_secret.smtp_password[0]
}

moved {
  from = google_secret_manager_secret.mail_from
  to   = google_secret_manager_secret.mail_from[0]
}

moved {
  from = google_firestore_index.waitlist
  to   = google_firestore_index.waitlist[0]
}

moved {
  from = google_pubsub_topic.registrations
  to   = google_pubsub_topic.registrations[0]
}

moved {
  from = google_pubsub_topic.dead_letter
  to   = google_pubsub_topic.dead_letter[0]
}

moved {
  from = google_pubsub_topic.emails
  to   = google_pubsub_topic.emails[0]
}

moved {
  from = google_pubsub_subscription.dead_letter
  to   = google_pubsub_subscription.dead_letter[0]
}

moved {
  from = google_pubsub_subscription.ticket_worker
  to   = google_pubsub_subscription.ticket_worker[0]
}

moved {
  from = google_pubsub_subscription.mailer
  to   = google_pubsub_subscription.mailer[0]
}

moved {
  from = google_eventarc_trigger.registration_changed
  to   = google_eventarc_trigger.registration_changed[0]
}

moved {
  from = google_cloud_scheduler_job.housekeeping
  to   = google_cloud_scheduler_job.housekeeping[0]
}

moved {
  from = google_cloud_run_v2_job.housekeeping
  to   = google_cloud_run_v2_job.housekeeping[0]
}

moved {
  from = google_iam_workload_identity_pool.github
  to   = google_iam_workload_identity_pool.github[0]
}

moved {
  from = google_iam_workload_identity_pool_provider.github
  to   = google_iam_workload_identity_pool_provider.github[0]
}

moved {
  from = google_service_account_iam_member.github_is_deployer
  to   = google_service_account_iam_member.github_is_deployer[0]
}

moved {
  from = google_artifact_registry_repository_iam_member.deployer_pushes
  to   = google_artifact_registry_repository_iam_member.deployer_pushes[0]
}

moved {
  from = google_project_iam_member.deployer_deploys
  to   = google_project_iam_member.deployer_deploys[0]
}

moved {
  from = google_secret_manager_secret_iam_member.worker_reads_smtp_password
  to   = google_secret_manager_secret_iam_member.worker_reads_smtp_password[0]
}

moved {
  from = google_secret_manager_secret_iam_member.worker_reads_mail_from
  to   = google_secret_manager_secret_iam_member.worker_reads_mail_from[0]
}

moved {
  from = google_storage_bucket_iam_member.web_uses_assets
  to   = google_storage_bucket_iam_member.web_uses_assets[0]
}

moved {
  from = google_storage_bucket_iam_member.worker_writes_tickets
  to   = google_storage_bucket_iam_member.worker_writes_tickets[0]
}

moved {
  from = google_pubsub_subscription_iam_member.web_reads_dead_letters
  to   = google_pubsub_subscription_iam_member.web_reads_dead_letters[0]
}

moved {
  from = google_cloud_run_v2_service_iam_member.pubsub_invokes_worker
  to   = google_cloud_run_v2_service_iam_member.pubsub_invokes_worker[0]
}

moved {
  from = google_pubsub_topic_iam_member.agent_publishes_dead_letters
  to   = google_pubsub_topic_iam_member.agent_publishes_dead_letters[0]
}

moved {
  from = google_project_iam_member.eventarc_receiver
  to   = google_project_iam_member.eventarc_receiver[0]
}

moved {
  from = google_cloud_run_v2_service_iam_member.eventarc_invokes_promoter
  to   = google_cloud_run_v2_service_iam_member.eventarc_invokes_promoter[0]
}

moved {
  from = google_cloud_run_v2_job_iam_member.scheduler_runs_housekeeping
  to   = google_cloud_run_v2_job_iam_member.scheduler_runs_housekeeping[0]
}

moved {
  from = google_project_iam_custom_role.web_scaler
  to   = google_project_iam_custom_role.web_scaler[0]
}

moved {
  from = google_cloud_run_v2_service_iam_member.housekeeping_updates_web
  to   = google_cloud_run_v2_service_iam_member.housekeeping_updates_web[0]
}

moved {
  from = google_service_account_iam_member.housekeeping_acts_as_web
  to   = google_service_account_iam_member.housekeeping_acts_as_web[0]
}
