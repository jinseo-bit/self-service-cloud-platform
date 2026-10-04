# PROD monitoring for a CloudFront-fronted environment (D-62, PR-ALL-06).
# CloudFront metrics live in us-east-1, so the alarms and their SNS topic do too.

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

variable "distribution_id" {
  type = string
}

variable "alert_email" {
  type = string
}

resource "aws_sns_topic" "alerts" {
  provider = aws.us_east_1
  name     = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarms = {
    "5xx-error-rate" = { metric = "5xxErrorRate", threshold = 5, desc = "서버 오류 응답이 5%를 넘었습니다." }
    "4xx-error-rate" = { metric = "4xxErrorRate", threshold = 25, desc = "잘못된 요청·없는 페이지 응답이 25%를 넘었습니다." }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each            = local.alarms
  provider            = aws.us_east_1
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/CloudFront"
  metric_name         = each.value.metric
  dimensions          = { DistributionId = var.distribution_id, Region = "Global" }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "this" {
  provider       = aws.us_east_1
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [for i, m in ["Requests", "BytesDownloaded", "4xxErrorRate", "5xxErrorRate"] : {
      type = "metric", x = (i % 2) * 12, y = floor(i / 2) * 6, width = 12, height = 6
      properties = {
        title   = m, region = "us-east-1", stat = m == "Requests" || m == "BytesDownloaded" ? "Sum" : "Average", period = 300
        metrics = [["AWS/CloudFront", m, "DistributionId", var.distribution_id, "Region", "Global"]]
      }
    }]
  })
}

output "topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "alarm_names" {
  value = [for a in aws_cloudwatch_metric_alarm.this : a.alarm_name]
}
