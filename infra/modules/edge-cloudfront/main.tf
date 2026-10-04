# CloudFront edge in front of an S3 origin (OAC).
# Baseline: SB-TLS-01 (redirect-to-https), SB-TLS-05 (managed SecurityHeadersPolicy),
# SB-LOG-02 (standard access logs), SB-PUB-08 (WAF IP allowlist when requested).
# Uses the default *.cloudfront.net certificate (D-42); see SB-EXC-02.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

variable "name" {
  type = string
}

variable "s3_origin_domain" {
  type = string
}

variable "log_bucket_domain" {
  description = "Shared access-log bucket domain (<bucket>.s3.amazonaws.com)."
  type        = string
}

variable "log_prefix" {
  type = string
}

variable "spa_fallback" {
  type    = bool
  default = false
}

variable "ip_allowlist" {
  description = "Empty = public. Non-empty = WAF allows only these CIDRs (SB-PUB-08)."
  type        = list(string)
  default     = []
}

variable "price_class" {
  type    = string
  default = "PriceClass_200"
}

locals {
  # AWS managed policies (stable, documented IDs)
  cache_policy_caching_optimized   = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  response_headers_security_policy = "67f7725c-6f97-4210-82d7-5512b31e9d03"
  origin_id                        = "s3-origin"
  use_waf                          = length(var.ip_allowlist) > 0
}

resource "aws_cloudfront_origin_access_control" "this" {
  name                              = var.name
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_wafv2_ip_set" "allow" {
  count              = local.use_waf ? 1 : 0
  provider           = aws.us_east_1
  name               = "${var.name}-allow"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = var.ip_allowlist
}

resource "aws_wafv2_web_acl" "this" {
  count    = local.use_waf ? 1 : 0
  provider = aws.us_east_1
  name     = var.name
  scope    = "CLOUDFRONT"

  default_action {
    block {}
  }

  rule {
    name     = "allow-listed-ips"
    priority = 0
    action {
      allow {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.allow[0].arn
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-allow"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  comment             = var.name
  default_root_object = "index.html"
  price_class         = var.price_class
  http_version        = "http2and3"
  web_acl_id          = local.use_waf ? aws_wafv2_web_acl.this[0].arn : null

  origin {
    origin_id                = local.origin_id
    domain_name              = var.s3_origin_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  default_cache_behavior {
    target_origin_id           = local.origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_caching_optimized
    response_headers_policy_id = local.response_headers_security_policy
  }

  dynamic "custom_error_response" {
    for_each = var.spa_fallback ? [403, 404] : []
    content {
      error_code            = custom_error_response.value
      response_code         = 200
      response_page_path    = "/index.html"
      error_caching_min_ttl = 10
    }
  }

  logging_config {
    bucket          = var.log_bucket_domain
    prefix          = var.log_prefix
    include_cookies = false
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

output "distribution_id" {
  value = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  value = aws_cloudfront_distribution.this.arn
}

output "domain_name" {
  value = aws_cloudfront_distribution.this.domain_name
}

output "web_acl_arn" {
  value = local.use_waf ? aws_wafv2_web_acl.this[0].arn : null
}
