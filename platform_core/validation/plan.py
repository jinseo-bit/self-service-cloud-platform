"""PLAN-layer validator: inspects `terraform show -json <plan>` before apply (D-91).

Any FAIL here blocks apply. Values that are unknown until apply are reported as
DEFERRED and must be covered by a STATE/HTTP check with the same baseline id.
"""

from __future__ import annotations

import json
from collections import defaultdict

from .result import DEFERRED, FAIL, NOT_APPLICABLE, PASS, CheckResult

REQUIRED_TAGS = ("project", "managed-by", "env-id", "owner", "tier", "blueprint")
SECURE_VIEWER = ("redirect-to-https", "https-only")


def _resources(plan: dict) -> list[dict]:
    """Resources being created or updated, with their planned values."""
    out = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions in (["delete"], ["no-op"], ["read"]):
            continue
        out.append({
            "address": rc["address"],
            "type": rc["type"],
            "module": rc.get("module_address", ""),
            "after": rc["change"].get("after") or {},
            "unknown": rc["change"].get("after_unknown") or {},
        })
    return out


def _r(check_id, dim, status, detail, *sb):
    return CheckResult(check_id, "PLAN", dim, status, detail, tuple(sb))


def _by_type(res, t):
    return [r for r in res if r["type"] == t]


def check_s3_public_access(res) -> CheckResult:
    buckets = defaultdict(int)
    blocks = defaultdict(int)
    for b in _by_type(res, "aws_s3_bucket"):
        buckets[b["module"]] += 1
    bad = []
    for p in _by_type(res, "aws_s3_bucket_public_access_block"):
        a = p["after"]
        flags = [a.get(k) for k in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets")]
        if all(f is True for f in flags):
            blocks[p["module"]] += 1
        else:
            bad.append(p["address"])
    missing = [m or "<root>" for m, n in buckets.items() if blocks[m] < n]
    if not buckets:
        return _r("SB-PUB-01", "Security", NOT_APPLICABLE, "no S3 buckets", "SB-PUB-01")
    if bad or missing:
        return _r("SB-PUB-01", "Security", FAIL, f"BPA not fully enabled: {bad} missing for modules {missing}", "SB-PUB-01")
    return _r("SB-PUB-01", "Security", PASS, f"{sum(buckets.values())} bucket(s) with full BPA", "SB-PUB-01")


def check_s3_encryption(res) -> CheckResult:
    buckets = defaultdict(int)
    enc = defaultdict(int)
    for b in _by_type(res, "aws_s3_bucket"):
        buckets[b["module"]] += 1
    for e in _by_type(res, "aws_s3_bucket_server_side_encryption_configuration"):
        rules = e["after"].get("rule") or []
        algos = [d.get("sse_algorithm") for r in rules for d in (r.get("apply_server_side_encryption_by_default") or [])]
        if any(a in ("AES256", "aws:kms", "aws:kms:dsse") for a in algos):
            enc[e["module"]] += 1
    if not buckets:
        return _r("SB-ENC-01", "Security", NOT_APPLICABLE, "no S3 buckets", "SB-ENC-01")
    missing = [m or "<root>" for m, n in buckets.items() if enc[m] < n]
    if missing:
        return _r("SB-ENC-01", "Security", FAIL, f"buckets without SSE in modules {missing}", "SB-ENC-01")
    return _r("SB-ENC-01", "Security", PASS, "all buckets encrypted", "SB-ENC-01")


def _behaviors(dist_after: dict) -> list[dict]:
    return list(dist_after.get("default_cache_behavior") or []) + list(dist_after.get("ordered_cache_behavior") or [])


def check_cloudfront_tls(res) -> CheckResult:
    dists = _by_type(res, "aws_cloudfront_distribution")
    if not dists:
        return _r("SB-TLS-01", "Security", NOT_APPLICABLE, "no distributions", "SB-TLS-01")
    bad = [(d["address"], b.get("viewer_protocol_policy")) for d in dists for b in _behaviors(d["after"])
           if b.get("viewer_protocol_policy") not in SECURE_VIEWER]
    if bad:
        return _r("SB-TLS-01", "Security", FAIL, f"insecure viewer protocol: {bad}", "SB-TLS-01")
    return _r("SB-TLS-01", "Security", PASS, "all behaviors HTTPS-only/redirect", "SB-TLS-01")


def check_cloudfront_headers(res) -> CheckResult:
    dists = _by_type(res, "aws_cloudfront_distribution")
    if not dists:
        return _r("SB-TLS-05", "Security", NOT_APPLICABLE, "no distributions", "SB-TLS-05")
    bad = [d["address"] for d in dists for b in _behaviors(d["after"]) if not b.get("response_headers_policy_id")]
    if bad:
        return _r("SB-TLS-05", "Security", FAIL, f"behaviors without response headers policy: {bad}", "SB-TLS-05")
    return _r("SB-TLS-05", "Security", PASS, "security headers policy on all behaviors", "SB-TLS-05")


def check_cloudfront_logging(res) -> CheckResult:
    dists = _by_type(res, "aws_cloudfront_distribution")
    if not dists:
        return _r("SB-LOG-02", "Security", NOT_APPLICABLE, "no distributions", "SB-LOG-02")
    bad, deferred = [], []
    for d in dists:
        if (d["after"].get("logging_config") or [{}])[0].get("bucket"):
            continue
        # A logging block whose bucket is only known after apply (e.g. a bucket created in the same plan) is not "disabled".
        unknown = d["unknown"].get("logging_config")
        if isinstance(unknown, list) and unknown and isinstance(unknown[0], dict) and unknown[0].get("bucket") is True:
            deferred.append(d["address"])
        else:
            bad.append(d["address"])
    if bad:
        return _r("SB-LOG-02", "Security", FAIL, f"access logging disabled: {bad}", "SB-LOG-02")
    if deferred:
        return _r("SB-LOG-02", "Security", DEFERRED, f"log bucket known after apply: {deferred}", "SB-LOG-02")
    return _r("SB-LOG-02", "Security", PASS, "access logging configured", "SB-LOG-02")


def check_waf_for_allowlist(res, ip_allowlist: list[str]) -> CheckResult:
    if not ip_allowlist:
        return _r("SB-PUB-08", "Security", NOT_APPLICABLE, "public exposure", "SB-PUB-08")
    dists = _by_type(res, "aws_cloudfront_distribution")
    bad = [d["address"] for d in dists if not (d["after"].get("web_acl_id") or d["unknown"].get("web_acl_id"))]
    acls = _by_type(res, "aws_wafv2_web_acl")
    blocks_by_default = all((a["after"].get("default_action") or [{}])[0].get("block") for a in acls)
    if bad or not acls or not blocks_by_default:
        return _r("SB-PUB-08", "Security", FAIL, f"allowlist requested but WAF missing/permissive: {bad}", "SB-PUB-08")
    return _r("SB-PUB-08", "Security", PASS, "WAF default-block with IP allow rule", "SB-PUB-08")


def _statements(policy) -> list[dict] | None:
    if policy is None:
        return None
    doc = json.loads(policy) if isinstance(policy, str) else policy
    st = doc.get("Statement", [])
    return st if isinstance(st, list) else [st]


def check_bucket_policy(res) -> list[CheckResult]:
    pols = _by_type(res, "aws_s3_bucket_policy")
    if not pols:
        if _by_type(res, "aws_s3_bucket"):
            return [_r("SB-TLS-04", "Security", FAIL, "bucket without policy (no TLS deny)", "SB-TLS-04")]
        return [_r("SB-TLS-04", "Security", NOT_APPLICABLE, "no S3 buckets", "SB-TLS-04")]
    results = []
    for p in pols:
        st = _statements(p["after"].get("policy"))
        if st is None:
            results.append(_r("SB-TLS-04", "Security", DEFERRED, f"{p['address']} policy known after apply", "SB-TLS-04"))
            results.append(_r("SB-PUB-01/policy", "Security", DEFERRED, f"{p['address']} policy known after apply", "SB-PUB-01"))
            continue
        results.append(_policy_tls(p["address"], st, "PLAN"))
        results.append(_policy_public(p["address"], st, "PLAN"))
    return results


def _policy_tls(addr, statements, layer) -> CheckResult:
    ok = any(s.get("Effect") == "Deny"
             and str((s.get("Condition") or {}).get("Bool", {}).get("aws:SecureTransport")).lower() == "false"
             for s in statements)
    return CheckResult("SB-TLS-04", layer, "Security", PASS if ok else FAIL,
                       f"{addr}: {'denies' if ok else 'does not deny'} non-TLS requests", ("SB-TLS-04",))


def _policy_public(addr, statements, layer) -> CheckResult:
    bad = []
    for s in statements:
        if s.get("Effect") != "Allow":
            continue
        pr = s.get("Principal")
        if pr == "*" or (isinstance(pr, dict) and pr.get("AWS") in ("*", ["*"])):
            bad.append(s.get("Sid", "?"))
        if isinstance(pr, dict) and pr.get("Service") == "cloudfront.amazonaws.com":
            cond = (s.get("Condition") or {}).get("StringEquals", {})
            if not (cond.get("AWS:SourceArn") or cond.get("aws:SourceArn")):
                bad.append(s.get("Sid", "?") + "(no SourceArn)")
    return CheckResult("SB-PUB-01/policy", layer, "Security", FAIL if bad else PASS,
                       f"{addr}: public/unscoped allow {bad}" if bad else f"{addr}: OAC-scoped only", ("SB-PUB-01",))


def _has_default_tags(plan: dict) -> bool:
    providers = (plan.get("configuration") or {}).get("provider_config") or {}
    return any("default_tags" in (p.get("expressions") or {}) for p in providers.values())


def check_tags(res, default_tags: bool) -> CheckResult:
    missing, deferred = [], []
    for r in res:
        if "tags_all" not in r["after"] and "tags_all" not in r["unknown"]:
            continue  # resource type not taggable
        tags = r["after"].get("tags_all")
        if tags is None:
            if default_tags:
                deferred.append(r["address"])
                continue
            # No provider default_tags: tags_all can only come from the resource's own tags.
            tags = r["after"].get("tags") or {}
        lack = [t for t in REQUIRED_TAGS if not tags.get(t)]
        if lack:
            missing.append(f"{r['address']}:{lack}")
    if missing:
        return _r("SB-TAG-01", "Cost", FAIL, f"missing tags {missing}", "SB-TAG-01")
    if deferred:
        return _r("SB-TAG-01", "Cost", DEFERRED, f"tags known after apply: {deferred}", "SB-TAG-01")
    return _r("SB-TAG-01", "Cost", PASS, "required tags on all taggable resources", "SB-TAG-01")


def check_no_nat(res) -> CheckResult:
    nat = [r["address"] for r in _by_type(res, "aws_nat_gateway")]
    return _r("SB-PUB-09", "Cost", FAIL if nat else PASS, f"NAT gateways: {nat}" if nat else "no NAT gateway", "SB-PUB-09")


def check_no_open_ingress(res) -> CheckResult:
    bad = []
    for sg in _by_type(res, "aws_security_group"):
        for rule in sg["after"].get("ingress") or []:
            if "0.0.0.0/0" in (rule.get("cidr_blocks") or []) or "::/0" in (rule.get("ipv6_cidr_blocks") or []):
                bad.append(sg["address"])
    for rule in _by_type(res, "aws_vpc_security_group_ingress_rule") + _by_type(res, "aws_security_group_rule"):
        a = rule["after"]
        if rule["type"] == "aws_security_group_rule" and a.get("type") != "ingress":
            continue
        cidrs = [a.get("cidr_ipv4"), a.get("cidr_ipv6")] + list(a.get("cidr_blocks") or []) + list(a.get("ipv6_cidr_blocks") or [])
        if "0.0.0.0/0" in cidrs or "::/0" in cidrs:
            bad.append(rule["address"])
    return _r("SB-PUB-10", "Security", FAIL if bad else PASS, f"open ingress: {bad}" if bad else "no 0.0.0.0/0 ingress", "SB-PUB-10")


def check_iam_wildcards(res) -> CheckResult:
    bad, deferred = [], []
    for r in res:
        if r["type"] not in ("aws_iam_policy", "aws_iam_role_policy"):
            continue
        st = _statements(r["after"].get("policy"))
        if st is None:
            deferred.append(r["address"])
            continue
        for s in st:
            acts = s.get("Action")
            rsrc = s.get("Resource")
            acts = acts if isinstance(acts, list) else [acts]
            rsrc = rsrc if isinstance(rsrc, list) else [rsrc]
            if s.get("Effect") == "Allow" and ("*" in acts) and ("*" in rsrc):
                bad.append(r["address"])
    if bad:
        return _r("SB-IAM-02", "Security", FAIL, f"Action:* on Resource:* in {bad}", "SB-IAM-02")
    if deferred:
        return _r("SB-IAM-02", "Security", DEFERRED, f"policies known after apply: {deferred}", "SB-IAM-02")
    return _r("SB-IAM-02", "Security", PASS, "no wildcard admin statements", "SB-IAM-02")


def check_no_function_url(res) -> CheckResult:
    urls = [r["address"] for r in _by_type(res, "aws_lambda_function_url")]
    return _r("SB-PUB-06", "Security", FAIL if urls else PASS, f"function URLs: {urls}" if urls else "no function URL", "SB-PUB-06")


def check_log_retention(res) -> CheckResult:
    groups = _by_type(res, "aws_cloudwatch_log_group")
    if not groups:
        return _r("SB-LOG-04", "Observability", NOT_APPLICABLE, "no log groups", "SB-LOG-04")
    bad = [g["address"] for g in groups if not g["after"].get("retention_in_days")]
    return _r("SB-LOG-04", "Observability", FAIL if bad else PASS,
              f"log groups without retention: {bad}" if bad else "retention set", "SB-LOG-04")


def validate_plan(plan: dict, *, ip_allowlist: list[str] | None = None) -> list[CheckResult]:
    res = _resources(plan)
    out = [
        check_s3_public_access(res),
        check_s3_encryption(res),
        check_cloudfront_tls(res),
        check_cloudfront_headers(res),
        check_cloudfront_logging(res),
        check_waf_for_allowlist(res, ip_allowlist or []),
        check_tags(res, _has_default_tags(plan)),
        check_no_nat(res),
        check_no_open_ingress(res),
        check_iam_wildcards(res),
        check_no_function_url(res),
        check_log_retention(res),
    ]
    out += check_bucket_policy(res)
    return out
