# Web UI hosting — dogfoods the same modules as the Static blueprint.

module "ui_origin" {
  source        = "../modules/s3-site-origin"
  bucket_name   = "${local.prefix}-ui-${local.account_id}"
  force_destroy = true
}

module "ui_edge" {
  source            = "../modules/edge-cloudfront"
  providers         = { aws.us_east_1 = aws.us_east_1 }
  name              = "${local.prefix}-ui"
  s3_origin_domain  = module.ui_origin.bucket_regional_domain_name
  log_bucket_domain = aws_s3_bucket.logs.bucket_domain_name
  log_prefix        = "platform/ui/"
  spa_fallback      = true
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = module.ui_origin.bucket_id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${module.ui_origin.bucket_arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = module.ui_edge.distribution_arn } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [module.ui_origin.bucket_arn, "${module.ui_origin.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

locals {
  web_dir = "${path.module}/../../web"
  mime = {
    html = "text/html; charset=utf-8", js = "application/javascript; charset=utf-8",
    css  = "text/css; charset=utf-8", svg = "image/svg+xml", json = "application/json", ico = "image/x-icon"
  }
}

resource "aws_s3_object" "ui_files" {
  for_each      = fileset(local.web_dir, "**")
  bucket        = module.ui_origin.bucket_id
  key           = each.value
  source        = "${local.web_dir}/${each.value}"
  source_hash   = filemd5("${local.web_dir}/${each.value}")
  content_type  = lookup(local.mime, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = endswith(each.value, ".html") ? "no-cache" : "public, max-age=300"
}

resource "aws_s3_object" "ui_config" {
  bucket        = module.ui_origin.bucket_id
  key           = "config.js"
  content_type  = "application/javascript; charset=utf-8"
  cache_control = "no-cache"
  content = <<-JS
    window.SSCP_CONFIG = ${jsonencode({
  apiUrl   = aws_apigatewayv2_stage.default.invoke_url
  region   = var.region
  clientId = aws_cognito_user_pool_client.web.id
})};
  JS
}

output "ui_url" {
  value = "https://${module.ui_edge.domain_name}"
}

output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "environment_table" {
  value = aws_dynamodb_table.environments.name
}

output "ui_distribution_id" {
  value = module.ui_edge.distribution_id
}
