terraform {
  required_version = "~> 1.9.0"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.47.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.47.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.13.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

provider "github" {
  owner = local.github_owner
}
