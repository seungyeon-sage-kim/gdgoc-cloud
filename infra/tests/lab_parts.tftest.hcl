# Plans the configuration for each stage of the lab against a pretend Google Cloud,
# so a part that refers to a piece students build by hand fails here, not in class.
#   cd infra && terraform init -backend=false && terraform test

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
}

mock_provider "random" {}

# Every setting is named here, so a terraform.tfvars in infra/ changes nothing
variables {
  project_id        = "student-project"
  github_repo_id    = "1000"
  admin_emails      = ["student@example.com"]
  staff_emails      = []
  alert_email       = ""
  extra_web_hosts   = []
  google_client_id  = ""
  web_service_name  = "web"
  mail_enabled      = false
  production_extras = false
  lab_image_tag     = "lab"
}

run "production_builds_everything" {
  command = plan
  variables {
    production_extras = true
  }
  assert {
    condition     = length(google_cloud_run_v2_service.svc) == 3 && length(google_cloud_run_v2_job.housekeeping) == 1
    error_message = "Production should build all three services and the job."
  }
  assert {
    condition     = length(google_pubsub_subscription.ticket_worker[0].push_config) == 1
    error_message = "Production's ticket-worker subscription should push."
  }
  assert {
    condition     = google_pubsub_subscription.dead_letter[0].ack_deadline_seconds == 60
    error_message = "A pulled but unacked dead letter should come back within a minute."
  }
}

run "lab_start" {
  command = plan
  variables {
    lab_parts = ["start"]
  }
  assert {
    condition     = length(google_artifact_registry_repository.images) == 1 && length(google_service_account.sa) == 0
    error_message = "start builds the registry and nothing students build later."
  }
  assert {
    condition     = contains(keys(google_project_service.apis), "billingbudgets.googleapis.com")
    error_message = "./lab check 0 reads the budget."
  }
}

run "lab_base_after_firestore_by_hand" {
  command = plan
  variables {
    lab_parts = ["start", "base"]
  }
  assert {
    condition     = length(google_firestore_database.main) == 0 && length(google_firestore_field.ttl) == 5
    error_message = "base adds the expiry rules to the database students made, without owning it."
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.access) == 0
    error_message = "web's secret access is the student's step 5 grant."
  }
}

run "lab_workers_after_topics_and_web_by_hand" {
  command = plan
  variables {
    lab_parts = ["start", "base", "workers"]
  }
  assert {
    condition     = toset(keys(google_cloud_run_v2_service.svc)) == toset(["ticket-worker", "waitlist-promoter"])
    error_message = "workers deploys the two workers, not web."
  }
  assert {
    condition     = google_cloud_run_v2_service.svc["ticket-worker"].template[0].containers[0].image == "asia-northeast3-docker.pkg.dev/student-project/gdgoc-tickets/ticket-worker:lab"
    error_message = "The workers start on the images Cloud Build made."
  }
  assert {
    condition     = google_pubsub_subscription.mailer[0].topic == "projects/student-project/topics/emails"
    error_message = "mailer subscribes to the emails topic the student made."
  }
  assert {
    condition     = toset(keys(google_secret_manager_secret_iam_member.access)) == toset(["ticket-worker/ticket-secret"])
    error_message = "workers grants only ticket-worker's secret."
  }
  assert {
    condition     = length(google_pubsub_subscription.ticket_worker) == 0 && length(google_eventarc_trigger.registration_changed) == 0
    error_message = "Hand-built pieces stay out of Terraform."
  }
}

run "lab_skip_topics_makes_pull" {
  command = plan
  variables {
    lab_parts = ["start", "base", "topics"]
  }
  assert {
    condition     = length(google_pubsub_subscription.ticket_worker[0].push_config) == 0
    error_message = "Step 4 makes a Pull subscription."
  }
}

run "lab_skip_push" {
  command = plan
  variables {
    lab_parts = ["start", "base", "topics", "workers", "push"]
  }
  assert {
    condition     = google_pubsub_subscription.ticket_worker[0].push_config[0].oidc_token[0].service_account_email == "tickets-pubsub-push@student-project.iam.gserviceaccount.com"
    error_message = "Step 8 signs each push as tickets-pubsub-push."
  }
}

run "lab_warmup_with_web_by_hand" {
  command = plan
  variables {
    lab_parts = ["start", "base", "workers", "warmup"]
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.housekeeping_updates_web[0].name == "web"
    error_message = "warmup applies to the web service the student deployed."
  }
}

run "lab_skip_everything" {
  command = plan
  variables {
    lab_parts = ["start", "database", "base", "topics", "web", "workers", "push", "eventarc", "schedule", "warmup", "trust", "deployer"]
  }
  assert {
    condition     = google_cloud_run_v2_job.housekeeping[0].template[0].template[0].containers[0].image == "asia-northeast3-docker.pkg.dev/student-project/gdgoc-tickets/housekeeping:lab"
    error_message = "The job starts on its Cloud Build image."
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.github[0].attribute_condition == "assertion.repository_id == '1000' && assertion.ref == 'refs/heads/main'"
    error_message = "Only the student's repository, only main."
  }
}

run "lab_deployer_with_trust_by_hand" {
  command = plan
  variables {
    lab_parts = ["start", "base", "workers", "warmup", "deployer"]
  }
  assert {
    condition     = output.github_variables.WIF_PROVIDER == "projects/123456789012/locations/global/workloadIdentityPools/github/providers/gdgoc-tickets"
    error_message = "With a hand-built trust, the provider's name follows the names in the sheet."
  }
  assert {
    condition     = length(google_service_account_iam_member.github_is_deployer) == 0
    error_message = "The repository's act-as on the deployer is the student's grant."
  }
}
