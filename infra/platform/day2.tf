# ---------------------------------------------------------------- Day-2: drift detection and repair (D-186)
# A CloudTrail write call on a watched service -> the worker rechecks the environment it touched (seconds to a minute).
# The hourly sweep (api.tf) rechecks every running environment as well; it is the only path for CloudFront and IAM,
# global services whose CloudTrail events reach EventBridge in us-east-1, not here.
# The event names are platform_core/drift.py WATCHED_EVENTS (tests/test_day2_rule_sync.py keeps them identical).

locals {
  drift_watched = {
    "aws.ec2"                  = ["AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress", "RevokeSecurityGroupIngress", "RevokeSecurityGroupEgress", "ModifySecurityGroupRules", "ModifyNetworkInterfaceAttribute", "CreateRoute", "ReplaceRoute", "AssociateRouteTable", "ReplaceRouteTableAssociation"]
    "aws.s3"                   = ["PutBucketPolicy", "DeleteBucketPolicy", "PutBucketPublicAccessBlock", "DeleteBucketPublicAccessBlock", "PutBucketAcl", "PutBucketEncryption", "DeleteBucketEncryption", "PutBucketOwnershipControls", "PutBucketLogging"]
    "aws.lambda"               = ["AddPermission20150331v2", "RemovePermission20150331v2", "CreateFunctionUrlConfig20211031", "UpdateFunctionUrlConfig20211031", "UpdateFunctionConfiguration20150331v2", "PutFunctionConcurrency20171031"]
    "aws.apigateway"           = ["UpdateStage", "DeleteStage", "CreateRoute", "UpdateRoute", "UpdateIntegration", "UpdateApi"]
    "aws.ecs"                  = ["UpdateService"]
    "aws.elasticloadbalancing" = ["ModifyLoadBalancerAttributes", "ModifyListener", "CreateListener", "ModifyRule", "CreateRule", "SetSecurityGroups", "SetSubnets"]
    "aws.logs"                 = ["DeleteLogGroup", "PutRetentionPolicy", "DeleteRetentionPolicy"]
  }
}

resource "aws_cloudwatch_event_rule" "drift" {
  name        = "${local.prefix}-drift"
  description = "Console or API changes to platform resources -> recheck (D-186)"
  event_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail"]
    source        = keys(local.drift_watched)
    detail        = { eventName = distinct(flatten(values(local.drift_watched))) }
  })
}

resource "aws_cloudwatch_event_target" "drift" {
  rule = aws_cloudwatch_event_rule.drift.name
  arn  = aws_lambda_function.worker.arn
  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 600
  }
}

resource "aws_lambda_permission" "drift_events" {
  statement_id  = "AllowDriftRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drift.arn
}

# Repair: phase "plan" (started by the worker when drift is found) saves a validated plan back to the approved
# configuration; phase "apply" (started by POST /environments/{id}/repair, a person) applies exactly that plan.
locals {
  repair_definition = {
    Comment = "drift -> plan back to the approved config (validated, saved) | approved -> apply that plan -> validate"
    StartAt = "Phase"
    States = {
      Phase = {
        Type    = "Choice"
        Choices = [{ Variable = "$.phase", StringEquals = "apply", Next = "RepairApply" }]
        Default = "RepairPlan"
      }
      RepairPlan = {
        Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync", ResultPath = null, End = true
        Parameters = merge(local.build, { EnvironmentVariablesOverride = [
          { Name = "ENV_ID", "Value.$" = "$.env_id", Type = "PLAINTEXT" },
          { Name = "ACTION", Value = "repair_plan", Type = "PLAINTEXT" },
        ] })
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "PlanFailed" }]
      }
      RepairApply = {
        Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync", ResultPath = null, Next = "MarkValidating"
        Parameters = merge(local.build, { EnvironmentVariablesOverride = [
          { Name = "ENV_ID", "Value.$" = "$.env_id", Type = "PLAINTEXT" },
          { Name = "ACTION", Value = "repair_apply", Type = "PLAINTEXT" },
        ] })
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "ApplyFailed" }]
      }
      MarkValidating = {
        Type       = "Task", Resource = local.worker, ResultPath = null, Next = "Validate"
        Parameters = { action = "mark", status = "VALIDATING", "env_id.$" = "$.env_id" }
      }
      Validate = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "validate", "env_id.$" = "$.env_id" }
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"], MaxAttempts = 2 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "ApplyFailed" }]
      }
      PlanFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "repair_failed", phase = "plan", "env_id.$" = "$.env_id" }
      }
      ApplyFailed = {
        Type       = "Task", Resource = local.worker, End = true
        Parameters = { action = "repair_failed", phase = "apply", "env_id.$" = "$.env_id" }
      }
    }
  }
}

resource "aws_sfn_state_machine" "repair" {
  name       = "${local.prefix}-repair"
  role_arn   = aws_iam_role.sfn.arn
  definition = jsonencode(local.repair_definition)
}
