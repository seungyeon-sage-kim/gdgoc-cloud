# infra

Terraform for Cloud Ticketing on Google Cloud: three Cloud Run services, one Cloud Run job,
Pub/Sub, Eventarc, Cloud Scheduler, Firestore, Cloud Storage, Secret Manager, and keyless
deploys from GitHub Actions. One root module; one Google Cloud project per deployment.

## Files

| File | What it builds |
|---|---|
| `versions.tf` | Terraform and provider versions, the `gcs` backend, the `google` provider, the project lookup |
| `main.tf` | APIs, the Artifact Registry repository, one service account per workload |
| `data.tf` | Firestore database, TTL fields and index, the assets bucket, Secret Manager secrets |
| `run.tf` | Cloud Run services `web`, `ticket-worker`, `waitlist-promoter` and the job `housekeeping` |
| `events.tf` | Pub/Sub topics and subscriptions (with dead letter), the Eventarc trigger, the Cloud Scheduler job |
| `iam.tf` | Runtime grants: Firestore, secrets, bucket, topics, invokers, the pre-warm custom role |
| `github.tf` | Workload Identity Federation for GitHub Actions, the deployer's roles, the CI Terraform account |
| `abuse.tf` | reCAPTCHA Enterprise key (`production_extras`) |
| `monitoring.tf` | Uptime check, SLOs, alert policies, log-based metrics, dashboard (`production_extras`) |
| `lab.tf` | `lab_parts`, the names of hand-built pieces, `moved` blocks for the switch to `count` |
| `variables.tf`, `outputs.tf` | Inputs and outputs |
| `tests/lab_parts.tftest.hcl` | `terraform test` with mock providers: plans production and each lab stage, no cloud calls |
| `bootstrap-state.sh` | Creates the state bucket and moves state into it (once per project) |
| `connect-github.sh` | Copies the `github_variables` output into the repository's Actions variables |
| `cleanup.sh` | `terraform destroy`, then lists billable leftovers |
| `*.example` | Templates for `terraform.tfvars` and `backend.hcl` (the real files are git-ignored) |

## Lab parts

`lab_parts = null` (the default) builds everything, as production does. In the lab, `./lab`
writes `lab_parts` to `~/.gdgoc-lab/terraform.tfvars` and turns a part on only when a
shortcut builds it or a student skips that step. A piece built by hand in the Console stays
outside Terraform until then, and `./lab` imports it before it applies.

| Part | Main resources | Built by |
|---|---|---|
| `start` | APIs, Artifact Registry repository | `./lab up`, `./lab start` |
| `database` | Firestore database `(default)` | `./lab up` |
| `base` | TTL fields, index, assets bucket, service accounts, secrets, Firestore and bucket grants | `./lab up`, `./lab base` |
| `topics` | Topics `registrations`, `emails`, `dead-letter`; subscriptions `ticket-worker` (Pull), `dead-letter` | `./lab up` |
| `web` | Cloud Run service `web`, its secret access | `./lab up` |
| `workers` | `ticket-worker`, `waitlist-promoter`, subscription `mailer`, topic and secret grants | `./lab up`, `./lab workers` |
| `push` | `ticket-worker` subscription switched to Push | by hand in the Console |
| `eventarc` | Trigger `registration-changed`, its invoker grant | `./lab up` |
| `schedule` | Job `housekeeping`, Scheduler job, its invoker grant | `./lab up` |
| `warmup` | Custom role `webScaler`, act-as on `web` | `./lab up`, `./lab warmup` |
| `trust` | Workload identity pool `github`, provider, the repository's act-as on the deployer | by hand in the Console (Task 2), or `./lab github` |
| `deployer` | Deployer's Artifact Registry, Cloud Run and act-as roles | `./lab up` |

## Where to see it in the Console

| Resource | Console page |
|---|---|
| Services and the job | Cloud Run → Services / Jobs (서비스 / 작업) |
| Topics and subscriptions | Pub/Sub → Topics / Subscriptions (주제 / 구독) |
| Eventarc trigger | Eventarc → Triggers (트리거) |
| Scheduler job | Cloud Scheduler |
| Secrets | Security → Secret Manager |
| Database, TTL, index | Firestore → Databases → `(default)` → Time-to-live / Indexes |
| Assets bucket, state bucket | Cloud Storage → Buckets (버킷) |
| Image repository | Artifact Registry → Repositories (저장소) |
| Service accounts, grants, custom role | IAM & Admin → IAM / Service accounts / Roles (IAM 및 관리자 → IAM / 서비스 계정 / 역할) |
| GitHub trust | IAM & Admin → Workload Identity Federation (워크로드 아이덴티티 제휴) |
| Alerts, SLOs, dashboard | Monitoring → Alerting / SLOs / Dashboards |

## Run it

Production (a person with Owner on the project, once; afterwards CI applies `infra/` on
pushes to `main` when `production_extras` is on):

```sh
cp terraform.tfvars.example terraform.tfvars   # project_id, github_repo_id, admin_emails, ...
gcloud auth application-default login
./bootstrap-state.sh                           # state bucket + backend.hcl + terraform init
terraform plan -out=tfplan                     # read it
terraform apply tfplan
./connect-github.sh                            # GitHub Actions variables, deploys on
```

Later runs: `terraform init -backend-config=backend.hcl`, then plan and apply.

Lab: do not run Terraform directly. `./lab` writes the settings, creates the state bucket,
imports hand-built pieces and applies (see `docs/lab.ko.md`).

Checks that need no credentials (CI runs the same):

```sh
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
terraform test
```

## Security notes

- **State contains secrets.** `ticket-secret` and `session-secret` are generated by
  `random_password` and written to Secret Manager, so their values are in the state file
  (`random_password.secret`, `google_secret_manager_secret_version.secrets`). Write-only
  attributes (`secret_data_wo`) would keep them out, but need Terraform 1.11 or later; CI
  and Cloud Shell pin 1.9.8. Changing how the values are generated would also rotate them
  and invalidate every issued ticket QR code and session, so it is left as is.
  `smtp-password` and `mail-from` are created empty; a person adds the values with
  `gcloud secrets versions add`, so they are never in state.
- **The state bucket is the boundary.** `bootstrap-state.sh` creates `PROJECT_ID-tfstate`
  with uniform bucket-level access, public access prevention and object versioning.
  Readers are project Owners and Editors, anyone holding a Cloud Storage role on the
  project or the bucket, and the CI account `tickets-terraform` (`roles/storage.admin`).
  No runtime service account has a project-level storage role; `web` and `ticket-worker`
  can only use the assets bucket. Old object versions keep old state; delete the bucket
  when the project is retired (`cleanup.sh` prints the command).
- **CI identities.** GitHub Actions signs in without keys. The provider accepts tokens only
  when `repository_id` matches `github_repo_id` and `ref` is `refs/heads/main`.
  `tickets-deployer` can push images, deploy revisions and act as the four runtime
  accounts. `tickets-terraform` (only with `production_extras`) holds near-owner roles,
  including `resourcemanager.projectIamAdmin`, so a push to `main` is admin access to the
  project; protect `main` accordingly.
- **Runtime identities.** Each workload has its own service account. Grants are on the
  bucket, topic, subscription, secret, service or job where Google allows it; Firestore
  (`roles/datastore.user`), Eventarc (`roles/eventarc.eventReceiver`) and reCAPTCHA
  (`roles/recaptchaenterprise.agent`) are project-level because those roles have no
  narrower resource. `housekeeping` holds act-as on `web`'s account, a documented exception
  in `iam.tf` that Cloud Run requires for a scaling update.
- **Network.** `web` is public by design (`invoker_iam_disabled`, not an `allUsers` grant).
  `ticket-worker` and `waitlist-promoter` accept internal traffic only and require
  `roles/run.invoker`, held by the Pub/Sub and Eventarc identities.
- **`production_extras`** adds Firestore delete protection, point-in-time recovery and daily
  backups, reCAPTCHA, monitoring, and the CI Terraform account.

## Deliberate deviations from Google's Terraform best practices

- **One root module, no `environments/` + `modules/`.** Each Google Cloud project is one
  environment; `terraform.tfvars` and `backend.hcl` choose it. A lab project and the
  production project run the same code with different tfvars. With `production_extras` the
  state holds about 125 resources, above Google's suggested 100; most are IAM members and
  monitoring objects that refresh quickly, and a split would mean two states per project.
- **`count` for on/off parts.** Google recommends `count` for conditional resources; the
  lab parts and `production_extras` use it, and `for_each` is used for collections.
  The 33 `moved` blocks in `lab.tf` (and 3 in `iam.tf`) keep production's existing
  resources when they became conditional or were renamed.
- **Resource names kept as deployed.** A few names predate the naming rules
  (`google_project_service.apis`, `google_service_account.sa`,
  `google_cloud_run_v2_service.svc`, `google_secret_manager_secret.secrets`). `./lab` imports
  by these addresses, so a rename needs `moved` blocks and a matching change in `./lab`.
- **`data.tf` holds data stores** (Firestore, Cloud Storage, Secret Manager), not Terraform
  data sources; the only data source, `google_project`, is in `versions.tf`.
- **Helper scripts in `infra/`**, not `helpers/`, so `./lab` and the docs keep one path.
- **No extra labels.** Each project holds only this application, so billing per project
  already separates the cost; the provider adds `goog-terraform-provisioned` to what it
  creates.
