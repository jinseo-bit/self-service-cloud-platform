"""Serverless blueprint validators (D-165): image input, PLAN (real offline terraform plan), STATE (fake AWS
clients with real response shapes), HTTP (a local server standing in for the HTTP API)."""

import copy
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from platform_core import baseline
from platform_core.production_ready import CHECK_TITLES, SERVERLESS_CHECKS
from platform_core.validation.http import validate_serverless_http
from platform_core.validation.image import normalize_image, validate_image_spec
from platform_core.validation.plan import validate_plan
from platform_core.validation.result import FAIL, NOT_APPLICABLE, PASS, verdict
from platform_core.validation.state import validate_serverless_state

from .conftest import requires_terraform


def by_id(results):
    out = {}
    for r in results:
        out.setdefault(r.check_id, set()).add(r.status)
    return out


# ---------------------------------------------------------------- image input (ARTIFACT)

@pytest.mark.parametrize("ref,full", [
    ("traefik/whoami:v1.10.3", "docker.io/traefik/whoami:v1.10.3"),
    ("nginx:1.27", "docker.io/library/nginx:1.27"),
    ("public.ecr.aws/docker/library/python:3.12-slim", "public.ecr.aws/docker/library/python:3.12-slim"),
])
def test_image_references_are_normalized(ref, full):
    assert normalize_image(ref) == full


def test_valid_image_spec_passes():
    checks, spec = validate_image_spec({"image": "ghcr.io/acme/app@sha256:" + "b" * 64, "env": {"GREETING": "hi"},
                                        "health_path": "/healthz"})
    assert all(c.status == PASS for c in checks), [(c.check_id, c.detail) for c in checks]
    assert spec["env"] == {"GREETING": "hi"} and spec["health_path"] == "/healthz"


@pytest.mark.parametrize("spec,failing", [
    ({"image": "traefik/whoami"}, "IMG-REF"),                                    # no tag or digest
    ({"image": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/x:1"}, "IMG-REF"),  # private registry
    ({"image": "not an image"}, "IMG-REF"),
    ({"image": "nginx:1", "env": {"DB_PASSWORD": "x"}}, "SB-SEC-02"),             # secret-looking key
    ({"image": "nginx:1", "env": {"KEY_ID": "AKIAABCDEFGHIJKLMNOP"}}, "SB-SEC-02"),   # AWS key in a value
    ({"image": "nginx:1", "env": {"AWS_REGION": "x"}}, "IMG-ENV"),                 # reserved
    ({"image": "nginx:1", "env": {"PORT": "9000"}}, "IMG-ENV"),                    # set by the platform
    ({"image": "nginx:1", "env": {"1BAD": "x"}}, "IMG-ENV"),
    ({"image": "nginx:1", "health_path": "health"}, "IMG-HEALTH"),
])
def test_invalid_image_specs_fail_before_anything_is_created(spec, failing):
    checks, _ = validate_image_spec(spec)
    assert by_id(checks)[failing] == {FAIL}


# ---------------------------------------------------------------- PLAN

@requires_terraform
def test_serverless_plan_has_no_failures_and_no_fixed_cost_resources(serverless_plan):
    results = validate_plan(serverless_plan, blueprint="serverless")
    assert not [r for r in results if r.status == FAIL], [(r.check_id, r.detail) for r in results if r.status == FAIL]
    ids = by_id(results)
    for cid in ("SB-IAM-04", "SB-SEC-02", "SB-LOG-03", "SB-LOG-04", "SB-PUB-06", "SB-PUB-06/permission", "SB-PUB-07",
                "SB-PUB-07/immutable", "SB-PUB-09", "SB-TAG-01", "PLAN-ALLOWLIST"):
        assert ids[cid] == {PASS}, cid
    types = {r["type"] for r in serverless_plan["resource_changes"]}
    assert not types & {"aws_nat_gateway", "aws_lb", "aws_ecs_service", "aws_instance", "aws_eks_cluster", "aws_vpc"}


@requires_terraform
def test_serverless_prod_plan_adds_monitoring(serverless_plan_prod):
    types = [r["type"] for r in serverless_plan_prod["resource_changes"]]
    assert types.count("aws_cloudwatch_metric_alarm") == 2 and "aws_sns_topic_subscription" in types
    assert not [r for r in validate_plan(serverless_plan_prod, blueprint="serverless") if r.status == FAIL]


def _rc(plan, rtype):
    return next(r for r in plan["resource_changes"] if r["type"] == rtype)


@requires_terraform
def test_negative_serverless_plan_violations_are_caught(serverless_plan):
    bad = copy.deepcopy(serverless_plan)
    _rc(bad, "aws_iam_role")["change"]["after"]["permissions_boundary"] = None
    _rc(bad, "aws_lambda_function")["change"]["after"]["environment"][0]["variables"]["DB_PASSWORD"] = "hunter2"
    _rc(bad, "aws_apigatewayv2_stage")["change"]["after"]["access_log_settings"] = []
    _rc(bad, "aws_apigatewayv2_stage")["change"]["after_unknown"].pop("access_log_settings", None)
    _rc(bad, "aws_ecr_repository")["change"]["after"]["image_scanning_configuration"] = [{"scan_on_push": False}]
    _rc(bad, "aws_ecr_repository")["change"]["after"]["image_tag_mutability"] = "MUTABLE"
    _rc(bad, "aws_lambda_permission")["change"]["after"]["principal"] = "*"
    bad["resource_changes"].append({"address": "aws_lambda_function_url.open", "type": "aws_lambda_function_url",
                                    "change": {"actions": ["create"], "after": {}, "after_unknown": {}}})
    bad["resource_changes"].append({"address": "aws_nat_gateway.oops", "type": "aws_nat_gateway",
                                    "change": {"actions": ["create"], "after": {}, "after_unknown": {}}})
    ids = by_id(validate_plan(bad, blueprint="serverless"))
    for cid in ("SB-IAM-04", "SB-SEC-02", "SB-LOG-03", "SB-PUB-07", "SB-PUB-07/immutable", "SB-PUB-06",
                "SB-PUB-06/permission", "SB-PUB-09", "PLAN-ALLOWLIST"):
        assert ids[cid] == {FAIL}, cid


@requires_terraform
def test_static_plan_is_unaffected_and_inside_its_allowlist(static_plan):
    results = validate_plan(static_plan, blueprint="static")
    assert not [r for r in results if r.status == FAIL]
    assert by_id(results)["PLAN-ALLOWLIST"] == {PASS}
    assert "SB-IAM-04" not in by_id(results)  # serverless-only checks are not attached to a Static plan


# ---------------------------------------------------------------- STATE (fake clients, real response shapes)

ACCT, REPO = "111122223333", "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-env-abcd1234"
BOUNDARY = f"arn:aws:iam::{ACCT}:policy/sscp-env-boundary"
FN_ARN = f"arn:aws:lambda:ap-northeast-2:{ACCT}:function:sscp-env-abcd1234"
TAGS = {"project": "sscp", "managed-by": "platform", "env-id": "env-abcd1234", "owner": "t", "tier": "DEV",
        "blueprint": "serverless", "expires-at": "2026-10-11T00:00:00Z"}
CTX = {"function_name": "sscp-env-abcd1234", "role_name": "sscp-env-abcd1234", "api_id": "abc123",
       "log_group": "/aws/lambda/sscp-env-abcd1234", "repository_name": "sscp-env-abcd1234", "repository_url": REPO,
       "env_boundary_arn": BOUNDARY, "tier": "DEV", "log_retention_days": 7, "trail_name": "sscp-account-trail"}


class Fake:
    def __init__(self, **calls):
        self.calls = calls

    def __getattr__(self, name):
        value = self.calls[name]
        return value if callable(value) else (lambda **_: value)


def clients(boundary=BOUNDARY, url_configs=(), retention=7, policy_action="logs:PutLogEvents", env=None,
            image=f"{REPO}@sha256:" + "c" * 64, scan=True, access_log=True, principal="apigateway.amazonaws.com"):
    lam = Fake(
        get_function={"Configuration": {"State": "Active", "LastUpdateStatus": "Successful", "PackageType": "Image",
                                        "Role": f"arn:aws:iam::{ACCT}:role/sscp-env-abcd1234", "Timeout": 29, "MemorySize": 512,
                                        "FunctionArn": FN_ARN, "Environment": {"Variables": env or {"PORT": "8080"}}},
                      "Code": {"ImageUri": image}},
        list_function_url_configs={"FunctionUrlConfigs": list(url_configs)},
        get_policy={"Policy": json.dumps({"Statement": [{"Principal": {"Service": principal}, "Condition": {
            "ArnLike": {"AWS:SourceArn": f"arn:aws:execute-api:ap-northeast-2:{ACCT}:abc123/*/*"}}}]})},
        list_tags={"Tags": TAGS})
    iam = Fake(get_role={"Role": {"Arn": f"arn:aws:iam::{ACCT}:role/sscp-env-abcd1234",
                                  **({"PermissionsBoundary": {"PermissionsBoundaryArn": boundary}} if boundary else {})}},
               list_role_policies={"PolicyNames": ["inline"]},
               get_role_policy={"PolicyDocument": {"Statement": [{
                   "Effect": "Allow", "Action": [policy_action],
                   "Resource": "arn:aws:logs:x:y:log-group:/aws/lambda/sscp-env-abcd1234:*"}]}})
    logs = Fake(describe_log_groups={"logGroups": [{"logGroupName": CTX["log_group"], "retentionInDays": retention}]})
    apigw = Fake(get_stage={"AccessLogSettings": {"DestinationArn": "arn:aws:logs:...:api-access"} if access_log else {},
                            "DefaultRouteSettings": {"ThrottlingRateLimit": 20.0}})
    ecr = Fake(describe_repositories={"repositories": [{"imageScanningConfiguration": {"scanOnPush": scan},
                                                        "imageTagMutability": "IMMUTABLE",
                                                        "encryptionConfiguration": {"encryptionType": "AES256"}}]})
    trail = Fake(get_trail_status={"IsLogging": True})
    return dict(lambda_=lam, iam=iam, logs=logs, apigw=apigw, ecr=ecr, cloudtrail=trail)


def test_serverless_state_happy_path():
    results = validate_serverless_state(**clients(), ctx=CTX)
    fails = [(r.check_id, r.detail) for r in results if r.status == FAIL]
    assert not fails, fails
    assert verdict(results)["verdict"] == "READY"
    assert by_id(results)["SB-SEC-01"] == {NOT_APPLICABLE}


@pytest.mark.parametrize("kw,failing", [
    ({"boundary": None}, "SB-IAM-04"),
    ({"boundary": f"arn:aws:iam::{ACCT}:policy/other"}, "SB-IAM-04"),
    ({"url_configs": [{"FunctionUrl": "https://x.lambda-url"}]}, "SB-PUB-06"),
    ({"principal": "*"}, "SB-PUB-06/permission"),
    ({"retention": None}, "SB-LOG-04"),
    ({"policy_action": "*"}, "SB-IAM-02"),
    ({"env": {"API_TOKEN": "x"}}, "SB-SEC-02"),
    ({"image": "docker.io/traefik/whoami:v1"}, "PR-L-02"),  # not the platform-built image
    ({"scan": False}, "SB-PUB-07"),
    ({"access_log": False}, "SB-LOG-03"),
])
def test_serverless_state_negative(kw, failing):
    results = validate_serverless_state(**clients(**kw), ctx=CTX)
    assert by_id(results)[failing] == {FAIL}
    failed = next(r for r in results if r.check_id == failing)
    # Security failures disable the endpoint (D-65); others (e.g. log retention) make the environment DEGRADED/FAILED.
    assert verdict(results)["security_failed"] == (failed.dimension == "Security")


# ---------------------------------------------------------------- HTTP (local server as the HTTP API)

@pytest.fixture
def app_server():
    mode = {"headers": True, "status": 200, "body": b"hello from app", "gw_error": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(mode["status"])
            if mode["headers"]:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
            if mode["gw_error"]:
                self.send_header("x-amzn-ErrorType", "InternalServerErrorException")
            self.end_headers()
            self.wfile.write(mode["body"])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", mode
    srv.shutdown()


def test_serverless_http_happy_path(app_server):
    url, _ = app_server  # the fixture speaks plain HTTP, so the content checks are probed against it directly
    ids = by_id(validate_serverless_http(base_url=url, attempts=1, delay=0))
    assert ids["PR-ALL-02"] == {PASS} and ids["PR-L-05"] == {PASS} and ids["PR-ALL-04"] == {PASS}


def test_serverless_http_detects_missing_headers_and_gateway_errors(app_server):
    url, mode = app_server
    mode["headers"] = False
    assert by_id(validate_serverless_http(base_url=url, attempts=1, delay=0))["PR-ALL-04"] == {FAIL}
    mode.update(headers=True, status=500, gw_error=True, body=b'{"message":"Internal Server Error"}')
    ids = by_id(validate_serverless_http(base_url=url, attempts=1, delay=0))
    assert ids["PR-ALL-02"] == {FAIL} and ids["PR-L-05"] == {FAIL}


def test_https_only_check_treats_a_plain_http_page_as_failure(app_server):
    url, _ = app_server  # the fake server answers plain HTTP with 200 — exactly what must not happen
    ids = by_id(validate_serverless_http(base_url=url.replace("http://", "https://"), attempts=1, delay=0))
    assert ids["PR-ALL-03"] == {FAIL}


# ---------------------------------------------------------------- coverage

SERVERLESS_VALIDATORS = {
    "SB-IAM-01": "state", "SB-IAM-02": "plan,state", "SB-IAM-04": "plan,state", "SB-ENC-04": "state(n/a)",
    "SB-ENC-05": "state", "SB-TLS-02": "http", "SB-SEC-01": "state(n/a)", "SB-SEC-02": "artifact,plan,state",
    "SB-SEC-05": "state(n/a)", "SB-PUB-06": "plan,state", "SB-PUB-07": "plan,state", "SB-PUB-09": "plan",
    "SB-PUB-10": "plan", "SB-LOG-03": "plan,state", "SB-LOG-04": "plan,state", "SB-TAG-01": "plan,state",
}


def test_serverless_baseline_coverage_is_complete():
    assert set(baseline.applicable({"L"})) == set(SERVERLESS_VALIDATORS)


def _all_emitted(plan):
    ids = {r.check_id for r in validate_image_spec({"image": "nginx:1"})[0]}
    ids |= {r.check_id for r in validate_plan(plan, blueprint="serverless")}
    ids |= {r.check_id for r in validate_serverless_state(**clients(), ctx=CTX)} | {"PR-ALL-06"}
    ids |= {r.check_id for r in validate_serverless_http(base_url="http://127.0.0.1:9", attempts=1, delay=0)}
    return ids


@requires_terraform
def test_every_listed_serverless_check_is_emitted_and_worded(serverless_plan):
    emitted = _all_emitted(serverless_plan)
    assert not {c[0] for c in SERVERLESS_CHECKS} - emitted
    assert not emitted - set(CHECK_TITLES), emitted - set(CHECK_TITLES)
    baselines = {b for r in validate_serverless_state(**clients(), ctx=CTX) for b in r.baseline_ids}
    baselines |= {b for r in validate_plan(serverless_plan, blueprint="serverless") for b in r.baseline_ids}
    baselines |= {b for r in validate_serverless_http(base_url="http://127.0.0.1:9", attempts=1, delay=0) for b in r.baseline_ids}
    assert set(SERVERLESS_VALIDATORS) <= baselines, set(SERVERLESS_VALIDATORS) - baselines


# ---------------------------------------------------------------- runner permissions vs blueprint

LOG_DELIVERY = {"logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies"}


def runner_allowed_statements():
    """{sid: (effect, actions, resources)} parsed from local.runner_allowed in infra/platform/runner.tf."""
    tf = (Path(__file__).resolve().parents[2] / "infra" / "platform" / "runner.tf").read_text(encoding="utf-8")
    body = tf.split("  runner_allowed = {", 1)[1].split("\n  }\n", 1)[0]
    out = {}
    for sid, block in re.findall(r"^    (\w+) = \{(.*?)^    \}", body, re.S | re.M):
        effect = re.search(r'effect\s*=\s*"(\w+)"', block)
        actions = re.search(r"actions\s*=\s*\[(.*?)\]", block, re.S)
        resources = re.search(r"resources\s*=\s*\[(.*?)\]", block, re.S)
        out[sid] = (effect.group(1) if effect else "Allow", set(re.findall(r'"([^"]+)"', actions.group(1))),
                    set(re.findall(r'"([^"]+)"', resources.group(1))) if resources else set())
    return out


@requires_terraform
def test_runner_can_deliver_http_api_access_logs(serverless_plan):
    """An HTTP API stage with access_log_settings needs the vended log-delivery actions on the caller (the runner);
    without them stage creation fails after the image is already built. AWS: apigateway http-api-logging."""
    change = _rc(serverless_plan, "aws_apigatewayv2_stage")["change"]
    assert change["after"].get("access_log_settings") or change["after_unknown"].get("access_log_settings")
    granted = set()
    for effect, actions, resources in runner_allowed_statements().values():
        if effect == "Allow" and "*" in resources:
            granted |= actions & LOG_DELIVERY
    assert granted == LOG_DELIVERY, sorted(LOG_DELIVERY - granted)
    denied = {a for e, acts, _ in runner_allowed_statements().values() if e == "Deny" for a in acts}
    assert not denied & LOG_DELIVERY


@requires_terraform
def test_runner_can_create_tagged_api_gateway_resources(serverless_plan):
    """default_tags put tags on the HTTP API and its stage; CreateStage with tags is also authorized as
    apigateway:TagResource (Serverless E2E 2026-10-07: AccessDenied on /apis/{id}/stages). The platform's own API
    stays denied."""
    tagged = [r for r in serverless_plan["resource_changes"] if r["type"].startswith("aws_apigatewayv2_")
              and (r["change"]["after"] or {}).get("tags_all")]
    assert {r["type"] for r in tagged} >= {"aws_apigatewayv2_api", "aws_apigatewayv2_stage"}
    st = runner_allowed_statements()
    effect, actions, resources = st["ServerlessApis"]
    assert effect == "Allow" and {"apigateway:POST", "apigateway:TagResource"} <= actions
    assert any(r.endswith("/apis/*") for r in resources)
    effect, actions, _ = st["PlatformApiIsOffLimits"]
    assert effect == "Deny" and "apigateway:TagResource" in actions


@requires_terraform
def test_final_plan_still_checks_resources_that_already_exist(serverless_plan):
    """Serverless applies the ECR repository before the image build, so the final plan sees it as no-op. It is still
    part of the final configuration: SB-PUB-07 must be checked there, not shown as N/A (E2E 2026-10-07)."""
    final = copy.deepcopy(serverless_plan)
    for rc in final["resource_changes"]:
        if rc["type"].startswith("aws_ecr_"):
            rc["change"]["actions"] = ["no-op"]
            rc["change"]["before"] = rc["change"]["after"]
    got = by_id(validate_plan(final, blueprint="serverless"))
    assert got["SB-PUB-07"] == {PASS} and got["SB-PUB-07/immutable"] == {PASS}
    _rc(final, "aws_ecr_repository")["change"]["after"]["image_scanning_configuration"] = [{"scan_on_push": False}]
    assert by_id(validate_plan(final, blueprint="serverless"))["SB-PUB-07"] == {FAIL}
    for rc in final["resource_changes"]:
        if rc["type"].startswith("aws_ecr_"):
            rc["change"]["actions"] = ["delete"]
    assert by_id(validate_plan(final, blueprint="serverless"))["SB-PUB-07"] == {NOT_APPLICABLE}
