"""Day-2 drift detection logic (D-186). Pure functions; the worker adds the AWS calls.

Drift = a check that passed when the environment was approved and fails now. A recheck runs the same STATE and HTTP
checks as validation; only failures that are NEW compared with the environment's last verdict count, so an
environment that was already DEGRADED does not flap.

Two triggers: an EventBridge rule on CloudTrail write events (seconds to a minute) and the hourly sweep (catches
what the rule cannot see: CloudFront and IAM are global services whose events arrive in us-east-1 only).
"""

from __future__ import annotations

import json

from platform_core.validation.result import FAIL, CheckResult

# Write API calls that can change what the checks verify. The EventBridge rule in infra/platform/day2.tf uses the
# same lists (tests/test_day2_rule_sync.py keeps them identical). Read-only calls never match a rule in the default
# ENABLED state, so Describe/Get noise cannot trigger a recheck.
WATCHED_EVENTS: dict[str, list[str]] = {
    "aws.ec2": ["AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress", "RevokeSecurityGroupIngress",
                "RevokeSecurityGroupEgress", "ModifySecurityGroupRules", "ModifyNetworkInterfaceAttribute",
                "CreateRoute", "ReplaceRoute", "AssociateRouteTable", "ReplaceRouteTableAssociation"],
    "aws.s3": ["PutBucketPolicy", "DeleteBucketPolicy", "PutBucketPublicAccessBlock", "DeleteBucketPublicAccessBlock",
               "PutBucketAcl", "PutBucketEncryption", "DeleteBucketEncryption", "PutBucketOwnershipControls",
               "PutBucketLogging"],
    "aws.lambda": ["AddPermission20150331v2", "RemovePermission20150331v2", "CreateFunctionUrlConfig20211031",
                   "UpdateFunctionUrlConfig20211031", "UpdateFunctionConfiguration20150331v2", "PutFunctionConcurrency20171031"],
    "aws.apigateway": ["UpdateStage", "DeleteStage", "CreateRoute", "UpdateRoute", "UpdateIntegration", "UpdateApi"],
    "aws.ecs": ["UpdateService"],
    "aws.elasticloadbalancing": ["ModifyLoadBalancerAttributes", "ModifyListener", "CreateListener", "ModifyRule",
                                 "CreateRule", "SetSecurityGroups", "SetSubnets"],
    "aws.logs": ["DeleteLogGroup", "PutRetentionPolicy", "DeleteRetentionPolicy"],
}

RECHECKABLE = ("READY", "DEGRADED", "DRIFTED")


def failed_ids(results: list[CheckResult] | list[dict]) -> set[str]:
    out = set()
    for r in results:
        status = r.status if isinstance(r, CheckResult) else r.get("status")
        cid = r.check_id if isinstance(r, CheckResult) else r.get("check_id")
        if status == FAIL:
            out.add(cid)
    return out


def new_failures(env: dict, results: list[CheckResult]) -> list[CheckResult]:
    """Failures that were not failing at the last verdict (approval or the last recheck)."""
    before = set((env.get("verdict") or {}).get("failed") or [])
    if env.get("status") == "DRIFTED":  # already drifted: compare with the approved state, not the drifted one
        before = set((env.get("drift") or {}).get("baseline_failed") or before)
    return [r for r in results if r.status == FAIL and r.check_id not in before]


def block_effects(env: dict, results: list[CheckResult]) -> set[str]:
    """Failures caused by the platform's own endpoint block (D-65) on a drifted environment: CloudFront disabled
    (or Lambda concurrency 0) makes the CDN check and every HTTP probe fail. They are not someone's new change."""
    if env.get("status") != "DRIFTED" or not (env.get("drift") or {}).get("endpoint_disabled"):
        return set()
    return {r.check_id for r in results if r.status == FAIL and (r.layer == "HTTP" or r.check_id == "PR-ALL-02/enabled")}


def is_platform_actor(detail: dict, prefix: str) -> bool:
    """The platform's own roles (runner, workers, publisher) change resources on purpose; that is not drift."""
    ident = detail.get("userIdentity") or {}
    issuer = ((ident.get("sessionContext") or {}).get("sessionIssuer") or {}).get("userName", "")
    arn = ident.get("arn", "")
    return issuer.startswith(f"{prefix}-") or f":assumed-role/{prefix}-" in arn


def identifiers(env: dict) -> set[str]:
    """Names and ids that show up in CloudTrail request parameters for this environment's resources."""
    ids = {env["env_id"], f"sscp-{env['env_id']}"}
    for v in (env.get("outputs") or {}).values():
        if isinstance(v, str) and len(v) >= 8:
            ids.add(v)
            ids.add(v.rsplit("/", 1)[-1].rsplit(":", 1)[-1])  # ARN or URL -> its last segment
        elif isinstance(v, list):
            ids.update(x for x in v if isinstance(x, str) and len(x) >= 8)
    return {i for i in ids if i}


def match_environments(detail: dict, envs: list[dict], prefix: str) -> list[str]:
    """Environments an API call touched. Shared container infrastructure (VPC, ALB) touches every container env."""
    if is_platform_actor(detail, prefix):
        return []
    blob = json.dumps({k: detail.get(k) for k in ("requestParameters", "responseElements", "resources")}, default=str)
    hits = []
    for env in envs:
        if env.get("status") not in RECHECKABLE:
            continue
        shared = set(identifiers({"env_id": "shared", "outputs": env.get("shared") or {}})) - {"shared", "sscp-shared"}
        if any(i in blob for i in identifiers(env)) or any(i in blob for i in shared):
            hits.append(env["env_id"])
    return hits


def drift_record(env: dict, fails: list[CheckResult], trigger: str, detail: dict | None, detected_at: str) -> dict:
    detail = detail or {}
    ident = detail.get("userIdentity") or {}
    return {
        "detected_at": detected_at,
        "trigger": trigger,  # EVENT | SCHEDULE | MANUAL
        "event_name": detail.get("eventName"),
        "event_time": detail.get("eventTime"),
        "actor": ident.get("arn") or ident.get("principalId"),
        "source_ip": detail.get("sourceIPAddress"),
        "failed": [{"check_id": r.check_id, "dimension": r.dimension, "detail": r.detail} for r in fails],
        "security": any(r.dimension == "Security" for r in fails),
        "baseline_failed": sorted((env.get("verdict") or {}).get("failed") or []),
    }


def seconds_between(start: str | None, end: str | None) -> int | None:
    """CloudTrail eventTime -> detection, the time-to-detect metric."""
    import datetime as dt
    if not start or not end:
        return None
    parse = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return max(0, int((parse(end) - parse(start)).total_seconds()))
