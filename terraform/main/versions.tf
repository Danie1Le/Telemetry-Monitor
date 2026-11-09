terraform {
  required_version = "~> 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project = "telemetry-monitor"
    }
  }
}

variable "alert_email" {
  description = "Address that receives Alert and ops emails."
  type        = string
}
