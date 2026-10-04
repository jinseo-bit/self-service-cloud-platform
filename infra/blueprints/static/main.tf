# Static Blueprint (D-31): private S3 origin + CloudFront (OAC) + baseline.
# Root module executed by the platform runner, one state per environment.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {}
}

variable "env_id" {
  type = string
  validation {
    condition     = can(regex("^env-[a-z0-9]{8}$", var.env_id))
    error_message = "env_id must look like env-xxxxxxxx."
  }
}

variable "account_id" {
  type = string
}

variable "owner" {
  type = string
}

variable "tier" {
  type = string
  validation {
    condition     = contains(["DEV", "PROD"], var.tier)
    error_message = "tier must be DEV or PROD."
  }
}

variable "expires_at" {
  description = "ISO-8601 TTL for DEV, empty for PROD (D-63)."
  type        = string
  default     = ""
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "log_bucket" {
  description = "Shared access-log bucket name created by the platform bootstrap."
  type        = string
}

variable "alert_email" {
  description = "PROD alarm recipient (environment owner)."
  type        = string
  default     = ""
}

variable "spa_fallback" {
  type    = bool
  default = false
}

variable "ip_allowlist" {
  type    = list(string)
  default = []
}

locals {
  name = "sscp-${var.env_id}"
  tags = merge(
    {
      "project"    = "sscp"
      "managed-by" = "platform"
      "env-id"     = var.env_id
      "owner"      = var.owner
      "tier"       = var.tier
      "blueprint"  = "static"
    },
    var.expires_at == "" ? {} : { "expires-at" = var.expires_at },
  )
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

module "origin" {
  source      = "../../modules/s3-site-origin"
  bucket_name = "${local.name}-${var.account_id}"
}

module "edge" {
  source            = "../../modules/edge-cloudfront"
  providers         = { aws.us_east_1 = aws.us_east_1 }
  name              = local.name
  s3_origin_domain  = module.origin.bucket_regional_domain_name
  log_bucket_domain = "${var.log_bucket}.s3.amazonaws.com"
  log_prefix        = "envs/${var.env_id}/cloudfront/"
  spa_fallback      = var.spa_fallback
  ip_allowlist      = var.ip_allowlist
}

module "monitoring" {
  count           = var.tier == "PROD" ? 1 : 0
  source          = "../../modules/observability-cloudfront"
  providers       = { aws.us_east_1 = aws.us_east_1 }
  name            = local.name
  distribution_id = module.edge.distribution_id
  alert_email     = var.alert_email
}

locals {
  bucket_arn = "arn:aws:s3:::${local.name}-${var.account_id}"
}

# SB-PUB-01: only this distribution (OAC, SourceArn) may read. SB-TLS-04: deny non-TLS.
resource "aws_s3_bucket_policy" "origin" {
  bucket = module.origin.bucket_id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${local.bucket_arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = module.edge.distribution_arn } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [module.origin]
}

output "bucket_name" {
  value = module.origin.bucket_id
}

output "distribution_id" {
  value = module.edge.distribution_id
}

output "distribution_domain" {
  value = module.edge.domain_name
}

output "url" {
  value = "https://${module.edge.domain_name}"
}

output "alarm_names" {
  value = var.tier == "PROD" ? module.monitoring[0].alarm_names : []
}

output "alert_topic_arn" {
  value = var.tier == "PROD" ? module.monitoring[0].topic_arn : null
}

output "web_acl_arn" {
  value = module.edge.web_acl_arn
}
