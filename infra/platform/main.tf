# Self-Service Cloud Platform — control plane (D-100) and account baseline.
# Deploy:  python scripts/build_bundles.py && terraform -chdir=infra/platform apply

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "admin_email" {
  description = "First admin user (Cognito) and AWS Budgets alert recipient."
  type        = string
}

variable "create_trail" {
  description = "Create the account CloudTrail (SB-LOG-01). Set false if the account already has one (a second management-event trail is billed)."
  type        = bool
  default     = true
}

variable "existing_trail_name" {
  type    = string
  default = ""
}

variable "monthly_budget_usd" {
  description = "Platform-wide cap (D-71: $30)."
  type        = number
  default     = 30
}

locals {
  prefix     = "sscp"
  account_id = data.aws_caller_identity.current.account_id
  tags = {
    "project"    = "sscp"
    "managed-by" = "platform"
    "env-id"     = "platform"
    "owner"      = var.admin_email
    "tier"       = "PROD"
    "blueprint"  = "control-plane"
  }
  build_dir  = "${path.module}/../../build"
  trail_name = var.create_trail ? "${local.prefix}-account-trail" : var.existing_trail_name
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
