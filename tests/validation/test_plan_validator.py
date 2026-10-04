from platform_core.validation.plan import validate_plan
from platform_core.validation.result import DEFERRED, FAIL, PASS

from .conftest import requires_terraform

pytestmark = requires_terraform


def by_id(results):
    out = {}
    for r in results:
        out.setdefault(r.check_id, []).append(r)
    return out


def test_static_blueprint_plan_has_no_failures(static_plan):
    results = validate_plan(static_plan)
    fails = [r for r in results if r.status == FAIL]
    assert not fails, [r.to_dict() for r in fails]
    ids = by_id(results)
    for must_pass in ("SB-PUB-01", "SB-ENC-01", "SB-TLS-01", "SB-TLS-05", "SB-LOG-02", "SB-TAG-01", "SB-PUB-09", "SB-PUB-10"):
        assert any(r.status == PASS for r in ids[must_pass]), must_pass


def test_static_blueprint_defers_policy_to_state(static_plan):
    deferred = {r.check_id for r in validate_plan(static_plan) if r.status == DEFERRED}
    # Bucket policy references the distribution ARN, unknown until apply → STATE covers it.
    assert deferred <= {"SB-TLS-04", "SB-PUB-01/policy"}


def test_static_allowlist_plan_has_waf(static_plan_allowlist):
    r = [x for x in validate_plan(static_plan_allowlist, ip_allowlist=["203.0.113.0/24"]) if x.check_id == "SB-PUB-08"]
    assert r and r[0].status == PASS


def test_allowlist_requested_but_missing_waf_fails(static_plan):
    r = [x for x in validate_plan(static_plan, ip_allowlist=["203.0.113.0/24"]) if x.check_id == "SB-PUB-08"]
    assert r[0].status == FAIL


def test_negative_violating_plan_is_caught(violating_plan):
    results = by_id(validate_plan(violating_plan))
    expected_failures = {
        "SB-PUB-01": "BPA partially disabled",
        "SB-ENC-01": "no SSE",
        "SB-TLS-01": "allow-all viewer protocol",
        "SB-TLS-05": "no security headers",
        "SB-LOG-02": "no access logs",
        "SB-TLS-04": "no TLS deny",
        "SB-PUB-01/policy": "Principal *",
        "SB-PUB-09": "NAT gateway",
        "SB-PUB-10": "0.0.0.0/0 ingress",
        "SB-IAM-02": "Action:* Resource:*",
        "SB-PUB-06": "function URL",
        "SB-LOG-04": "log group without retention",
        "SB-TAG-01": "missing tags",
    }
    for check_id, why in expected_failures.items():
        assert any(r.status == FAIL for r in results[check_id]), f"{check_id} should fail: {why}"


def test_static_prod_plan_has_monitoring_and_no_failures(static_plan_prod):
    results = validate_plan(static_plan_prod)
    assert not [r.to_dict() for r in results if r.status == FAIL]
    types = {rc["type"] for rc in static_plan_prod["resource_changes"]}
    assert {"aws_cloudwatch_metric_alarm", "aws_sns_topic", "aws_cloudwatch_dashboard"} <= types


# SB-LOG-02 must distinguish "no logging" from "log bucket known after apply" (platform plan, review 2026-10-05).
def _dist_plan(after_logging, unknown_logging):
    return {"resource_changes": [{
        "address": "module.ui_edge.aws_cloudfront_distribution.this", "type": "aws_cloudfront_distribution",
        "change": {"actions": ["create"], "after": {"logging_config": after_logging},
                   "after_unknown": {"logging_config": unknown_logging} if unknown_logging is not None else {}}}]}


def _log_check(plan):
    return next(r for r in validate_plan(plan) if r.check_id == "SB-LOG-02")


def test_cloudfront_logging_with_known_bucket_passes():
    plan = _dist_plan([{"bucket": "logs.s3.amazonaws.com", "prefix": "x/", "include_cookies": False}], None)
    assert _log_check(plan).status == PASS


def test_cloudfront_logging_bucket_unknown_at_plan_is_deferred_not_failed():
    plan = _dist_plan([{"prefix": "platform/ui/", "include_cookies": False}], [{"bucket": True}])
    assert _log_check(plan).status == DEFERRED


def test_cloudfront_without_logging_still_fails():
    assert _log_check(_dist_plan([], [])).status == FAIL
    assert _log_check(_dist_plan([{"prefix": "x/"}], [{}])).status == FAIL
