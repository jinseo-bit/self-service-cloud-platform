"""Worker (validation Lambda) permissions for the Container path, pinned to the AWS Service Authorization Reference
(D-177). The Container E2E of 2026-10-08 passed apply and then failed VALIDATE on its first call: `ecs:ListTasks`
authorizes on the *container-instance* resource type, and that ARN had been removed from the worker policy as
"unnecessary". Every AWS call the validators make is listed here with the resource ARNs IAM evaluates; the worker
policy in infra/platform/api.tf must allow each one, and a call missing from this map fails the test."""

import inspect
import json
import re
from pathlib import Path

from platform_core.validation import state
from tests.test_platform_iam_size import _EXPR, ACCOUNT, REFS, REGION, _values

API_TF = Path(__file__).resolve().parents[1] / "infra" / "platform" / "api.tf"
_EXPR.setdefault("local.trail_name", "sscp-account-trail")

ECS = f"arn:aws:ecs:{REGION}:{ACCOUNT}"
ENV = "env-af602604"  # the environment of the 2026-10-08 E2E
DIST = f"arn:aws:cloudfront::{ACCOUNT}:distribution/EXEUXRN1385HY"
ANY = None            # the action has no resource-level permissions: the statement must use Resource "*"

# boto3 call -> (IAM action, ARNs IAM evaluates). Resource types from
# https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html (and the ELB, EC2, IAM, ECR,
# CloudFront, CloudTrail, CloudWatch, SNS, Logs references).
CALLS = {
    "ecs.describe_services": ("ecs:DescribeServices", [f"{ECS}:service/sscp-shared/sscp-{ENV}"]),
    "ecs.list_tasks": ("ecs:ListTasks", [f"{ECS}:container-instance/sscp-shared/*"]),  # what IAM evaluated in the E2E
    "ecs.describe_tasks": ("ecs:DescribeTasks", [f"{ECS}:task/sscp-shared/0123456789abcdef"]),
    "ecs.describe_task_definition": ("ecs:DescribeTaskDefinition", ANY),
    "ecs.list_tags_for_resource": ("ecs:ListTagsForResource", [f"{ECS}:service/sscp-shared/sscp-{ENV}"]),
    "elbv2.describe_target_groups": ("elasticloadbalancing:DescribeTargetGroups", ANY),
    "elbv2.describe_load_balancers": ("elasticloadbalancing:DescribeLoadBalancers", ANY),
    "elbv2.describe_target_health": ("elasticloadbalancing:DescribeTargetHealth", ANY),
    "ec2.describe_route_tables": ("ec2:DescribeRouteTables", ANY),
    "ec2.describe_security_groups": ("ec2:DescribeSecurityGroups", ANY),
    "ec2.describe_network_interfaces": ("ec2:DescribeNetworkInterfaces", ANY),
    "iam.get_role": ("iam:GetRole", [f"arn:aws:iam::{ACCOUNT}:role/sscp-{ENV}-exec"]),
    "iam.list_role_policies": ("iam:ListRolePolicies", [f"arn:aws:iam::{ACCOUNT}:role/sscp-{ENV}-task"]),
    "iam.get_role_policy": ("iam:GetRolePolicy", [f"arn:aws:iam::{ACCOUNT}:role/sscp-{ENV}-exec"]),
    "logs.describe_log_groups": ("logs:DescribeLogGroups", ANY),
    "ecr.describe_repositories": ("ecr:DescribeRepositories", [f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/sscp-{ENV}"]),
    "cloudfront.get_distribution_config": ("cloudfront:GetDistributionConfig", [DIST]),
    "cloudfront.update_distribution": ("cloudfront:UpdateDistribution", [DIST]),
    "cloudtrail.get_trail_status": ("cloudtrail:GetTrailStatus", [f"arn:aws:cloudtrail:{REGION}:{ACCOUNT}:trail/sscp-account-trail"]),
    "cloudwatch.describe_alarms": ("cloudwatch:DescribeAlarms", [f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:sscp-{ENV}-unhealthy"]),
    "sns.list_subscriptions_by_topic": ("sns:ListSubscriptionsByTopic", [f"arn:aws:sns:{REGION}:{ACCOUNT}:sscp-{ENV}-alerts"]),
}

# Everything the worker runs for a Container environment (validate) and on a security failure (D-65).
VALIDATOR_FUNCTIONS = [state.validate_container_state, state.task_public_ips, state._wide_statements,
                       state.check_prod_monitoring, state.disable_distribution]


def worker_statements() -> list[dict]:
    tf = API_TF.read_text(encoding="utf-8")
    body = tf.split('data "aws_iam_policy_document" "worker" {', 1)[1].split("\n}\n", 1)[0]
    out = []
    for block in re.split(r"\n  statement \{", body)[1:]:
        def field(name, block=block):
            m = re.search(rf"\b{name}\s*=\s*(\[.*?\]|local\.[a-z_]+)", block, re.S)
            return _values(m.group(1)) if m else []
        out.append({"Sid": (re.search(r'sid\s*=\s*"(\w+)"', block) or [None, None])[1],
                    "Effect": (re.search(r'effect\s*=\s*"(\w+)"', block) or [None, "Allow"])[1],
                    "Action": field("actions"), "Resource": field("resources")})
    return out


def _matches(pattern: str, arn: str) -> bool:
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), arn) is not None


def _calls_in(fn) -> set[str]:
    src = inspect.getsource(fn)
    return {f"{c}.{m}" for c, m in re.findall(r"\b(ecs|elbv2|ec2|iam|logs|ecr|cloudfront|cloudtrail|cloudwatch|sns)\.([a-z_]+)\(", src)}


def test_every_validator_call_is_in_the_permission_map():
    """A new boto3 call in the Container validators must be added to CALLS (with its resource type) first."""
    used = set().union(*(_calls_in(f) for f in VALIDATOR_FUNCTIONS))
    assert used <= set(CALLS), f"unmapped calls: {sorted(used - set(CALLS))}"


def test_worker_policy_allows_every_mapped_call_on_its_real_resource():
    allowed = [s for s in worker_statements() if s["Effect"] == "Allow"]
    denied = [s for s in worker_statements() if s["Effect"] == "Deny"]
    missing = []
    for call, (action, arns) in CALLS.items():
        stmts = [s for s in allowed if action in s["Action"]]
        if arns is ANY:
            if not any("*" in s["Resource"] for s in stmts):
                missing.append(f"{call}: {action} needs Resource '*'")
            continue
        for arn in arns:
            if not any(_matches(r, arn) for s in stmts for r in s["Resource"]):
                missing.append(f"{call}: {action} on {arn}")
            if any(action in s["Action"] and any(_matches(r, arn) for r in s["Resource"]) for s in denied):
                missing.append(f"{call}: {action} denied on {arn}")
    assert not missing, "\n".join(missing)


def test_list_tasks_is_authorized_on_container_instances():
    """The exact regression: ListTasks with only service/task ARNs is AccessDenied."""
    stmt = next(s for s in worker_statements() if "ecs:ListTasks" in s["Action"])
    assert any(_matches(r, f"{ECS}:container-instance/sscp-shared/*") for r in stmt["Resource"])
    assert not any(r == "*" for r in stmt["Resource"]), "scoped to the platform's clusters, not every cluster"
    assert not any(_matches(r, f"{ECS}:container-instance/other-cluster/x") for r in stmt["Resource"])


def test_container_read_statements_stay_read_only():
    for sid in ("InspectContainerServices", "InspectContainerNetwork"):
        stmt = next(s for s in worker_statements() if s["Sid"] == sid)
        verbs = {a.split(":", 1)[1] for a in stmt["Action"]}
        assert all(v.startswith(("Describe", "List", "Get")) for v in verbs), (sid, verbs)


def _rendered_size(statements) -> int:
    doc = {"Version": "2012-10-17", "Statement": [{k: v for k, v in s.items() if k != "Sid" or v} for s in statements]}
    return len(json.dumps(doc, separators=(",", ":")))


def test_worker_inline_policy_fits_the_role_limit():
    """Worker has one inline policy (10,240 characters for all inline policies of a role). Measured live on
    2026-10-09: 3,562 characters before the container-instance ARN, 3,632 after (35 %). The estimate leaves out
    `local.table_rw`, so it runs a little low; the 92 % bound keeps the margin."""
    size = _rendered_size(worker_statements())
    assert size <= 10_240 * 0.92, size
    assert REFS  # shared renderer is loaded
