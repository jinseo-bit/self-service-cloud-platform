# Control plane Lambdas (one role each, SB-IAM-06), HTTP API with Cognito JWT, TTL schedule.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  lambda_zip = "${local.build_dir}/lambda.zip"
  common_env = {
    ENV_TABLE     = aws_dynamodb_table.environments.name
    UPLOAD_BUCKET = aws_s3_bucket.uploads.id
    LOG_BUCKET    = aws_s3_bucket.logs.id
    TRAIL_NAME    = local.trail_name
    ENV_BOUNDARY  = aws_iam_policy.env_boundary.arn
  }
  table_rw = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
}

# ---------------------------------------------------------------- api
data "aws_iam_policy_document" "api" {
  statement {
    actions   = local.table_rw
    resources = [aws_dynamodb_table.environments.arn, "${aws_dynamodb_table.environments.arn}/index/*"]
  }
  statement {
    sid       = "ListRevisionBundles"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.uploads.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["uploads/*"]
    }
  }
  statement {
    sid       = "PresignedUploadAndArtifactCheck"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/uploads/*"]
  }
  statement {
    actions = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.provision.arn, aws_sfn_state_machine.delete.arn, aws_sfn_state_machine.redeploy.arn,
    aws_sfn_state_machine.repair.arn]
  }
  statement {
    sid       = "RecheckNow" # D-186: POST /environments/{id}/recheck
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api_fn.arn}:*"]
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.prefix}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_cloudwatch_log_group" "api_fn" {
  name              = "/aws/lambda/${local.prefix}-api"
  retention_in_days = 30
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.prefix}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "control_plane.api.handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = 30
  memory_size      = 512
  environment {
    variables = merge(local.common_env, {
      PROVISION_SFN_ARN = aws_sfn_state_machine.provision.arn
      DELETE_SFN_ARN    = aws_sfn_state_machine.delete.arn
      REDEPLOY_SFN_ARN  = aws_sfn_state_machine.redeploy.arn
      REPAIR_SFN_ARN    = aws_sfn_state_machine.repair.arn
      WORKER_FUNCTION   = aws_lambda_function.worker.function_name
      AGENT_CLIENT_ID   = aws_cognito_user_pool_client.agent.id
    })
  }
  depends_on = [aws_cloudwatch_log_group.api_fn]
}

# ---------------------------------------------------------------- worker (validation, status, TTL sweep)
data "aws_iam_policy_document" "worker" {
  statement {
    actions   = local.table_rw
    resources = [aws_dynamodb_table.environments.arn]
  }
  statement {
    sid = "ReadEnvironmentBuckets"
    actions = ["s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetEncryptionConfiguration",
    "s3:GetBucketPolicy", "s3:GetBucketTagging"]
    resources = [local.env_bucket_arn]
  }
  statement {
    actions   = ["s3:GetLifecycleConfiguration"]
    resources = [aws_s3_bucket.logs.arn]
  }
  statement {
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/uploads/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.uploads.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["uploads/*"]
    }
  }
  statement {
    sid       = "InspectDistributions"
    actions   = ["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:ListTagsForResource"]
    resources = [local.any_distribution_arn]
  }
  statement {
    sid       = "DisableEnvironmentDistributionsOnly" # D-65: security failure disables the endpoint; never the platform UI
    actions   = ["cloudfront:UpdateDistribution"]
    resources = [local.any_distribution_arn]
    condition {
      test     = local.env_distribution_only.test
      variable = local.env_distribution_only.variable
      values   = local.env_distribution_only.values
    }
  }
  statement {
    sid     = "InspectProdMonitoring"
    actions = ["cloudwatch:DescribeAlarms", "sns:ListSubscriptionsByTopic"]
    resources = [
      "arn:${data.aws_partition.current.partition}:cloudwatch:us-east-1:${local.account_id}:alarm:${local.prefix}-env-*",
      "arn:${data.aws_partition.current.partition}:sns:us-east-1:${local.account_id}:${local.prefix}-env-*",
      "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${local.account_id}:alarm:${local.prefix}-env-*",
      "arn:${data.aws_partition.current.partition}:sns:${var.region}:${local.account_id}:${local.prefix}-env-*",
    ]
  }
  statement {
    sid = "InspectAndDisableServerlessFunctions" # D-65: concurrency 0 takes a failed endpoint offline
    actions = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListFunctionUrlConfigs", "lambda:GetPolicy",
    "lambda:ListTags", "lambda:PutFunctionConcurrency"]
    resources = [local.env_function_arn]
  }
  statement {
    sid       = "InspectServerlessRoles"
    actions   = ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy"]
    resources = [local.env_role_arn]
  }
  statement {
    sid       = "InspectServerlessEndpoints"
    actions   = ["apigateway:GET"]
    resources = ["${local.apigw_arn}/apis/*"]
  }
  statement {
    sid       = "InspectServerlessImages"
    actions   = ["ecr:DescribeRepositories"]
    resources = [local.env_repository_arn]
  }
  statement {
    sid       = "InspectLogRetention"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
  statement {
    # Resource types per the ECS Service Authorization Reference (D-177), pinned by tests/test_worker_iam_map.py:
    # DescribeServices -> service, DescribeTasks -> task, ListTasks -> container-instance (not task!), ListTagsForResource -> service.
    sid     = "InspectContainerServices"
    actions = ["ecs:DescribeServices", "ecs:ListTasks", "ecs:DescribeTasks", "ecs:ListTagsForResource"]
    resources = ["arn:${data.aws_partition.current.partition}:ecs:${var.region}:${local.account_id}:service/${local.prefix}-*/*",
      "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${local.account_id}:task/${local.prefix}-*/*",
    "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${local.account_id}:container-instance/${local.prefix}-*/*"]
  }
  statement {
    sid = "InspectContainerNetwork" # read-only APIs without resource-level permissions
    actions = ["ecs:DescribeTaskDefinition", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTargetHealth", "ec2:DescribeSecurityGroups", "ec2:DescribeRouteTables",
    "ec2:DescribeNetworkInterfaces"]
    resources = ["*"]
  }
  statement {
    actions   = ["cloudtrail:GetTrailStatus"]
    resources = ["arn:${data.aws_partition.current.partition}:cloudtrail:${var.region}:${local.account_id}:trail/${local.trail_name}"]
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.delete.arn, aws_sfn_state_machine.repair.arn]
  }
  statement {
    sid       = "RecheckFanOut" # D-186: the hourly sweep invokes itself once per running environment
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:${data.aws_partition.current.partition}:lambda:${var.region}:${local.account_id}:function:${local.prefix}-worker"]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker_fn.arn}:*"]
  }
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_cloudwatch_log_group" "worker_fn" {
  name              = "/aws/lambda/${local.prefix}-worker"
  retention_in_days = 30
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.prefix}-worker"
  role             = aws_iam_role.worker.arn
  runtime          = "python3.12"
  handler          = "control_plane.worker.handler"
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  timeout          = 900
  memory_size      = 256
  environment {
    # DELETE_SFN_ARN is read at runtime from the name to avoid a dependency cycle (worker ↔ state machine).
    variables = merge(local.common_env, {
      DELETE_SFN_ARN  = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.prefix}-delete"
      REPAIR_SFN_ARN  = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.prefix}-repair"
      PLATFORM_PREFIX = local.prefix # CloudTrail calls by the platform's own roles are not drift
    })
  }
  # B-1 (D-189): the code checks log-bucket versioning, a Security check. If it went live before versioning or the read
  # permission, every Static recheck would see a new security failure and block the endpoint (D-65).
  depends_on = [aws_cloudwatch_log_group.worker_fn, aws_iam_role_policy.worker_log_versioning, aws_s3_bucket_versioning.logs]
}

# A separate policy so the worker Lambda can depend on it: the main worker policy references the state machines,
# which reference the worker Lambda (a cycle).
resource "aws_iam_role_policy" "worker_log_versioning" {
  name = "log-bucket-versioning-read"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadLogBucketVersioning" # SB-LOG-01/versioning
      Effect   = "Allow"
      Action   = "s3:GetBucketVersioning"
      Resource = aws_s3_bucket.logs.arn
    }]
  })
}

# ---------------------------------------------------------------- TTL sweep (D-63)
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.worker.arn }]
  })
}

resource "aws_scheduler_schedule" "ttl_sweep" {
  name                = "${local.prefix}-ttl-sweep"
  schedule_expression = "rate(1 hour)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ action = "sweep" })
  }
}

# ---------------------------------------------------------------- HTTP API
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/sscp/api-access"
  retention_in_days = 30
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${local.prefix}-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://${module.ui_edge.domain_name}"]
    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 600
  }
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.http.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id, aws_cognito_user_pool_client.agent.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

locals {
  routes = [
    "GET /presets", "GET /me", "POST /recommendations", "POST /environments", "GET /environments",
    "GET /environments/{env_id}", "POST /environments/{env_id}/upload-url", "POST /environments/{env_id}/approve",
    "POST /environments/{env_id}/extend", "POST /environments/{env_id}/retry", "POST /environments/{env_id}/redeploy",
    "POST /environments/{env_id}/recheck", "POST /environments/{env_id}/repair", # D-186
    "DELETE /environments/{env_id}",
  ]
}

resource "aws_apigatewayv2_route" "routes" {
  for_each           = toset(local.routes)
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp", sub = "$context.authorizer.claims.sub",
      route     = "$context.routeKey", status = "$context.status", latency = "$context.responseLatency"
    })
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
