# Terraform runner (CodeBuild) with a permission boundary (SB-IAM-03), and the
# provision / delete state machines.

resource "aws_s3_object" "runner_bundle" {
  bucket      = aws_s3_bucket.state.id
  key         = "bundles/runner.zip"
  source      = "${local.build_dir}/runner.zip"
  source_hash = filemd5("${local.build_dir}/runner.zip")
}

locals {
  env_bucket_arn       = "arn:${data.aws_partition.current.partition}:s3:::${local.prefix}-env-*"
  any_distribution_arn = "arn:${data.aws_partition.current.partition}:cloudfront::${local.account_id}:distribution/*"
  # Environment distributions carry env-id = env-xxxxxxxx; the platform UI carries env-id = platform and never matches.
  env_distribution_only = { test = "StringLike", variable = "aws:ResourceTag/env-id", values = ["env-*"] }
  runner_allowed = {
    # Static blueprint resources only. Extended per blueprint (Serverless/Container later).
    StaticSiteBuckets = {
      actions = ["s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucket", "s3:Get*", "s3:PutBucket*", "s3:DeleteBucketPolicy",
        "s3:PutEncryptionConfiguration", "s3:PutLifecycleConfiguration", "s3:PutObject", "s3:DeleteObject",
      "s3:DeleteObjectVersion", "s3:ListBucketVersions"]
      resources = [local.env_bucket_arn, "${local.env_bucket_arn}/*"]
    }
    CodeBuildSource = {
      actions   = ["s3:GetObjectVersion", "s3:GetBucketAcl", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/bundles/*"]
    }
    State = {
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/envs/*", "${aws_s3_bucket.state.arn}/bundles/*"]
    }
    Uploads = {
      actions   = ["s3:GetObject"]
      resources = ["${aws_s3_bucket.uploads.arn}/uploads/*"]
    }
    LogBucketAclForCloudFront = {
      actions   = ["s3:GetBucketAcl", "s3:PutBucketAcl"]
      resources = [aws_s3_bucket.logs.arn]
    }
    # Create/List APIs and OACs cannot be scoped by tag; reads are harmless.
    CloudFrontCreateAndRead = {
      actions = ["cloudfront:CreateDistribution", "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig",
        "cloudfront:ListTagsForResource", "cloudfront:GetInvalidation",
        "cloudfront:CreateOriginAccessControl", "cloudfront:GetOriginAccessControl",
      "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl", "cloudfront:ListOriginAccessControls"]
      resources = ["*"]
    }
    # CreateDistributionWithTags also authorizes TagResource before the tags exist, so it is matched on the request.
    CloudFrontTagOnCreate = {
      actions    = ["cloudfront:TagResource"]
      resources  = [local.any_distribution_arn]
      conditions = [{ test = "StringLike", variable = "aws:RequestTag/env-id", values = ["env-*"] }]
    }
    # Changing or deleting is limited to environment distributions (never the platform UI).
    CloudFrontEnvironmentDistributions = {
      actions = ["cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution", "cloudfront:CreateInvalidation",
      "cloudfront:TagResource", "cloudfront:UntagResource"]
      resources  = [local.any_distribution_arn]
      conditions = [local.env_distribution_only]
    }
    Waf = {
      actions = ["wafv2:CreateWebACL", "wafv2:UpdateWebACL", "wafv2:DeleteWebACL", "wafv2:GetWebACL",
        "wafv2:CreateIPSet", "wafv2:UpdateIPSet", "wafv2:DeleteIPSet", "wafv2:GetIPSet",
      "wafv2:TagResource", "wafv2:UntagResource", "wafv2:ListTagsForResource"]
      resources = ["arn:${data.aws_partition.current.partition}:wafv2:us-east-1:${local.account_id}:global/*"]
    }
    ProdMonitoring = {
      actions = ["sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe",
        "sns:Unsubscribe", "sns:GetSubscriptionAttributes", "sns:ListTagsForResource", "sns:TagResource",
        "sns:UntagResource", "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms",
        "cloudwatch:ListTagsForResource", "cloudwatch:TagResource", "cloudwatch:UntagResource",
      "cloudwatch:PutDashboard", "cloudwatch:GetDashboard", "cloudwatch:DeleteDashboards"]
      resources = [
        "arn:${data.aws_partition.current.partition}:sns:us-east-1:${local.account_id}:${local.prefix}-env-*",
        "arn:${data.aws_partition.current.partition}:cloudwatch:us-east-1:${local.account_id}:alarm:${local.prefix}-env-*",
        "arn:${data.aws_partition.current.partition}:cloudwatch::${local.account_id}:dashboard/${local.prefix}-env-*",
      ]
    }
    EnvironmentRecords = {
      actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
      resources = [aws_dynamodb_table.environments.arn]
    }
    BuildLogs = {
      actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      resources = ["${aws_cloudwatch_log_group.runner.arn}:*"]
    }
    Identity = {
      actions   = ["sts:GetCallerIdentity"]
      resources = ["*"]
    }
  }
}

data "aws_iam_policy_document" "runner_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "runner" {
  dynamic "statement" {
    for_each = local.runner_allowed
    content {
      sid       = statement.key
      actions   = statement.value.actions
      resources = statement.value.resources
      dynamic "condition" {
        for_each = lookup(statement.value, "conditions", [])
        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

# SB-IAM-03: even if the runner policy is widened by mistake, the boundary caps it.
data "aws_iam_policy_document" "runner_boundary" {
  statement {
    sid = "AllowedServices"
    actions = ["s3:*", "cloudfront:*", "wafv2:*", "sns:*", "cloudwatch:*", "dynamodb:GetItem", "dynamodb:UpdateItem", "logs:CreateLogStream",
    "logs:PutLogEvents", "sts:GetCallerIdentity"]
    resources = ["*"]
  }
  statement {
    sid       = "NeverManageIdentityOrAccount"
    effect    = "Deny"
    actions   = ["iam:*", "organizations:*", "account:*", "sts:AssumeRole", "ec2:CreateNatGateway"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "runner_boundary" {
  name   = "${local.prefix}-runner-boundary"
  policy = data.aws_iam_policy_document.runner_boundary.json
}

resource "aws_iam_role" "runner" {
  name                 = "${local.prefix}-runner"
  assume_role_policy   = data.aws_iam_policy_document.runner_assume.json
  permissions_boundary = aws_iam_policy.runner_boundary.arn
}

resource "aws_iam_role_policy" "runner" {
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner.json
}

resource "aws_cloudwatch_log_group" "runner" {
  name              = "/sscp/runner"
  retention_in_days = 30
}

resource "aws_codebuild_project" "runner" {
  name          = "${local.prefix}-runner"
  service_role  = aws_iam_role.runner.arn
  build_timeout = 45

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.state.id}/${aws_s3_object.runner_bundle.key}"
    buildspec = "runner/buildspec.yml"
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  cache {
    type  = "LOCAL"
    modes = ["LOCAL_CUSTOM_CACHE"]
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type         = "LINUX_CONTAINER"

    environment_variable {
      name  = "ENV_TABLE"
      value = aws_dynamodb_table.environments.name
    }
    environment_variable {
      name  = "STATE_BUCKET"
      value = aws_s3_bucket.state.id
    }
    environment_variable {
      name  = "UPLOAD_BUCKET"
      value = aws_s3_bucket.uploads.id
    }
    environment_variable {
      name  = "LOG_BUCKET"
      value = aws_s3_bucket.logs.id
    }
    environment_variable {
      name  = "ACCOUNT_ID"
      value = local.account_id
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.runner.name
    }
  }
}

# ---------------------------------------------------------------- state machines
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "sfn" {
  statement {
    actions   = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds"]
    resources = [aws_codebuild_project.runner.arn]
  }
  statement {
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:${data.aws_partition.current.partition}:events:${var.region}:${local.account_id}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule"]
  }
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.prefix}-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

resource "aws_iam_role_policy" "sfn" {
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

locals {
  worker = aws_lambda_function.worker.arn
  build = {
    ProjectName = aws_codebuild_project.runner.name
  }
  provision_definition = {
    Comment = "Approve → apply (PLAN validator inside) → validate → READY/DEGRADED/FAILED"
    StartAt = "MarkProvisioning"
    States = {
      MarkProvisioning = {
        Type       = "Task", Resource = local.worker, ResultPath = null, Next = "Apply"
        Parameters = { action = "mark", status = "PROVISIONING", "env_id.$" = "$.env_id", message = "Terraform plan과 보안 검사를 실행합니다." }
      }
      Apply = {
        Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync", ResultPath = null, Next = "MarkValidating"
        Parameters = merge(local.build, { EnvironmentVariablesOverride = [
          { Name = "ENV_ID", "Value.$" = "$.env_id", Type = "PLAINTEXT" },
          { Name = "ACTION", Value = "apply", Type = "PLAINTEXT" },
        ] })
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "ProvisionFailed" }]
      }
      MarkValidating = {
        Type       = "Task", Resource = local.worker, ResultPath = null, Next = "Validate"
        Parameters = { action = "mark", status = "VALIDATING", "env_id.$" = "$.env_id" }
      }
      Validate = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "validate", "env_id.$" = "$.env_id" }
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"], MaxAttempts = 2 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "ValidateFailed" }]
      }
      ProvisionFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "fail", stage = "PROVISION", "env_id.$" = "$.env_id", "error.$" = "$.error" }
      }
      ValidateFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "fail", stage = "VALIDATE", "env_id.$" = "$.env_id", "error.$" = "$.error" }
      }
    }
  }
  delete_definition = {
    Comment = "terraform destroy → DELETED (DELETE_FAILED is retried by the TTL sweeper)"
    StartAt = "Destroy"
    States = {
      Destroy = {
        Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync", ResultPath = null, Next = "MarkDeleted"
        Parameters = merge(local.build, { EnvironmentVariablesOverride = [
          { Name = "ENV_ID", "Value.$" = "$.env_id", Type = "PLAINTEXT" },
          { Name = "ACTION", Value = "destroy", Type = "PLAINTEXT" },
        ] })
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "DeleteFailed" }]
      }
      MarkDeleted = {
        Type = "Task", Resource = local.worker, End = true, Parameters = { action = "deleted", "env_id.$" = "$.env_id" }
      }
      DeleteFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "fail", stage = "DELETE", "env_id.$" = "$.env_id", "error.$" = "$.error" }
      }
    }
  }
}

resource "aws_sfn_state_machine" "provision" {
  name       = "${local.prefix}-provision"
  role_arn   = aws_iam_role.sfn.arn
  definition = jsonencode(local.provision_definition)
}

resource "aws_sfn_state_machine" "delete" {
  name       = "${local.prefix}-delete"
  role_arn   = aws_iam_role.sfn.arn
  definition = jsonencode(local.delete_definition)
}
