"""IAM size limits for the runner role (D-175). The platform apply of 2026-10-07 failed with LimitExceeded: a role's
inline policies may hold 10,240 characters in total and a managed policy 6,144 — limits the offline tests never
exercised. The policies are rendered from the Terraform source with the account's real name lengths and checked
against the limits with a margin, so growth is caught before `terraform apply`."""

import json
import re
from pathlib import Path

RUNNER_TF = Path(__file__).resolve().parents[1] / "infra" / "platform" / "runner.tf"
INLINE_LIMIT, MANAGED_LIMIT = 10_240, 6_144  # https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
MARGIN = 0.92  # keep 8 % free for the next statement

ACCOUNT, REGION, PREFIX = "954222410428", "ap-northeast-2", "sscp"
_ARN = "arn:aws"
LOCALS = {
    "local.arn_prefix": _ARN, "local.account_id": ACCOUNT, "var.region": REGION, "local.prefix": PREFIX,
    "data.aws_partition.current.partition": "aws",
}
REFS = {  # bare references in `resources = [...]` and their rendered values
    "local.env_bucket_arn": [f"{_ARN}:s3:::{PREFIX}-env-*"],
    "local.any_distribution_arn": [f"{_ARN}:cloudfront::{ACCOUNT}:distribution/*"],
    "local.env_role_arn": [f"{_ARN}:iam::{ACCOUNT}:role/{PREFIX}-env-*"],
    "local.env_function_arn": [f"{_ARN}:lambda:{REGION}:{ACCOUNT}:function:{PREFIX}-env-*"],
    "local.env_repository_arn": [f"{_ARN}:ecr:{REGION}:{ACCOUNT}:repository/{PREFIX}-env-*"],
    "local.env_log_group_arns": [f"{_ARN}:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{PREFIX}-env-*",
                                 f"{_ARN}:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{PREFIX}-env-*:*",
                                 f"{_ARN}:logs:{REGION}:{ACCOUNT}:log-group:/sscp/env/*",
                                 f"{_ARN}:logs:{REGION}:{ACCOUNT}:log-group:/sscp/env/*:*"],
    "local.ec2_arn": [f"{_ARN}:ec2:{REGION}:{ACCOUNT}:*"],
    "local.container_slr_arns": [f"{_ARN}:iam::{ACCOUNT}:role/aws-service-role/{s}/*" for s in
                                 ("vpcorigin.cloudfront.amazonaws.com", "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com")],
    "local.container_slr_services": ["vpcorigin.cloudfront.amazonaws.com", "ecs.amazonaws.com", "elasticloadbalancing.amazonaws.com"],
    "local.env_distribution_only": None,
    "aws_s3_bucket.state.arn": [f"{_ARN}:s3:::{PREFIX}-state-{ACCOUNT}"],
    "aws_s3_bucket.uploads.arn": [f"{_ARN}:s3:::{PREFIX}-uploads-{ACCOUNT}"],
    "aws_s3_bucket.logs.arn": [f"{_ARN}:s3:::{PREFIX}-logs-{ACCOUNT}"],
    "aws_dynamodb_table.environments.arn": [f"{_ARN}:dynamodb:{REGION}:{ACCOUNT}:table/{PREFIX}-environments"],
    "aws_cloudwatch_log_group.runner.arn": [f"{_ARN}:logs:{REGION}:{ACCOUNT}:log-group:/sscp/runner"],
    "aws_iam_policy.env_boundary.arn": [f"{_ARN}:iam::{ACCOUNT}:policy/{PREFIX}-env-boundary"],
    "aws_apigatewayv2_api.http.id": ["f0i4pyw7y9"],
}
_EXPR = {
    "local.elb_arn": f"{_ARN}:elasticloadbalancing:{REGION}:{ACCOUNT}", "local.ecs_arn": f"{_ARN}:ecs:{REGION}:{ACCOUNT}",
    "local.apigw_arn": f"{_ARN}:apigateway:{REGION}::",
    **{k: v[0] for k, v in REFS.items() if v and len(v) == 1}, **LOCALS,
}


def _interpolate(text: str) -> str:
    return re.sub(r"\$\{([^}]+)\}", lambda m: _EXPR.get(m.group(1).strip(), m.group(0)), text)


def _values(expr: str) -> list[str]:
    """`["a", local.x, "${local.y}/z"]` or `local.list` → rendered strings."""
    out = [_interpolate(v) for v in re.findall(r'"((?:[^"\\]|\\.)*)"', expr)]
    for ref in re.findall(r"\b(local\.[a-z_]+|aws_[a-z0-9_]+\.[a-z_]+\.(?:arn|id))\b", re.sub(r'"(?:[^"\\]|\\.)*"', "", expr)):
        out += REFS.get(ref) or []
    return out


def statements(map_name: str) -> list[dict]:
    tf = RUNNER_TF.read_text(encoding="utf-8")
    body = tf.split(f"  {map_name} = {{", 1)[1].split("\n  }\n", 1)[0]
    out = []
    for sid, block in re.findall(r"^    (\w+) = \{(.*?)^    \}", body, re.S | re.M):
        def field(name, block=block):
            m = re.search(rf"\b{name}\s*=\s*(\[.*?\]|local\.[a-z_]+)", block, re.S)
            return _values(m.group(1)) if m else []
        st = {"Sid": sid, "Effect": (re.search(r'effect\s*=\s*"(\w+)"', block) or [None, "Allow"])[1],
              "Action": field("actions"), "Resource": field("resources")}
        if "conditions" in block or "env_distribution_only" in block:
            st["Condition"] = {"StringLike": {"aws:ResourceTag/env-id": "env-*"}}  # every condition here is about this long
        out.append(st)
    return out


def rendered_size(map_name: str) -> int:
    return len(json.dumps({"Version": "2012-10-17", "Statement": statements(map_name)}, separators=(",", ":")))


def test_runner_inline_policy_fits_the_role_limit():
    size = rendered_size("runner_allowed")
    assert 6_000 < size <= INLINE_LIMIT * MARGIN, f"runner inline policy ~{size} chars (limit {INLINE_LIMIT})"


def test_runner_container_policy_fits_the_managed_policy_limit():
    size = rendered_size("runner_container_allowed")
    assert 2_500 < size <= MANAGED_LIMIT * MARGIN, f"runner container policy ~{size} chars (limit {MANAGED_LIMIT})"


def test_container_statements_live_in_the_managed_policy():
    inline = {s["Sid"] for s in statements("runner_allowed")}
    managed = {s["Sid"] for s in statements("runner_container_allowed")}
    assert not inline & managed
    assert {"SharedNetworkCreate", "SharedNetworkDeleteOwn", "LoadBalancing", "EcsPlatformResources",
            "CloudFrontVpcOrigins", "ContainerServiceLinkedRoles"} <= managed
    tf = RUNNER_TF.read_text(encoding="utf-8")
    assert 'resource "aws_iam_role_policy_attachment" "runner_container"' in tf


def test_the_estimate_matches_the_measured_policy():
    """Calibration: the runner inline policy measured from the 2026-10-07 plan was 7,661 characters (Static +
    Serverless + 3 widened statements) and the Container part 3,477 before EcsDeploymentTracking. The estimate stays
    within 5 %."""
    assert abs(rendered_size("runner_allowed") - 7_661) / 7_661 < 0.05, rendered_size("runner_allowed")
    # 3,477 measured + EcsDeploymentTracking rendered with the account's real ARNs = 3,871 (D-176)
    assert abs(rendered_size("runner_container_allowed") - 3_871) / 3_871 < 0.05, rendered_size("runner_container_allowed")


# ---------------------------------------------------------------- ECS deployment tracking (D-176)

CONTAINER_TF = Path(__file__).resolve().parents[1] / "infra" / "blueprints" / "container" / "main.tf"
ECS_ARN = f"{_ARN}:ecs:{REGION}:{ACCOUNT}"
# Service Authorization Reference: ListServiceDeployments needs `service`; the Describe* actions need `service` AND
# their own `service-deployment` / `service-revision` resource.
TRACKING = {
    "ecs:ListServiceDeployments": {"service"},
    "ecs:DescribeServiceDeployments": {"service", "service-deployment"},
    "ecs:DescribeServiceRevisions": {"service", "service-revision"},
}
ENV_SERVICE = f"{ECS_ARN}:service/sscp-shared/sscp-env-74469f09"  # the service of the failed E2E


def _arn_matches(pattern: str, arn: str) -> bool:
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), arn) is not None


def _resource_type(arn: str) -> str:
    return arn.split(":", 5)[5].split("/", 1)[0]


def test_wait_for_steady_state_has_its_deployment_tracking_permissions():
    """terraform-provider-aws waits for a new ECS service through its service deployments; without these the
    apply fails after CreateService succeeded (Container E2E 2026-10-07)."""
    assert re.search(r"wait_for_steady_state\s*=\s*true", CONTAINER_TF.read_text(encoding="utf-8"))
    allowed = [s for s in statements("runner_container_allowed") if s["Effect"] == "Allow"]
    for action, needed in TRACKING.items():
        stmts = [s for s in allowed if action in s["Action"]]
        assert stmts, f"{action} missing from the runner container policy"
        types = {_resource_type(r) for s in stmts for r in s["Resource"]}
        assert needed <= types, f"{action} needs resource types {needed}, has {types}"
    tracking = next(s for s in allowed if "ecs:ListServiceDeployments" in s["Action"])
    assert any(_arn_matches(r, ENV_SERVICE) for r in tracking["Resource"]), "the environment's service ARN is covered"
    assert any(_arn_matches(r, f"{ECS_ARN}:service-deployment/sscp-shared/sscp-env-74469f09/abc") for r in tracking["Resource"])
    assert any(_arn_matches(r, f"{ECS_ARN}:service-revision/sscp-shared/sscp-env-74469f09/123") for r in tracking["Resource"])


def test_deployment_tracking_stays_read_only_and_platform_scoped():
    tracking = next(s for s in statements("runner_container_allowed") if s["Sid"] == "EcsDeploymentTracking")
    assert set(tracking["Action"]) == set(TRACKING), "no write actions in the tracking statement"
    assert all(r.startswith(f"{ECS_ARN}:") and "/sscp-" in r and r != "*" for r in tracking["Resource"])
    assert not any(_arn_matches(r, f"{ECS_ARN}:service/other-cluster/other-service") for r in tracking["Resource"])
