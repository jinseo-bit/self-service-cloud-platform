# Deliberately non-compliant configuration for negative tests of the PLAN validator.
# Never applied: tests only run an offline `terraform plan` against it.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region                      = "ap-northeast-2"
  access_key                  = "offline"
  secret_key                  = "offline"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

# no BPA, no SSE, no tags
resource "aws_s3_bucket" "site" {
  bucket = "violating-site-bucket"
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = false
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# public read, no TLS deny
resource "aws_s3_bucket_policy" "site" {
  bucket = "violating-site-bucket"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicRead"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "arn:aws:s3:::violating-site-bucket/*"
    }]
  })
}

# allow-all viewer, no headers policy, no logging, no WAF
resource "aws_cloudfront_distribution" "site" {
  enabled = true
  origin {
    origin_id   = "s3"
    domain_name = "violating-site-bucket.s3.ap-northeast-2.amazonaws.com"
  }
  default_cache_behavior {
    target_origin_id       = "s3"
    viewer_protocol_policy = "allow-all"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6"
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

resource "aws_nat_gateway" "nat" {
  subnet_id     = "subnet-00000000000000000"
  allocation_id = "eipalloc-00000000000000000"
}

resource "aws_security_group" "open" {
  name   = "open"
  vpc_id = "vpc-00000000000000000"
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_policy" "admin" {
  name = "too-broad"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "*", Resource = "*" }]
  })
}

resource "aws_lambda_function_url" "url" {
  function_name      = "whatever"
  authorization_type = "NONE"
}

resource "aws_cloudwatch_log_group" "forever" {
  name = "/sscp/forever"
}
