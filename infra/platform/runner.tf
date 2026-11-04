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
  arn_prefix            = "arn:${data.aws_partition.current.partition}"
  env_role_arn          = "${local.arn_prefix}:iam::${local.account_id}:role/${local.prefix}-env-*"
  env_function_arn      = "${local.arn_prefix}:lambda:${var.region}:${local.account_id}:function:${local.prefix}-env-*"
  env_repository_arn    = "${local.arn_prefix}:ecr:${var.region}:${local.account_id}:repository/${local.prefix}-env-*"
  env_log_group_arns = [
    "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-env-*",
    "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-env-*:*",
    "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/sscp/env/*",
    "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/sscp/env/*:*",
  ]
  apigw_arn = "${local.arn_prefix}:apigateway:${var.region}::"
  ec2_arn   = "${local.arn_prefix}:ec2:${var.region}:${local.account_id}:*"
  ecs_arn   = "${local.arn_prefix}:ecs:${var.region}:${local.account_id}"
  elb_arn   = "${local.arn_prefix}:elasticloadbalancing:${var.region}:${local.account_id}"
  # Service-linked roles the Container blueprint relies on (they already exist in the account; AWS may re-check them).
  container_slr_services = ["vpcorigin.cloudfront.amazonaws.com", "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com"]
  container_slr_arns     = [for s in local.container_slr_services : "${local.arn_prefix}:iam::${local.account_id}:role/aws-service-role/${s}/*"]
  runner_allowed = {
    # Static + Serverless + Container blueprint resources only. Everything is scoped to sscp-* names, sscp tags,
    # environment tags, or (for IAM) the environment permissions boundary.
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
      actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/envs/*", "${aws_s3_bucket.state.arn}/bundles/*",
      "${aws_s3_bucket.state.arn}/shared/*"]
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
        # Serverless PROD alarms watch Regional metrics (Lambda, API Gateway).
        "arn:${data.aws_partition.current.partition}:sns:${var.region}:${local.account_id}:${local.prefix}-env-*",
        "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${local.account_id}:alarm:${local.prefix}-env-*",
      ]
    }
    # ---------------------------------------------------------------- Serverless blueprint (D-165, D-166)
    EcrLogin = {
      actions   = ["ecr:GetAuthorizationToken"] # registry-wide by design; pushes are limited below
      resources = ["*"]
    }
    ServerlessImages = {
      actions = ["ecr:CreateRepository", "ecr:DeleteRepository", "ecr:DescribeRepositories", "ecr:ListTagsForResource",
        "ecr:TagResource", "ecr:UntagResource", "ecr:PutLifecyclePolicy", "ecr:GetLifecyclePolicy", "ecr:DeleteLifecyclePolicy",
        "ecr:SetRepositoryPolicy", "ecr:GetRepositoryPolicy", "ecr:DeleteRepositoryPolicy", "ecr:PutImageScanningConfiguration",
        "ecr:PutImageTagMutability", "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages",
      "ecr:BatchDeleteImage", "ecr:ListImages"]
      resources = [local.env_repository_arn]
    }
    ServerlessFunctions = {
      actions = ["lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
        "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:ListVersionsByFunction",
        "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConcurrency", "lambda:TagResource", "lambda:UntagResource",
      "lambda:ListTags", "lambda:AddPermission", "lambda:RemovePermission", "lambda:GetPolicy"]
      resources = [local.env_function_arn]
    }
    ServerlessLogs = {
      actions = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
      "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource", "logs:TagLogGroup", "logs:ListTagsLogGroup"]
      resources = local.env_log_group_arns
    }
    LogsDescribe = {
      actions   = ["logs:DescribeLogGroups"] # list API, not resource-scoped
      resources = ["*"]
    }
    ApiAccessLogDelivery = { # HTTP API access logs (vended log delivery); these actions have no resource type
      actions = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies"]
      resources = ["*"]
    }
    ServerlessApis = { # TagResource: resources created with tags (default_tags) are also authorized as a tag action
      actions = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE",
      "apigateway:TagResource"]
      resources = ["${local.apigw_arn}/apis", "${local.apigw_arn}/apis/*", "${local.apigw_arn}/tags/*"]
    }
    # API ids are random, so the platform's own API is excluded explicitly.
    PlatformApiIsOffLimits = {
      effect    = "Deny"
      actions   = ["apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE", "apigateway:TagResource"]
      resources = ["${local.apigw_arn}/apis/${aws_apigatewayv2_api.http.id}", "${local.apigw_arn}/apis/${aws_apigatewayv2_api.http.id}/*"]
    }
    EnvironmentRoles = {
      actions = ["iam:GetRole", "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags", "iam:PutRolePolicy",
        "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole"]
      resources = [local.env_role_arn]
    }
    # SB-IAM-04: a role can only be created (or re-bounded) with the environment boundary attached.
    EnvironmentRoleCreate = {
      actions    = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
      resources  = [local.env_role_arn]
      conditions = [{ test = "StringEquals", variable = "iam:PermissionsBoundary", values = [aws_iam_policy.env_boundary.arn] }]
    }
    PassEnvironmentRoleToLambda = {
      actions    = ["iam:PassRole"]
      resources  = [local.env_role_arn]
      conditions = [{ test = "StringEquals", variable = "iam:PassedToService", values = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com"] }]
    }
    KeepEnvironmentBoundary = {
      effect    = "Deny"
      actions   = ["iam:DeleteRolePermissionsBoundary", "iam:AttachRolePolicy", "iam:PutUserPolicy", "iam:CreateUser", "iam:CreateAccessKey"]
      resources = ["*"]
    }
    EnvironmentRecords = { # Scan: refcount of environments on a shared stack (Container)
      actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
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
  # Container blueprint (D-33, D-44, D-170). A managed policy of its own: IAM caps a role's inline policies at
  # 10,240 characters in total, and the Static + Serverless statements above already use most of it (D-175).
  runner_container_allowed = {
    Ec2Describe = {
      actions   = ["ec2:Describe*"] # read-only, not resource-scoped
      resources = ["*"]
    }
    # Creating the shared network; NAT gateways, EIPs and instances stay denied by the boundary.
    SharedNetworkCreate = {
      actions = ["ec2:CreateVpc", "ec2:ModifyVpcAttribute", "ec2:CreateSubnet", "ec2:ModifySubnetAttribute",
        "ec2:CreateInternetGateway", "ec2:AttachInternetGateway", "ec2:CreateRouteTable", "ec2:CreateRoute",
        "ec2:AssociateRouteTable", "ec2:CreateVpcEndpoint", "ec2:ModifyVpcEndpoint", "ec2:CreateSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupEgress", "ec2:ModifySecurityGroupRules", "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress", "ec2:CreateTags"]
      resources = [local.ec2_arn, "${local.arn_prefix}:ec2:${var.region}:aws:prefix-list/*"] # AWS-managed CloudFront list
    }
    # Deleting is limited to network resources the platform tagged (project = sscp), never the default VPC.
    SharedNetworkDeleteOwn = {
      actions = ["ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
        "ec2:DeleteRouteTable", "ec2:DeleteRoute", "ec2:DisassociateRouteTable", "ec2:DeleteVpcEndpoints",
      "ec2:DeleteSecurityGroup", "ec2:DeleteTags"]
      resources  = [local.ec2_arn]
      conditions = [{ test = "StringEquals", variable = "aws:ResourceTag/project", values = [local.prefix] }]
    }
    LoadBalancingDescribe = {
      actions   = ["elasticloadbalancing:Describe*"]
      resources = ["*"]
    }
    LoadBalancing = {
      actions = ["elasticloadbalancing:*"]
      resources = ["${local.elb_arn}:loadbalancer/app/${local.prefix}-*/*", "${local.elb_arn}:listener/app/${local.prefix}-*/*",
      "${local.elb_arn}:listener-rule/app/${local.prefix}-*/*", "${local.elb_arn}:targetgroup/${local.prefix}-*/*"]
    }
    # Not resource-scoped by ECS (task definitions are tagged and scoped through TagResource below).
    EcsRegister = {
      actions = ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition",
      "ecs:ListTaskDefinitions", "ecs:CreateCluster", "ecs:DescribeClusters", "ecs:DescribeCapacityProviders"]
      resources = ["*"]
    }
    EcsPlatformResources = {
      actions = ["ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService", "ecs:DescribeServices", "ecs:ListTasks",
        "ecs:DescribeTasks", "ecs:DeleteCluster", "ecs:UpdateCluster", "ecs:UpdateClusterSettings",
      "ecs:PutClusterCapacityProviders", "ecs:TagResource", "ecs:UntagResource", "ecs:ListTagsForResource"]
      resources = ["${local.ecs_arn}:cluster/${local.prefix}-*", "${local.ecs_arn}:service/${local.prefix}-*/*",
      "${local.ecs_arn}:task/${local.prefix}-*/*", "${local.ecs_arn}:task-definition/${local.prefix}-env-*:*"]
    }
    # wait_for_steady_state follows the service deployment (Container E2E 2026-10-07: AccessDenied on ListServiceDeployments).
    # Describe* need both the service and the deployment/revision resource (Service Authorization Reference).
    EcsDeploymentTracking = {
      actions = ["ecs:ListServiceDeployments", "ecs:DescribeServiceDeployments", "ecs:DescribeServiceRevisions"]
      resources = ["${local.ecs_arn}:service/${local.prefix}-*/${local.prefix}-env-*",
        "${local.ecs_arn}:service-deployment/${local.prefix}-*/${local.prefix}-env-*/*",
      "${local.ecs_arn}:service-revision/${local.prefix}-*/${local.prefix}-env-*/*"]
    }
    CloudFrontVpcOrigins = {
      actions = ["cloudfront:CreateVpcOrigin", "cloudfront:GetVpcOrigin", "cloudfront:UpdateVpcOrigin", "cloudfront:DeleteVpcOrigin",
      "cloudfront:ListVpcOrigins", "cloudfront:TagResource", "cloudfront:UntagResource", "cloudfront:ListTagsForResource"]
      resources = ["${local.arn_prefix}:cloudfront::${local.account_id}:vpcorigin/*"]
    }
    ContainerServiceLinkedRoles = {
      actions    = ["iam:CreateServiceLinkedRole"]
      resources  = local.container_slr_arns
      conditions = [{ test = "StringEquals", variable = "iam:AWSServiceName", values = local.container_slr_services }]
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
      effect    = lookup(statement.value, "effect", "Allow")
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

data "aws_iam_policy_document" "runner_container" {
  dynamic "statement" {
    for_each = local.runner_container_allowed
    content {
      sid       = statement.key
      effect    = lookup(statement.value, "effect", "Allow")
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
    actions = ["s3:*", "cloudfront:*", "wafv2:*", "sns:*", "cloudwatch:*", "dynamodb:GetItem", "dynamodb:UpdateItem",
      "dynamodb:Scan", "logs:*", "ecr:*", "lambda:*", "apigateway:*", "ec2:*", "elasticloadbalancing:*", "ecs:*",
    "sts:GetCallerIdentity"]
    resources = ["*"]
  }
  statement {
    sid       = "ContainerServiceLinkedRolesOnly"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = local.container_slr_arns
  }
  statement {
    sid = "EnvironmentRolesOnly"
    actions = ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
      "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
    "iam:ListInstanceProfilesForRole", "iam:PutRolePermissionsBoundary", "iam:PassRole"]
    resources = [local.env_role_arn]
  }
  statement {
    sid           = "NeverManageOtherIdentities"
    effect        = "Deny"
    actions       = ["iam:*"]
    not_resources = concat([local.env_role_arn], local.container_slr_arns)
  }
  statement {
    sid    = "NeverEscalateOrLeaveTheAccount"
    effect = "Deny"
    actions = ["iam:DeleteRolePermissionsBoundary", "iam:AttachRolePolicy", "organizations:*", "account:*", "sts:AssumeRole",
    "ec2:CreateNatGateway", "ec2:AllocateAddress", "ec2:RunInstances", "ec2:CreateVpcPeeringConnection"]
    resources = ["*"]
  }
}

# SB-IAM-04: every role the runner creates for an environment carries this boundary. An app can at most
# write environment logs and (Container execution role) pull environment images, whatever its inline policy says.
data "aws_iam_policy_document" "env_boundary" {
  statement {
    sid     = "OwnLogsOnly"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-env-*:*",
    "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/sscp/env/*:*"]
  }
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "PullEnvironmentImages"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [local.env_repository_arn]
  }
}

resource "aws_iam_policy" "env_boundary" {
  name   = "${local.prefix}-env-boundary"
  policy = data.aws_iam_policy_document.env_boundary.json
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

resource "aws_iam_policy" "runner_container" {
  name        = "${local.prefix}-runner-container"
  description = "Runner permissions for the Container blueprint (shared VPC, internal ALB, ECS, CloudFront VPC origin)"
  policy      = data.aws_iam_policy_document.runner_container.json
}

resource "aws_iam_role_policy_attachment" "runner_container" {
  role       = aws_iam_role.runner.name
  policy_arn = aws_iam_policy.runner_container.arn
}

resource "aws_cloudwatch_log_group" "runner" {
  name              = "/sscp/runner"
  retention_in_days = 30
}

resource "aws_codebuild_project" "runner" {
  name          = "${local.prefix}-runner"
  service_role  = aws_iam_role.runner.arn
  build_timeout = 90 # Container: the first environment creates the VPC origin (up to 15 min), the last one deletes it

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
    environment_variable {
      name  = "ENV_BOUNDARY_ARN"
      value = aws_iam_policy.env_boundary.arn
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
