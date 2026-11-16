terraform {
  required_version = ">= 1.7" # import and moved blocks for the lab; the IaC session imports with for_each

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State lives in a Cloud Storage bucket (./bootstrap-state.sh creates it and
  # backend.hcl names it): terraform init -backend-config=backend.hcl
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}
