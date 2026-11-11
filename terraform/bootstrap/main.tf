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
  region = "us-east-2"

  default_tags {
    tags = {
      Project = "telemetry-monitor"
    }
  }
}

variable "alert_email" {
  description = "Address that receives the budget alerts."
  type        = string
}

# Tripwires only: the Free plan cannot bill, so these catch an accidental upgrade.
resource "aws_budgets_budget" "tripwire" {
  for_each = {
    "zero-spend" = "0.01"
    "one-dollar" = "1"
  }

  name         = "telemetry-monitor-${each.key}"
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = each.value
  limit_unit   = "USD"

  # Credits, refunds and discounts net against spend and would hide the first charge.
  cost_types {
    include_credit   = false
    include_refund   = false
    include_discount = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
