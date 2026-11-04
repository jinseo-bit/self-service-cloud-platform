"""Container blueprint validators (D-33, D-44, D-170): PLAN (real offline terraform plans of the environment and the
shared stack), STATE (fake AWS clients with real response shapes), HTTP (a local server standing in for CloudFront),
and baseline coverage."""

import copy
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from platform_core import baseline
from platform_core.production_ready import CHECK_TITLES, CONTAINER_CHECKS
from platform_core.validation.http import validate_container_http
from platform_core.validation.image import validate_image_spec
from platform_core.validation.plan import validate_plan
from platform_core.validation.result import FAIL, NOT_APPLICABLE, PASS, CheckResult, verdict
from platform_core.validation.state import validate_container_state
from runner.main import merge_shared

from .conftest import requires_terraform

FIX = {"FIX_ALWAYS_ON_COMPUTE", "FIX_ALB_FOR_CONTAINER", "FIX_IMAGE_REGISTRY"}


def by_id(results):
    out = {}
    for r in results:
        out.setdefault(r.check_id, set()).add(r.status)
    return out


def _rc(plan, rtype, name=None):
    return next(r for r in plan["resource_changes"] if r["type"] == rtype and (name is None or r["name"] == name))


def fails(results):
    return [(r.check_id, r.detail) for r in results if r.status == FAIL]


# ---------------------------------------------------------------- PLAN: positive

@requires_terraform
def test_container_plan_dev_is_minimal_and_passes(container_plan):
    types = sorted(r["type"] for r in container_plan["resource_changes"])
    assert types.count("aws_ecs_service") == 1 and types.count("aws_iam_role") == 2 and "aws_lb" not in types
    assert not {"aws_nat_gateway", "aws_eip", "aws_vpc", "aws_wafv2_web_acl", "aws_sns_topic"} & set(types)
    results = validate_plan(container_plan, blueprint="container", fixed_cost_reasons=FIX)
    assert not fails(results), fails(results)
    got = by_id(results)
    assert got["SB-PUB-03"] == {PASS} and got["SB-SEC-02"] == {PASS} and got["SB-IAM-04"] == {PASS}
    assert got["PLAN-FIXED-COST"] == {PASS} and got["SB-TLS-01"] == {PASS} and got["SB-LOG-02"] == {PASS}
    svc = _rc(container_plan, "aws_ecs_service")["change"]["after"]
    assert svc["desired_count"] == 1 and svc["capacity_provider_strategy"][0]["capacity_provider"] == "FARGATE_SPOT"
    assert svc["network_configuration"][0]["assign_public_ip"] is True  # D-44: no NAT
    assert svc["deployment_circuit_breaker"][0] == {"enable": True, "rollback": True}


@requires_terraform
def test_container_plan_prod_with_allowlist(container_plan_prod_allowlist):
    plan = container_plan_prod_allowlist
    results = validate_plan(plan, blueprint="container", ip_allowlist=["203.0.113.0/24"], fixed_cost_reasons=FIX)
    assert not fails(results), fails(results)
    assert by_id(results)["SB-PUB-08"] == {PASS}
    types = [r["type"] for r in plan["resource_changes"]]
    assert types.count("aws_cloudwatch_metric_alarm") == 2 and "aws_sns_topic_subscription" in types
    assert _rc(plan, "aws_ecs_service")["change"]["after"]["desired_count"] == 2
    origin = _rc(plan, "aws_cloudfront_distribution")["change"]["after"]["origin"][0]
    assert origin["vpc_origin_config"][0]["origin_read_timeout"] == 120  # D-157: 2-minute requests
    assert origin["custom_header"][0]["name"] == "X-Sscp-Env"


@requires_terraform
def test_shared_plan_has_no_nat_and_an_internal_alb(shared_container_plan):
    plan = shared_container_plan
    types = [r["type"] for r in plan["resource_changes"]]
    assert "aws_nat_gateway" not in types and "aws_eip" not in types
    results = validate_plan(plan, blueprint="container-shared", fixed_cost_reasons=FIX)
    assert not fails(results), fails(results)
    got = by_id(results)
    assert got["SB-PUB-02"] == {PASS} and got["SB-PUB-04"] == {PASS} and got["PLAN-FIXED-COST"] == {PASS}
    private_rt = _rc(plan, "aws_route_table", "private")["change"]["after"]
    assert not private_rt.get("route"), "private subnets have no internet route (SB-PUB-02)"
    assert _rc(plan, "aws_vpc_endpoint")["change"]["after"]["vpc_endpoint_type"] == "Gateway"  # free


@requires_terraform
def test_environment_plan_takes_the_shared_alb_checks_instead_of_na(container_plan, shared_container_plan):
    env = validate_plan(container_plan, blueprint="container", fixed_cost_reasons=FIX)
    shared = validate_plan(shared_container_plan, blueprint="container-shared", fixed_cost_reasons=FIX)
    assert by_id(env)["SB-PUB-02"] == {NOT_APPLICABLE}
    merged = by_id(merge_shared(env, shared))
    assert merged["SB-PUB-02"] == {PASS} and merged["SB-PUB-04"] == {PASS} and merged["SB-PUB-03"] == {PASS}


# ---------------------------------------------------------------- PLAN: negative

def _env_with(container_plan, mutate):
    bad = copy.deepcopy(container_plan)
    mutate(bad)
    return by_id(validate_plan(bad, blueprint="container", fixed_cost_reasons=FIX))


@requires_terraform
@pytest.mark.parametrize("mutate,failing", [
    (lambda p: _rc(p, "aws_vpc_security_group_ingress_rule")["change"]["after"].update(
        cidr_ipv4="0.0.0.0/0", referenced_security_group_id=None), {"SB-PUB-03", "SB-PUB-10"}),
    (lambda p: _rc(p, "aws_vpc_security_group_ingress_rule")["change"]["after"].update(
        cidr_ipv4="10.0.0.0/8", referenced_security_group_id=None), {"SB-PUB-03"}),
    (lambda p: _rc(p, "aws_vpc_security_group_ingress_rule")["change"]["after"].update(from_port=0, to_port=65535),
     {"SB-PUB-03"}),
    (lambda p: _rc(p, "aws_iam_role", "exec")["change"]["after"].update(permissions_boundary=None), {"SB-IAM-04"}),
    (lambda p: _rc(p, "aws_ecs_task_definition")["change"]["after"].update(container_definitions=json.dumps(
        [{"name": "app", "environment": [{"name": "DB_PASSWORD", "value": "hunter2"}]}])), {"SB-SEC-02"}),
    (lambda p: _rc(p, "aws_ecs_task_definition")["change"]["after"].update(container_definitions=json.dumps(
        [{"name": "app", "secrets": [{"name": "X", "valueFrom": "arn:aws:secretsmanager:..."}]}])), {"SB-SEC-02"}),
    (lambda p: _rc(p, "aws_cloudfront_distribution")["change"]["after"]["default_cache_behavior"][0].update(
        viewer_protocol_policy="allow-all"), {"SB-TLS-01"}),
    (lambda p: _rc(p, "aws_ecr_repository")["change"]["after"].update(image_tag_mutability="MUTABLE"), {"SB-PUB-07/immutable"}),
    (lambda p: p["resource_changes"].append({"address": "aws_nat_gateway.oops", "type": "aws_nat_gateway", "name": "oops",
                                             "change": {"actions": ["create"], "after": {}, "after_unknown": {}}}),
     {"SB-PUB-09", "PLAN-FIXED-COST", "PLAN-ALLOWLIST"}),
])
def test_container_plan_violations_are_caught(container_plan, mutate, failing):
    got = _env_with(container_plan, mutate)
    assert failing <= {k for k, v in got.items() if FAIL in v}, got


@requires_terraform
def test_unjustified_fixed_cost_is_blocked(container_plan):
    """A plan that runs an always-on service the recommendation did not justify is stopped (§9.6)."""
    got = by_id(validate_plan(container_plan, blueprint="container", fixed_cost_reasons=set()))
    assert got["PLAN-FIXED-COST"] == {FAIL}


@requires_terraform
@pytest.mark.parametrize("mutate,failing", [
    (lambda p: _rc(p, "aws_lb")["change"]["after"].update(internal=False), {"SB-PUB-02"}),
    (lambda p: _rc(p, "aws_vpc_security_group_ingress_rule")["change"]["after"].update(
        prefix_list_id=None, cidr_ipv4="0.0.0.0/0"), {"SB-PUB-04", "SB-PUB-10"}),
    (lambda p: _rc(p, "aws_vpc_endpoint")["change"]["after"].update(vpc_endpoint_type="Interface"), {"PLAN-FIXED-COST"}),
])
def test_shared_plan_violations_are_caught(shared_container_plan, mutate, failing):
    bad = copy.deepcopy(shared_container_plan)
    mutate(bad)
    got = by_id(validate_plan(bad, blueprint="container-shared", fixed_cost_reasons=FIX))
    assert failing <= {k for k, v in got.items() if FAIL in v}, got


# ---------------------------------------------------------------- STATE (fake AWS clients)

ACCT = "111122223333"
REPO = f"{ACCT}.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-env-abcd1234"
BOUNDARY = f"arn:aws:iam::{ACCT}:policy/sscp-env-boundary"
TG = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCT}:targetgroup/sscp-env-abcd1234/1"
LB = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCT}:loadbalancer/app/sscp-shared/1"
TAGS = {"project": "sscp", "managed-by": "platform", "env-id": "env-abcd1234", "owner": "o", "tier": "DEV",
        "blueprint": "container", "expires-at": "2026-10-11T00:00:00Z"}
CTX = {"cluster_name": "sscp-shared", "service_name": "sscp-env-abcd1234", "target_group_arn": TG,
       "task_security_group_id": "sg-task", "alb_security_group_id": "sg-alb", "exec_role_name": "sscp-env-abcd1234-exec",
       "task_role_name": "sscp-env-abcd1234-task", "log_group": "/sscp/env/env-abcd1234/app", "repository_name": "sscp-env-abcd1234",
       "repository_url": REPO, "distribution_id": "E123", "env_boundary_arn": BOUNDARY, "tier": "DEV", "multi_az": "SINGLE",
       "min_tasks": 1, "app_port": 8080, "log_retention_days": 7, "trail_name": "sscp-account-trail", "ip_allowlist": []}


def rule(port, *, groups=(), prefix_lists=(), cidrs=()):
    """One IpPermissions entry as EC2 DescribeSecurityGroups returns it."""
    return {"FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": c} for c in cidrs], "Ipv6Ranges": [],
            "PrefixListIds": [{"PrefixListId": p} for p in prefix_lists], "UserIdGroupPairs": [{"GroupId": g} for g in groups]}


class Fake:
    def __init__(self, **calls):
        self.calls = calls

    def __getattr__(self, name):
        value = self.calls[name]
        return value if callable(value) else (lambda **_: value)


def clients(running=1, desired=1, breaker=True, image=f"{REPO}@sha256:" + "c" * 64, boundary=BOUNDARY, env=None,
            scheme="internal", igw_route=False, health=("healthy",), task_ingress=None, alb_ingress=None, logging=True,
            vpc_origin=True, retention=7, policy_action="ecr:BatchGetImage", azs=("ap-northeast-2a",), scan=True,
            route_tables=None):
    breaker_cfg = {"deploymentCircuitBreaker": {"enable": breaker, "rollback": breaker}}
    service = {"status": "ACTIVE", "runningCount": running, "desiredCount": desired, "taskDefinition": "td-arn",
               "serviceArn": f"arn:aws:ecs:ap-northeast-2:{ACCT}:service/sscp-shared/sscp-env-abcd1234",
               "deploymentConfiguration": breaker_cfg}
    variables = [{"name": k, "value": v} for k, v in (env or {"PORT": "8080"}).items()]
    task_def = {"executionRoleArn": f"arn:aws:iam::{ACCT}:role/sscp-env-abcd1234-exec",
                "taskRoleArn": f"arn:aws:iam::{ACCT}:role/sscp-env-abcd1234-task",
                "containerDefinitions": [{"image": image, "environment": variables}]}
    ecs = Fake(describe_services={"services": [service]},
               list_tasks={"taskArns": [f"t{i}" for i in range(len(azs))]},
               describe_tasks={"tasks": [{"availabilityZone": az} for az in azs]},
               describe_task_definition={"taskDefinition": task_def},
               list_tags_for_resource={"tags": [{"key": k, "value": v} for k, v in TAGS.items()]})

    def get_role(RoleName):
        return {"Role": {"RoleName": RoleName, "Arn": f"arn:aws:iam::{ACCT}:role/{RoleName}",
                         **({"PermissionsBoundary": {"PermissionsBoundaryArn": boundary}} if boundary else {})}}
    repo_arn = f"arn:aws:ecr:ap-northeast-2:{ACCT}:repository/sscp-env-abcd1234"
    iam = Fake(get_role=get_role, list_role_policies={"PolicyNames": ["inline"]},
               get_role_policy={"PolicyDocument": {"Statement": [
                   {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                   {"Effect": "Allow", "Action": [policy_action], "Resource": repo_arn}]}})
    lb = {"Scheme": scheme, "AvailabilityZones": [{"SubnetId": "subnet-p1"}, {"SubnetId": "subnet-p2"}]}
    elbv2 = Fake(describe_target_groups={"TargetGroups": [{"LoadBalancerArns": [LB]}]},
                 describe_load_balancers={"LoadBalancers": [lb]},
                 describe_target_health={"TargetHealthDescriptions": [{"TargetHealth": {"State": s}} for s in health]})
    routes = [{"GatewayId": "local"}] + ([{"GatewayId": "igw-1", "DestinationCidrBlock": "0.0.0.0/0"}] if igw_route else [])
    from_alb = [rule(8080, groups=["sg-alb"])]
    from_cloudfront = [rule(80, prefix_lists=["pl-22a6434b"])]
    # The shape AWS returned in the 2026-10-08 E2E: ONE private route table explicitly associated with BOTH ALB subnets.
    ec2 = Fake(describe_route_tables={"RouteTables": route_tables if route_tables is not None else [
                   {"RouteTableId": "rtb-private", "Routes": routes,
                    "Associations": [{"SubnetId": s, "RouteTableId": "rtb-private", "Main": False} for s in ("subnet-p1", "subnet-p2")]}]},
               describe_security_groups={"SecurityGroups": [
                   {"GroupId": "sg-task", "IpPermissions": from_alb if task_ingress is None else task_ingress},
                   {"GroupId": "sg-alb", "IpPermissions": from_cloudfront if alb_ingress is None else alb_ingress}]})
    logs = Fake(describe_log_groups={"logGroups": [{"logGroupName": CTX["log_group"], "retentionInDays": retention}]})
    repo = {"imageScanningConfiguration": {"scanOnPush": scan}, "imageTagMutability": "IMMUTABLE",
            "encryptionConfiguration": {"encryptionType": "AES256"}}
    ecr = Fake(describe_repositories={"repositories": [repo]})
    origin = {"DomainName": "internal-sscp-shared", **({"VpcOriginConfig": {"VpcOriginId": "vo_1"}} if vpc_origin else {})}
    log_cfg = {"Enabled": logging, "Bucket": "sscp-logs.s3.amazonaws.com" if logging else "", "Prefix": "envs/x/"}
    cloudfront = Fake(get_distribution_config={"DistributionConfig": {"Logging": log_cfg, "Origins": {"Items": [origin]}}})
    trail = Fake(get_trail_status={"IsLogging": True})
    return dict(ecs=ecs, elbv2=elbv2, ec2=ec2, iam=iam, logs=logs, ecr=ecr, cloudfront=cloudfront, cloudtrail=trail)


def test_container_state_happy_path():
    results = validate_container_state(**clients(), ctx=CTX)
    assert not fails(results), fails(results)
    assert verdict(results)["verdict"] == "READY"
    got = by_id(results)
    assert got["SB-SEC-01"] == {NOT_APPLICABLE} and got["SB-PUB-02"] == {PASS} and got["PR-C-03"] == {PASS}


def _rt(rid, subnets, igw=False):
    routes = [{"GatewayId": "local"}] + ([{"GatewayId": "igw-1", "DestinationCidrBlock": "0.0.0.0/0"}] if igw else [])
    return {"RouteTableId": rid, "Routes": routes,
            "Associations": [{"SubnetId": s, "RouteTableId": rid, "Main": False} for s in subnets]}


@pytest.mark.parametrize("tables,expected", [
    ([_rt("rtb-private", ["subnet-p1", "subnet-p2"])], PASS),                  # E2E 2026-10-08: 1 table, 2 subnets
    ([_rt("rtb-a", ["subnet-p1"]), _rt("rtb-b", ["subnet-p2"])], PASS),         # one table per subnet
    ([_rt("rtb-private", ["subnet-p1"])], FAIL),                               # subnet-p2 falls back to the main table
    ([], FAIL),                                                                # nothing explicitly associated
    ([_rt("rtb-a", ["subnet-p1"]), _rt("rtb-b", ["subnet-p2"], igw=True)], FAIL),  # one ALB subnet routes to the internet
])
def test_alb_subnet_route_tables(tables, expected):
    """SB-PUB-02 false positive (D-179): a shared route table must not fail, an unseen or public one must."""
    results = validate_container_state(**clients(route_tables=tables), ctx=CTX)
    assert by_id(results)["SB-PUB-02"] == {expected}, [r.detail for r in results if r.check_id == "SB-PUB-02"]


def test_multi_az_requires_tasks_in_two_azs():
    ctx = {**CTX, "multi_az": "MULTI", "min_tasks": 2}
    one_az = validate_container_state(**clients(running=2, desired=2, health=("healthy", "healthy"),
                                                azs=("ap-northeast-2a", "ap-northeast-2a")), ctx=ctx)
    assert by_id(one_az)["PR-C-04"] == {FAIL}
    two = validate_container_state(**clients(running=2, desired=2, health=("healthy", "healthy"),
                                             azs=("ap-northeast-2a", "ap-northeast-2c")), ctx=ctx)
    assert by_id(two)["PR-C-04"] == {PASS}


OPEN = [rule(8080, cidrs=["0.0.0.0/0"])]


@pytest.mark.parametrize("kw,failing", [
    ({"running": 0}, "PR-C-04"),
    ({"breaker": False}, "PR-C-05"),
    ({"desired": 2, "running": 2}, "PR-C-06"),               # more than the recommended (and priced) minimum
    ({"health": ("unhealthy",)}, "PR-C-03"),
    ({"image": "docker.io/traefik/whoami:v1"}, "SB-PUB-07"),  # not the platform's pinned copy
    ({"scan": False}, "SB-PUB-07"),
    ({"boundary": None}, "SB-IAM-04"),
    ({"policy_action": "*"}, "SB-IAM-02"),
    ({"env": {"API_TOKEN": "x"}}, "SB-SEC-02"),
    ({"scheme": "internet-facing"}, "SB-PUB-02"),
    ({"igw_route": True}, "SB-PUB-02"),
    ({"task_ingress": OPEN}, "SB-PUB-03"),
    ({"task_ingress": OPEN}, "SB-PUB-10"),
    ({"alb_ingress": OPEN}, "SB-PUB-04"),
    ({"logging": False}, "SB-LOG-02"),
    ({"vpc_origin": False}, "PR-C-01"),
    ({"retention": None}, "SB-LOG-04"),
])
def test_container_state_negative(kw, failing):
    results = validate_container_state(**clients(**kw), ctx=CTX)
    assert by_id(results)[failing] == {FAIL}, by_id(results)
    failed = next(r for r in results if r.check_id == failing and r.status == FAIL)
    assert verdict(results)["security_failed"] == any(r.status == FAIL and r.dimension == "Security" for r in results)
    assert failed.layer == "STATE"


# ---------------------------------------------------------------- HTTP (local server as CloudFront)

@pytest.fixture
def edge_server():
    mode = {"headers": True, "status": 200, "body": b"hello from container", "x_cache": "Miss from cloudfront"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(mode["status"])
            if mode["headers"]:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Cache", mode["x_cache"])
            self.end_headers()
            self.wfile.write(mode["body"])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", srv.server_address[1], mode
    srv.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_container_http_happy_path(edge_server):
    url, _, _ = edge_server
    ids = by_id(validate_container_http(base_url=url, alb_dns_name="localhost", task_public_ips=["127.0.0.1"],
                                        app_port=_free_port(), attempts=1, delay=0, connect_timeout=0.5))
    assert ids["PR-ALL-02"] == {PASS} and ids["PR-ALL-04"] == {PASS}
    assert ids["PR-C-01"] == {PASS}, "localhost resolves to private addresses only"
    assert ids["PR-C-02"] == {PASS}, "nothing listens on the task's app port"
    assert ids["PR-C-07"] == {NOT_APPLICABLE}


def test_task_reachable_directly_fails_pr_c_02(edge_server):
    url, port, _ = edge_server  # the edge server's own port stands in for an exposed task port
    ids = by_id(validate_container_http(base_url=url, alb_dns_name="localhost", task_public_ips=["127.0.0.1"],
                                        app_port=port, attempts=1, delay=0, connect_timeout=0.5))
    assert ids["PR-C-02"] == {FAIL}


def test_public_alb_address_fails_pr_c_01(edge_server):
    url, _, _ = edge_server
    ids = by_id(validate_container_http(base_url=url, alb_dns_name="8.8.8.8", attempts=1, delay=0))
    assert ids["PR-C-01"] == {FAIL}


@pytest.mark.parametrize("change", [{"x_cache": "Error from cloudfront", "status": 200}, {"body": b"no environment"},
                                    {"status": 502}])
def test_edge_or_alb_default_responses_are_not_the_app(edge_server, change):
    url, _, mode = edge_server
    mode.update(change)
    assert by_id(validate_container_http(base_url=url, alb_dns_name="localhost", attempts=1, delay=0))["PR-ALL-02"] == {FAIL}


def test_missing_security_headers_fail(edge_server):
    url, _, mode = edge_server
    mode["headers"] = False
    assert by_id(validate_container_http(base_url=url, alb_dns_name="localhost", attempts=1, delay=0))["PR-ALL-04"] == {FAIL}


def test_allowlist_requires_a_403_for_this_checker(edge_server):
    url, _, mode = edge_server
    allow = ["203.0.113.0/24"]
    def pr_c_07():
        return by_id(validate_container_http(base_url=url, alb_dns_name="localhost", ip_allowlist=allow, attempts=1, delay=0))["PR-C-07"]
    assert pr_c_07() == {FAIL}
    mode["status"] = 403
    assert pr_c_07() == {PASS}


def test_plain_http_page_fails_https_redirect(edge_server):
    url, _, _ = edge_server  # the fake edge answers plain HTTP with 200 instead of redirecting
    ids = by_id(validate_container_http(base_url=url.replace("http://", "https://"), alb_dns_name="localhost", attempts=1, delay=0))
    assert ids["PR-ALL-03"] == {FAIL}


# ---------------------------------------------------------------- coverage

CONTAINER_VALIDATORS = {
    "SB-ENC-04": "state(n/a)", "SB-ENC-05": "state", "SB-IAM-01": "state", "SB-IAM-02": "plan,state", "SB-IAM-04": "plan,state",
    "SB-LOG-02": "plan,state", "SB-LOG-04": "plan,state", "SB-PUB-02": "plan(shared),state,http", "SB-PUB-03": "plan,state,http",
    "SB-PUB-04": "plan(shared),state", "SB-PUB-07": "plan,state", "SB-PUB-09": "plan", "SB-PUB-10": "plan,state",
    "SB-SEC-01": "state(n/a)", "SB-SEC-02": "artifact,plan,state", "SB-SEC-05": "state(n/a)", "SB-TAG-01": "plan,state",
    "SB-TLS-01": "plan,http", "SB-TLS-05": "plan,http",
}


def test_container_baseline_coverage_is_complete():
    assert set(baseline.applicable({"C"})) == set(CONTAINER_VALIDATORS)


@requires_terraform
def test_every_listed_container_check_is_emitted_and_worded(container_plan, shared_container_plan):
    results: list[CheckResult] = list(validate_image_spec({"image": "nginx:1"})[0])
    results += merge_shared(validate_plan(container_plan, blueprint="container", fixed_cost_reasons=FIX),
                            validate_plan(shared_container_plan, blueprint="container-shared", fixed_cost_reasons=FIX))
    results += validate_container_state(**clients(), ctx=CTX)
    results += validate_container_http(base_url="http://127.0.0.1:9", alb_dns_name="localhost", attempts=1, delay=0)
    ids = {r.check_id for r in results} | {"PR-ALL-06"}  # PR-ALL-06 is the shared check_prod_monitoring
    listed = {i for i, *_ in CONTAINER_CHECKS}
    assert listed <= ids, listed - ids
    assert ids <= set(CHECK_TITLES), ids - set(CHECK_TITLES)
    baselines = {b for r in results for b in r.baseline_ids}
    assert set(CONTAINER_VALIDATORS) <= baselines, set(CONTAINER_VALIDATORS) - baselines
