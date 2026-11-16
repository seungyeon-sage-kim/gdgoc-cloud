# Keyless deploys from GitHub Actions (Workload Identity Federation).
# GitHub issues a short-lived token for each workflow run; Google Cloud trusts it
# only when it comes from this repository's main branch, and swaps it for the
# deployer's access. The repository is matched by its numeric id, not its name:
# a name can be deleted and registered again by someone else, an id cannot.
#
# In the lab, students build the trust by hand (step 15, part "trust") and
# ./lab deployer adds the deployer's roles (part "deployer").

resource "google_iam_workload_identity_pool" "github" {
  count                     = local.on.trust ? 1 : 0
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = local.on.trust ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "gdgoc-tickets"
  display_name                       = "gdgoc-tickets repository"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository"    = "assertion.repository"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.ref"           = "assertion.ref"
  }
  attribute_condition = "assertion.repository_id == '${var.github_repo_id}' && assertion.ref == 'refs/heads/main'"
}

resource "google_service_account_iam_member" "github_is_deployer" {
  count              = local.on.trust ? 1 : 0
  service_account_id = try(google_service_account.sa["deployer"].name, local.sa_name["deployer"])
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository_id/${var.github_repo_id}"
}

# What the deployer may do: push images, deploy new revisions, and run them as
# the runtime identities. It cannot change IAM, buckets, topics or secrets.
resource "google_artifact_registry_repository_iam_member" "deployer_pushes" {
  count      = local.on.deployer ? 1 : 0
  repository = try(google_artifact_registry_repository.images[0].name, "projects/${var.project_id}/locations/${var.region}/repositories/gdgoc-tickets")
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = local.sa["deployer"]
}

resource "google_project_iam_member" "deployer_deploys" {
  count   = local.on.deployer ? 1 : 0
  project = var.project_id
  role    = "roles/run.developer"
  member  = local.sa["deployer"]
}

resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each           = local.on.deployer ? toset(["web", "ticket-worker", "waitlist-promoter", "housekeeping"]) : []
  service_account_id = google_service_account.sa[each.value].name
  role               = "roles/iam.serviceAccountUser"
  member             = local.sa["deployer"]
}

# Terraform in CI, with production_extras: a push that changes infra/ applies it
# (scripts/infra.sh). The deployer can't, so this is a separate account, used by
# that one job. A lab runs terraform apply by hand and doesn't get one.
#
# Applying everything in infra/ takes these roles, and they are close to project
# owner: projectIamAdmin alone lets the account grant itself more. So a push to
# main is admin access to this project, which is why the login above accepts only
# this repository's main branch. The list leaves out deleting the project.
locals {
  terraform_roles = var.production_extras ? toset([
    "roles/artifactregistry.admin",
    "roles/cloudscheduler.admin",
    "roles/datastore.owner",
    "roles/eventarc.admin",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/logging.configWriter",
    "roles/monitoring.admin",
    "roles/pubsub.admin",
    "roles/recaptchaenterprise.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin", # also Terraform's state, in the bucket bootstrap-state.sh made
  ]) : toset([])
}

resource "google_service_account" "terraform" {
  count        = var.production_extras ? 1 : 0
  account_id   = "tickets-terraform"
  display_name = "GitHub Actions: apply infra/"
}

resource "google_service_account_iam_member" "github_is_terraform" {
  count              = var.production_extras ? 1 : 0
  service_account_id = google_service_account.terraform[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${try(google_iam_workload_identity_pool.github[0].name, local.pool)}/attribute.repository_id/${var.github_repo_id}"
}

resource "google_project_iam_member" "terraform" {
  for_each = local.terraform_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.terraform[0].email}"
}
