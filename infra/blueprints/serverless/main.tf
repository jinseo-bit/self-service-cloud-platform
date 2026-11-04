# Serverless Blueprint MVP (D-32, D-165): HTTP container image on Lambda + Lambda Web Adapter,
# public HTTPS endpoint through an API Gateway HTTP API. No data layer, no VPC, no fixed hourly cost.
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

variable "alert_email" {
  description = "PROD alarm recipient (environment owner)."
  type        = string
  default     = ""
}

variable "image_uri" {
  description = "Derived image (user image + Lambda Web Adapter) in this environment's ECR repository, pinned by digest."
  type        = string
}

variable "env_vars" {
  description = "Plain (non-secret) application settings. Secrets are rejected before this point (D-167)."
  type        = map(string)
  default     = {}
}

variable "health_path" {
  type    = string
  default = "/"
}

variable "memory_mb" {
  type    = number
  default = 512
}

variable "timeout_s" {
  description = "HTTP API integrations time out at 30 s (D-157), so the function stays below it."
  type        = number
  default     = 29
  validation {
    condition     = var.timeout_s >= 3 && var.timeout_s <= 29
    error_message = "timeout_s must be between 3 and 29."
  }
}

variable "env_boundary_arn" {
  description = "Permissions boundary every environment role must carry (SB-IAM-04)."
  type        = string
}

variable "app_port" {
  type    = number
  default = 8080
}

locals {
  name           = "sscp-${var.env_id}"
  log_days       = var.tier == "PROD" ? 30 : 7 # SB-LOG-04
  security_codes = ["200", "201", "204", "301", "302", "304", "400", "401", "403", "404", "500"]
  tags = merge(
    {
      "project"    = "sscp"
      "managed-by" = "platform"
      "env-id"     = var.env_id
      "owner"      = var.owner
      "tier"       = var.tier
      "blueprint"  = "serverless"
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

# ---------------------------------------------------------------- image registry (SB-PUB-07, SB-ENC-05)
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # destroy removes the derived images too
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# Lambda pulls the image as a service; only this environment's function may do so.
resource "aws_ecr_repository_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaPullForThisEnvironment"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = { StringLike = { "aws:sourceArn" = "arn:aws:lambda:${var.region}:${var.account_id}:function:${local.name}" } }
    }]
  })
}

# ---------------------------------------------------------------- logs (SB-LOG-03, SB-LOG-04)
resource "aws_cloudwatch_log_group" "fn" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = local.log_days
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/sscp/env/${var.env_id}/api-access"
  retention_in_days = local.log_days
}

# ---------------------------------------------------------------- execution role (SB-IAM-01, SB-IAM-02, SB-IAM-04)
resource "aws_iam_role" "fn" {
  name                 = local.name
  permissions_boundary = var.env_boundary_arn
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "fn" {
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteOwnLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.fn.arn}:*"
    }]
  })
}

# ---------------------------------------------------------------- function
resource "aws_lambda_function" "app" {
  function_name = local.name
  role          = aws_iam_role.fn.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["x86_64"]
  memory_size   = var.memory_mb
  timeout       = var.timeout_s

  environment {
    # Lambda Web Adapter (in /opt/extensions of the derived image) forwards events to the app's HTTP port.
    variables = merge(var.env_vars, {
      PORT                         = tostring(var.app_port)
      AWS_LWA_PORT                 = tostring(var.app_port)
      AWS_LWA_READINESS_CHECK_PATH = var.health_path
    })
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.fn.name
  }

  depends_on = [aws_iam_role_policy.fn, aws_cloudwatch_log_group.fn, aws_ecr_repository_policy.app]
}

# ---------------------------------------------------------------- HTTPS endpoint (SB-TLS-02, SB-PUB-06)
resource "aws_apigatewayv2_api" "http" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "app" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000

  # Security headers on app responses (D-168): HTTP APIs have no response-headers policy, so they are mapped here.
  dynamic "response_parameters" {
    for_each = local.security_codes
    content {
      status_code = response_parameters.value
      mappings = {
        "overwrite:header.Strict-Transport-Security" = "max-age=31536000; includeSubDomains"
        "overwrite:header.X-Content-Type-Options"    = "nosniff"
        "overwrite:header.X-Frame-Options"           = "DENY"
        "overwrite:header.Referrer-Policy"           = "strict-origin-when-cross-origin"
      }
    }
  }
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.app.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_rate_limit  = 20 # cost guard: an unexpected flood is throttled, not billed
    throttling_burst_limit = 40
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp", route = "$context.routeKey",
      status    = "$context.status", latency = "$context.responseLatency", error = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowThisHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# ---------------------------------------------------------------- PROD monitoring (PR-ALL-06)
resource "aws_sns_topic" "alerts" {
  count = var.tier == "PROD" ? 1 : 0
  name  = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.tier == "PROD" && var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = var.tier == "PROD" ? {
    "errors"  = { ns = "AWS/Lambda", metric = "Errors", dims = { FunctionName = local.name }, desc = "앱 오류가 발생했습니다." }
    "api-5xx" = { ns = "AWS/ApiGateway", metric = "5xx", dims = { ApiId = aws_apigatewayv2_api.http.id }, desc = "서버 오류 응답이 발생했습니다." }
  } : {}
  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = each.value.ns
  metric_name         = each.value.metric
  dimensions          = each.value.dims
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

# ---------------------------------------------------------------- outputs (consumed by the worker's STATE/HTTP checks)
output "url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "api_id" {
  value = aws_apigatewayv2_api.http.id
}

output "function_name" {
  value = aws_lambda_function.app.function_name
}

output "role_name" {
  value = aws_iam_role.fn.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.fn.name
}

output "repository_name" {
  value = aws_ecr_repository.app.name
}

output "repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "image_uri" {
  value = aws_lambda_function.app.image_uri
}

output "alarm_names" {
  value = [for a in aws_cloudwatch_metric_alarm.this : a.alarm_name]
}

output "alert_topic_arn" {
  value = var.tier == "PROD" ? aws_sns_topic.alerts[0].arn : null
}
