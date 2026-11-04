# Container Blueprint MVP (D-33, D-44, D-46, D-170): the user's HTTP container image on ECS Fargate behind
# CloudFront → VPC origin → the shared internal ALB (one listener rule per environment). No data layer, no NAT.
# Root module executed by the platform runner, one state per environment; the shared VPC, ALB, cluster and
# VPC origin come from infra/shared/container and are passed in as variables.

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

variable "ip_allowlist" {
  description = "Empty = public. Non-empty = WAF allows only these CIDRs (SB-PUB-08)."
  type        = list(string)
  default     = []
}

variable "image_uri" {
  description = "The user's image copied into this environment's ECR repository, pinned by digest."
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

variable "app_port" {
  description = "D-34: the app listens on PORT."
  type        = number
  default     = 8080
}

variable "task_cpu" {
  description = "Fargate CPU units (engine ctr_task_vcpu x 1024)."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate memory MiB (engine ctr_task_memory_gb x 1024)."
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Engine ctr_min_tasks (A0: 1, A1: 2). Autoscaling is not part of the MVP."
  type        = number
  default     = 1
  validation {
    condition     = var.desired_count >= 1 && var.desired_count <= 6
    error_message = "desired_count must be 1-6 (platform cap)."
  }
}

variable "capacity_provider" {
  description = "FARGATE_SPOT for DEV + A0 (POL_SPOT_DEV_A0), otherwise FARGATE."
  type        = string
  default     = "FARGATE"
  validation {
    condition     = contains(["FARGATE", "FARGATE_SPOT"], var.capacity_provider)
    error_message = "capacity_provider must be FARGATE or FARGATE_SPOT."
  }
}

variable "origin_read_timeout_s" {
  description = "CloudFront origin response timeout from the longest request (D-157: 30/60/90/120 s)."
  type        = number
  default     = 30
  validation {
    condition     = var.origin_read_timeout_s >= 1 && var.origin_read_timeout_s <= 120
    error_message = "origin_read_timeout_s must be 1-120."
  }
}

variable "listener_priority" {
  description = "Rule priority on the shared listener (derived from env_id by the runner)."
  type        = number
}

variable "env_boundary_arn" {
  description = "Permissions boundary every environment role must carry (SB-IAM-04)."
  type        = string
}

# Shared infrastructure (outputs of infra/shared/container)
variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "alb_security_group_id" {
  type = string
}

variable "alb_dns_name" {
  type = string
}

variable "alb_arn_suffix" {
  type = string
}

variable "listener_arn" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "vpc_origin_id" {
  type = string
}

locals {
  name           = "sscp-${var.env_id}"
  log_days       = var.tier == "PROD" ? 30 : 7 # SB-LOG-04
  routing_header = "X-Sscp-Env"                # set by this environment's CloudFront origin; viewers cannot override it
  use_waf        = length(var.ip_allowlist) > 0
  # AWS managed policies (stable, documented IDs)
  cache_policy_caching_disabled             = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  origin_request_policy_all_viewer_but_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  response_headers_security_policy          = "67f7725c-6f97-4210-82d7-5512b31e9d03"
  tags = merge(
    {
      "project"    = "sscp"
      "managed-by" = "platform"
      "env-id"     = var.env_id
      "owner"      = var.owner
      "tier"       = var.tier
      "blueprint"  = "container"
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

# ---------------------------------------------------------------- image registry (SB-PUB-07, SB-ENC-05)
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # destroy removes the copied images too
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

# ---------------------------------------------------------------- logs (SB-LOG-04)
resource "aws_cloudwatch_log_group" "app" {
  name              = "/sscp/env/${var.env_id}/app"
  retention_in_days = local.log_days
}

# ---------------------------------------------------------------- roles (SB-IAM-01, SB-IAM-02, SB-IAM-04)
locals {
  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
    }]
  })
}

# Execution role: ECS pulls this environment's image and writes this environment's log group, nothing else.
resource "aws_iam_role" "exec" {
  name                 = "${local.name}-exec"
  permissions_boundary = var.env_boundary_arn
  assume_role_policy   = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "exec" {
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken" # not resource-scoped by AWS
        Resource = "*"
      },
      {
        Sid      = "PullOwnImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
    ]
  })
}

# Task role: the app itself gets no AWS permissions in the MVP (no data layer).
resource "aws_iam_role" "task" {
  name                 = "${local.name}-task"
  permissions_boundary = var.env_boundary_arn
  assume_role_policy   = local.ecs_tasks_trust
}

# ---------------------------------------------------------------- network (SB-PUB-03, D-44)
resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "App tasks: inbound only from the shared ALB on the app port"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  description                  = "Shared internal ALB"
  referenced_security_group_id = var.alb_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.app_port
  to_port                      = var.app_port
}

# Outbound: image pull (ECR over the public IP, S3 gateway endpoint) and the app's own calls; no NAT (D-44).
resource "aws_vpc_security_group_egress_rule" "task_out" {
  security_group_id = aws_security_group.task.id
  description       = "Outbound"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------- routing on the shared ALB (P-01)
resource "aws_lb_target_group" "app" {
  name                 = local.name
  port                 = var.app_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 10
  health_check {
    path                = var.health_path
    matcher             = "200-399"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "app" {
  listener_arn = var.listener_arn
  priority     = var.listener_priority
  condition {
    http_header {
      http_header_name = local.routing_header
      values           = [var.env_id]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ---------------------------------------------------------------- task + service (PR-C-03, PR-C-04, PR-C-05)
resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "app"
    image        = var.image_uri
    essential    = true
    portMappings = [{ containerPort = var.app_port, protocol = "tcp" }]
    environment  = [for k, v in merge(var.env_vars, { PORT = tostring(var.app_port) }) : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name                              = local.name
  cluster                           = var.cluster_name
  task_definition                   = aws_ecs_task_definition.app.arn
  desired_count                     = var.desired_count
  health_check_grace_period_seconds = 30
  propagate_tags                    = "SERVICE"
  enable_ecs_managed_tags           = true
  wait_for_steady_state             = true # apply returns once the tasks are running and healthy

  capacity_provider_strategy {
    capacity_provider = var.capacity_provider
    weight            = 1
  }

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # no NAT (D-44); inbound is still only from the ALB (SB-PUB-03)
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.app_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  timeouts {
    create = "20m"
    delete = "20m"
  }

  depends_on = [aws_lb_listener_rule.app, aws_iam_role_policy.exec]
}

# ---------------------------------------------------------------- edge (SB-TLS-01, SB-TLS-05, SB-LOG-02, SB-PUB-08)
resource "aws_wafv2_ip_set" "allow" {
  count              = local.use_waf ? 1 : 0
  provider           = aws.us_east_1
  name               = "${local.name}-allow"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = var.ip_allowlist
}

resource "aws_wafv2_web_acl" "this" {
  count    = local.use_waf ? 1 : 0
  provider = aws.us_east_1
  name     = local.name
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
      metric_name                = "${local.name}-allow"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled      = true
  comment      = local.name
  price_class  = "PriceClass_200"
  http_version = "http2and3"
  web_acl_id   = local.use_waf ? aws_wafv2_web_acl.this[0].arn : null

  origin {
    origin_id   = "shared-alb"
    domain_name = var.alb_dns_name
    vpc_origin_config {
      vpc_origin_id            = var.vpc_origin_id
      origin_read_timeout      = var.origin_read_timeout_s
      origin_keepalive_timeout = 5
    }
    custom_header {
      name  = local.routing_header
      value = var.env_id
    }
  }

  default_cache_behavior {
    target_origin_id           = "shared-alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_caching_disabled
    origin_request_policy_id   = local.origin_request_policy_all_viewer_but_host
    response_headers_policy_id = local.response_headers_security_policy
  }

  logging_config {
    bucket          = "${var.log_bucket}.s3.amazonaws.com"
    prefix          = "envs/${var.env_id}/cloudfront/"
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

  depends_on = [aws_ecs_service.app] # serve only once the app is healthy
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
    "unhealthy"  = { metric = "UnHealthyHostCount", stat = "Maximum", threshold = 1, desc = "응답하지 않는 컨테이너가 있습니다." }
    "target-5xx" = { metric = "HTTPCode_Target_5XX_Count", stat = "Sum", threshold = 5, desc = "앱 오류 응답이 발생했습니다." }
  } : {}
  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/ApplicationELB"
  metric_name         = each.value.metric
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = aws_lb_target_group.app.arn_suffix }
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

# ---------------------------------------------------------------- outputs (consumed by the worker's STATE/HTTP checks)
output "url" {
  value = "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "distribution_id" {
  value = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  value = aws_cloudfront_distribution.this.arn
}

output "cluster_name" {
  value = var.cluster_name
}

output "service_name" {
  value = aws_ecs_service.app.name
}

output "target_group_arn" {
  value = aws_lb_target_group.app.arn
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}

output "exec_role_name" {
  value = aws_iam_role.exec.name
}

output "task_role_name" {
  value = aws_iam_role.task.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "repository_name" {
  value = aws_ecr_repository.app.name
}

output "repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "image_uri" {
  value = var.image_uri
}

output "alarm_names" {
  value = [for a in aws_cloudwatch_metric_alarm.this : a.alarm_name]
}

output "alert_topic_arn" {
  value = var.tier == "PROD" ? aws_sns_topic.alerts[0].arn : null
}
