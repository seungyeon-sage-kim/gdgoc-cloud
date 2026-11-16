variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Region for everything regional (Cloud Run, buckets, Artifact Registry, Firestore)"
  type        = string
  default     = "asia-northeast3"
}

variable "github_repo_id" {
  description = "Its numeric id: gh api repos/OWNER/NAME -q .id"
  type        = string
}

variable "prewarm" {
  description = "Around each event's signup opening, the housekeeping job keeps this many web instances warm, then goes back to web_min_instances"
  type = object({
    instances      = number # CPU is the limit, not concurrency: 2 by estimate, plus one spare (docs/cold-start.md)
    minutes_before = number
    minutes_after  = number
  })
  default = { instances = 3, minutes_before = 15, minutes_after = 60 } # the job runs every 5 minutes
}

variable "web_min_instances" {
  description = "Instances of web kept warm outside openings. 0 = scale to zero (cold starts); 1+ = no cold start, billed while idle. The housekeeping job applies a change within 5 minutes"
  type        = number
  default     = 0
}

variable "signups_per_minute_per_ip" {
  description = "Signups one address may send per minute, counted on each web instance. A whole campus can share one address. A load test from one machine needs more (docs/load-test.md)"
  type        = number
  default     = 100
}

variable "worker_delay_ms" {
  description = "Extra time ticket-worker spends on each ticket, standing in for a slow email provider. 0 in production; a load test sets about 2000 so the Pub/Sub backlog lasts long enough to watch (docs/load-test.md)"
  type        = number
  default     = 0
}

variable "initial_image" {
  description = "Image used until CI pushes the real ones. CI replaces it on every deploy."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "initial_job_image" {
  description = "The job's image until CI pushes the real one. It runs and exits; a service image would keep running until the timeout, on every Scheduler run."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job"
}

variable "allow_public_web" {
  description = "Let anyone open the web service: Cloud Run skips its invoker check (run.tf). Works under domain restricted sharing; the org policy run.managed.requireInvokerIam forbids it"
  type        = bool
  default     = true
}

variable "production_extras" {
  description = "Abuse protection, monitoring, backups: what production adds beyond the lab. Off by default, so the lab is the core only"
  type        = bool
  default     = false
}

variable "admin_emails" {
  description = "Emails that may run events at /admin after signing in (they can also check people in)"
  type        = list(string)
  default     = []
}

variable "staff_emails" {
  description = "Emails that may check people in at /checkin after signing in"
  type        = list(string)
  default     = []
}

variable "alert_email" {
  description = "Where Cloud Monitoring sends alerts (empty: alerts are created without a notification)"
  type        = string
  default     = ""
}

variable "web_service_name" {
  description = "Name of web's Cloud Run service. It is part of the public address: https://NAME-PROJECT_NUMBER.REGION.run.app"
  type        = string
  default     = "web"
}

variable "extra_web_hosts" {
  description = "Other hostnames of web allowed to use the reCAPTCHA key, e.g. the web-xxxx-du.a.run.app address"
  type        = list(string)
  default     = []
}

variable "retention_days" {
  description = "Days after the event when attendee data is deleted (Firestore TTL)"
  type        = number
  default     = 30
}

variable "mail_enabled" {
  description = "Send real email through the provider. Turn on after adding its key: gcloud secrets versions add smtp-password --data-file=-"
  type        = bool
  default     = false
}

variable "smtp_host" {
  description = "The email provider's SMTP server"
  type        = string
  default     = "smtp.resend.com"
}

variable "smtp_user" {
  description = "The SMTP login. For Resend, the word resend; the password is the API key in the smtp-password secret"
  type        = string
  default     = "resend"
}

variable "contact_email" {
  description = "The address people can write to, shown in the footer and the privacy policy. Empty: none is published"
  type        = string
  default     = ""
}

variable "google_client_id" {
  description = "OAuth web client ID for Sign in with Google (Google Auth Platform in the console). Empty: no Google button"
  type        = string
  default     = ""
}
