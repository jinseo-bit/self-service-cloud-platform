# Static redeploy (D-150): new revision into an existing environment without terraform.
# Publisher Lambda has its own role (SB-IAM-06): read uploads, write only env site buckets,
# invalidate env distributions.

data "aws_iam_policy_document" "publisher" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.environments.arn]
  }
  statement {
    sid       = "ReadRevisionBundles"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/uploads/*"]
  }
  statement {
    sid       = "ReplaceSiteContent"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.env_bucket_arn}/*"]
  }
  statement {
    sid       = "ListSiteContent"
    actions   = ["s3:ListBucket"]
    resources = [local.env_bucket_arn]
  }
  statement {
    sid       = "ReadDistributionConfig"
    actions   = ["cloudfront:GetDistributionConfig"]
    resources = [local.any_distribution_arn]
  }
  statement {
    sid       = "InvalidateAndReenableEnvironmentDistributionsOnly"
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:UpdateDistribution"]
    resources = [local.any_distribution_arn]
    condition {
      test     = local.env_distribution_only.test
      variable = local.env_distribution_only.variable
      values   = local.env_distribution_only.values
    }
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.publisher_fn.arn}:*"]
  }
}

resource "aws_iam_role" "publisher" {
  name               = "${local.prefix}-publisher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "publisher" {
  role   = aws_iam_role.publisher.id
  policy = data.aws_iam_policy_document.publisher.json
}

resource "aws_cloudwatch_log_group" "publisher_fn" {
  name              = "/aws/lambda/${local.prefix}-publisher"
  retention_in_days = 30
}

resource "aws_lambda_function" "publisher" {
  function_name    = "${local.prefix}-publisher"
  role             = aws_iam_role.publisher.arn
  runtime          = "python3.12"
  handler          = "control_plane.publisher.handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = 300
  memory_size      = 512
  ephemeral_storage {
    size = 1024 # bundles are capped at 50 MB; room for download + extraction
  }
  environment {
    variables = local.common_env
  }
  depends_on = [aws_cloudwatch_log_group.publisher_fn]
}

resource "aws_iam_role_policy" "sfn_publisher" {
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.publisher.arn }]
  })
}

locals {
  redeploy_definition = {
    Comment = "New static revision → publish (no terraform) → STATE/HTTP validation → revision recorded"
    StartAt = "Publish"
    States = {
      Publish = {
        Type       = "Task", Resource = aws_lambda_function.publisher.arn, ResultPath = null, Next = "MarkValidating"
        Parameters = { "env_id.$" = "$.env_id" }
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"], MaxAttempts = 2 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "RedeployFailed" }]
      }
      MarkValidating = {
        Type       = "Task", Resource = local.worker, ResultPath = null, Next = "Validate"
        Parameters = { action = "mark", status = "VALIDATING", "env_id.$" = "$.env_id" }
      }
      Validate = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "validate", "env_id.$" = "$.env_id" }
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"], MaxAttempts = 2 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "RedeployFailed" }]
      }
      RedeployFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "fail", stage = "REDEPLOY", "env_id.$" = "$.env_id", "error.$" = "$.error" }
      }
    }
  }
}

resource "aws_sfn_state_machine" "redeploy" {
  name       = "${local.prefix}-redeploy"
  role_arn   = aws_iam_role.sfn.arn
  definition = jsonencode(local.redeploy_definition)
}
