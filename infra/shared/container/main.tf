# Shared infrastructure of the Container blueprint (D-33, D-43, D-44, D-46 / P-01, recommendation-engine §12):
# one VPC over 2 AZs (public subnets for Fargate tasks with a public IP, private subnets for the internal ALB,
# no NAT gateway), one ECS cluster, one internal ALB whose HTTP listener gets a rule per environment, and one
# CloudFront VPC origin for that ALB. The runner applies it before the first Container environment and destroys
# it after the last one, so it costs nothing while no Container environment exists.

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

variable "owner" {
  description = "Platform owner (tag)."
  type        = string
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "azs" {
  description = "Two Availability Zones (resolved by the runner; VPC origins and Fargate support every Seoul AZ)."
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]
  validation {
    condition     = length(var.azs) == 2
    error_message = "exactly two AZs."
  }
}

variable "cloudfront_prefix_list_id" {
  description = "AWS-managed prefix list com.amazonaws.global.cloudfront.origin-facing (SB-PUB-04), resolved by the runner."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.80.0.0/16"
}

variable "app_port" {
  description = "Port every app container listens on (D-34: PORT=8080)."
  type        = number
  default     = 8080
}

variable "alb_idle_timeout_s" {
  description = "One ALB serves every environment: longest CloudFront origin timeout (120 s, D-157) + 10 s."
  type        = number
  default     = 130
}

locals {
  name = "sscp-shared"
  tags = {
    "project"    = "sscp"
    "managed-by" = "platform"
    "env-id"     = "shared"
    "owner"      = var.owner
    "tier"       = "SHARED"
    "blueprint"  = "container-shared"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}

# ---------------------------------------------------------------- network (D-44: no NAT, tasks get a public IP)
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name } # also required by CloudFront VPC origins, which do not route through it
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  tags              = { Name = "${local.name}-public-${count.index}", tier-role = "public" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  tags              = { Name = "${local.name}-private-${count.index}", tier-role = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-public" }
}

# Private subnets have no route to the internet at all (SB-PUB-02).
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free gateway endpoint: ECR image layers come from S3 and stay on the AWS network.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

# ---------------------------------------------------------------- internal ALB (SB-PUB-02, SB-PUB-04)
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Internal ALB: CloudFront origin-facing only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "CloudFront (VPC origin)"
  prefix_list_id    = var.cloudfront_prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id = aws_security_group.alb.id
  description       = "App containers in this VPC"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = var.app_port
  to_port           = var.app_port
}

resource "aws_lb" "this" {
  name                       = local.name
  internal                   = true
  load_balancer_type         = "application"
  subnets                    = aws_subnet.private[*].id
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = var.alb_idle_timeout_s
  drop_invalid_header_fields = true
  enable_deletion_protection = false # destroyed with the last Container environment
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP" # CloudFront → VPC origin stays inside AWS; viewers get HTTPS only (SB-TLS-01)
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "no environment"
      status_code  = "404"
    }
  }
}

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = local.name
    arn                    = aws_lb.this.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
  depends_on = [aws_lb_listener.http]
}

# ---------------------------------------------------------------- ECS cluster
resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # cost: per-metric charges; task logs go to CloudWatch Logs
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"] # FARGATE_SPOT for DEV + A0 (POL_SPOT_DEV_A0)
}

# ---------------------------------------------------------------- outputs (passed to every Container environment)
output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "alb_arn" {
  value = aws_lb.this.arn
}

output "alb_arn_suffix" {
  value = aws_lb.this.arn_suffix
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "listener_arn" {
  value = aws_lb_listener.http.arn
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "vpc_origin_id" {
  value = aws_cloudfront_vpc_origin.alb.id
}
