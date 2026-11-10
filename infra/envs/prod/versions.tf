terraform {
  required_version = ">= 1.5.7, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "~> 3.0"
    }
  }

  # The state bucket is supplied at init time, never committed:
  #   terraform init -backend-config="bucket=<state-bucket>"
  backend "gcs" {
    prefix = "envs/prod"
  }
}
