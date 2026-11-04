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
    """Every resource of the configuration after apply, with its planned values. Unchanged (no-op) resources are part
    of the final configuration too: a later plan (Serverless after the ECR-only apply, a retry) must still check them."""
    out = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions in (["delete"], ["read"]):
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


# ---------------------------------------------------------------- Serverless (D-165)

# Resource types each blueprint may create (D-70 "허용 리소스 상한"). Anything else blocks apply.
ALLOWED_TYPES = {
    "static": {
        "aws_s3_bucket", "aws_s3_bucket_public_access_block", "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_server_side_encryption_configuration", "aws_s3_bucket_policy",
        "aws_cloudfront_distribution", "aws_cloudfront_origin_access_control", "aws_wafv2_ip_set", "aws_wafv2_web_acl",
        "aws_sns_topic", "aws_sns_topic_subscription", "aws_cloudwatch_metric_alarm", "aws_cloudwatch_dashboard",
    },
    "serverless": {
        "aws_ecr_repository", "aws_ecr_lifecycle_policy", "aws_ecr_repository_policy",
        "aws_iam_role", "aws_iam_role_policy", "aws_lambda_function", "aws_lambda_permission",
        "aws_cloudwatch_log_group", "aws_apigatewayv2_api", "aws_apigatewayv2_integration", "aws_apigatewayv2_route",
        "aws_apigatewayv2_stage", "aws_sns_topic", "aws_sns_topic_subscription", "aws_cloudwatch_metric_alarm",
    },
    "container": {
        "aws_ecr_repository", "aws_ecr_lifecycle_policy", "aws_cloudwatch_log_group", "aws_iam_role", "aws_iam_role_policy",
        "aws_security_group", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
        "aws_lb_target_group", "aws_lb_listener_rule", "aws_ecs_task_definition", "aws_ecs_service",
        "aws_cloudfront_distribution", "aws_wafv2_ip_set", "aws_wafv2_web_acl",
        "aws_sns_topic", "aws_sns_topic_subscription", "aws_cloudwatch_metric_alarm",
    },
    "container-shared": {
        "aws_vpc", "aws_internet_gateway", "aws_subnet", "aws_route_table", "aws_route_table_association", "aws_vpc_endpoint",
        "aws_security_group", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
        "aws_lb", "aws_lb_listener", "aws_cloudfront_vpc_origin", "aws_ecs_cluster", "aws_ecs_cluster_capacity_providers",
    },
}


def check_allowed_types(res, blueprint: str) -> CheckResult:
    allowed = ALLOWED_TYPES[blueprint]
    bad = sorted({r["type"] for r in res if r["type"] not in allowed})
    return _r("PLAN-ALLOWLIST", "Cost", FAIL if bad else PASS,
              f"resource types outside the {blueprint} blueprint: {bad}" if bad else f"only {blueprint} blueprint resource types")


def check_role_boundary(res) -> CheckResult:
    roles = _by_type(res, "aws_iam_role")
    if not roles:
        return _r("SB-IAM-04", "Security", NOT_APPLICABLE, "no IAM roles", "SB-IAM-04")
    bad = [r["address"] for r in roles if not r["after"].get("permissions_boundary") and not r["unknown"].get("permissions_boundary")]
    return _r("SB-IAM-04", "Security", FAIL if bad else PASS,
              f"roles without permissions boundary: {bad}" if bad else "permissions boundary on every role", "SB-IAM-04")


def check_lambda_env_secrets(res) -> CheckResult:
    from .artifact import SECRET_PATTERNS
    from .image import SECRET_KEY
    fns = _by_type(res, "aws_lambda_function")
    if not fns:
        return _r("SB-SEC-02", "Security", NOT_APPLICABLE, "no functions", "SB-SEC-02")
    hits = []
    for f in fns:
        variables = ((f["after"].get("environment") or [{}])[0] or {}).get("variables") or {}
        for k, v in variables.items():
            if SECRET_KEY.search(k) or any(rx.search(str(v).encode()) for rx in SECRET_PATTERNS.values()):
                hits.append(f"{f['address']}:{k}")
    return _r("SB-SEC-02", "Security", FAIL if hits else PASS,
              f"plaintext secret-like environment variables {hits}" if hits else "no plaintext secrets in environment", "SB-SEC-02")


def check_api_access_log(res) -> CheckResult:
    stages = _by_type(res, "aws_apigatewayv2_stage")
    if not stages:
        return _r("SB-LOG-03", "Security", NOT_APPLICABLE, "no API stages", "SB-LOG-03")
    bad = [s["address"] for s in stages if not (s["after"].get("access_log_settings") or s["unknown"].get("access_log_settings"))]
    return _r("SB-LOG-03", "Security", FAIL if bad else PASS,
              f"stages without access log: {bad}" if bad else "API access log configured", "SB-LOG-03")


def check_ecr(res) -> list[CheckResult]:
    repos = _by_type(res, "aws_ecr_repository")
    if not repos:
        return [_r("SB-PUB-07", "Security", NOT_APPLICABLE, "no ECR repositories", "SB-PUB-07")]
    no_scan = [r["address"] for r in repos if not ((r["after"].get("image_scanning_configuration") or [{}])[0] or {}).get("scan_on_push")]
    mutable = [r["address"] for r in repos if r["after"].get("image_tag_mutability") != "IMMUTABLE"]
    out = [_r("SB-PUB-07", "Security", FAIL if no_scan else PASS,
              f"scan on push disabled: {no_scan}" if no_scan else "private repository, scan on push", "SB-PUB-07")]
    out.append(_r("SB-PUB-07/immutable", "Security", FAIL if mutable else PASS,
                  f"mutable tags: {mutable}" if mutable else "image tags are immutable", "SB-PUB-07"))
    return out


def check_lambda_invoke_scope(res) -> CheckResult:
    perms = _by_type(res, "aws_lambda_permission")
    if not perms:
        return _r("SB-PUB-06/permission", "Security", NOT_APPLICABLE, "no invoke permissions", "SB-PUB-06")
    bad = [p["address"] for p in perms if p["after"].get("principal") != "apigateway.amazonaws.com"
           or not (p["after"].get("source_arn") or p["unknown"].get("source_arn"))]
    return _r("SB-PUB-06/permission", "Security", FAIL if bad else PASS,
              f"invoke permission not limited to this API: {bad}" if bad else "only this API can invoke the function", "SB-PUB-06")


# ---------------------------------------------------------------- Container (D-33, D-44, D-170)

def _ingress_rules(res) -> list[tuple[str, dict]]:
    """(address, rule) for every ingress rule, inline or standalone."""
    out = [(sg["address"], rule) for sg in _by_type(res, "aws_security_group") for rule in sg["after"].get("ingress") or []]
    return out + [(r["address"], r["after"]) for r in _by_type(res, "aws_vpc_security_group_ingress_rule")]


def check_alb_internal(res) -> CheckResult:
    lbs = _by_type(res, "aws_lb")
    if not lbs:
        return _r("SB-PUB-02", "Security", NOT_APPLICABLE, "no load balancers in this plan", "SB-PUB-02")
    bad = [lb["address"] for lb in lbs if lb["after"].get("internal") is not True]
    return _r("SB-PUB-02", "Security", FAIL if bad else PASS,
              f"internet-facing load balancers: {bad}" if bad else "internal ALB only", "SB-PUB-02")


def check_alb_ingress(res) -> CheckResult:
    """The shared ALB accepts traffic only from the CloudFront origin-facing prefix list (VPC origin)."""
    if not _by_type(res, "aws_lb"):
        return _r("SB-PUB-04", "Security", NOT_APPLICABLE, "no load balancers in this plan", "SB-PUB-04")
    bad = [addr for addr, rule in _ingress_rules(res) if not (rule.get("prefix_list_id") or rule.get("prefix_list_ids"))]
    return _r("SB-PUB-04", "Security", FAIL if bad else PASS,
              f"ALB-side ingress not limited to CloudFront: {bad}" if bad else "ingress only from the CloudFront prefix list",
              "SB-PUB-04")


def check_task_ingress(res) -> CheckResult:
    """Task security groups accept only the ALB's security group, on the app port (public IP, no internet inbound)."""
    if not _by_type(res, "aws_ecs_service"):
        return _r("SB-PUB-03", "Security", NOT_APPLICABLE, "no ECS services in this plan", "SB-PUB-03")
    bad = []
    for addr, rule in _ingress_rules(res):
        source = rule.get("referenced_security_group_id") or rule.get("security_groups")
        if not source or rule.get("cidr_ipv4") or rule.get("cidr_blocks") or rule.get("from_port") != rule.get("to_port"):
            bad.append(addr)
    return _r("SB-PUB-03", "Security", FAIL if bad else PASS,
              f"task ingress not limited to the ALB on one port: {bad}" if bad else "task ingress only from the ALB on the app port",
              "SB-PUB-03")


def check_container_env_secrets(res) -> CheckResult:
    from .artifact import SECRET_PATTERNS
    from .image import SECRET_KEY
    tds = _by_type(res, "aws_ecs_task_definition")
    if not tds:
        return _r("SB-SEC-02", "Security", NOT_APPLICABLE, "no task definitions", "SB-SEC-02")
    hits, deferred = [], []
    for td in tds:
        raw = td["after"].get("container_definitions")
        if raw is None:
            deferred.append(td["address"])
            continue
        for c in json.loads(raw) if isinstance(raw, str) else raw:
            if c.get("secrets"):
                hits.append(f"{td['address']}:secrets")  # the MVP accepts no secrets (D-167)
            for e in c.get("environment") or []:
                k, v = e.get("name", ""), str(e.get("value", ""))
                if SECRET_KEY.search(k) or any(rx.search(v.encode()) for rx in SECRET_PATTERNS.values()):
                    hits.append(f"{td['address']}:{k}")
    if hits:
        return _r("SB-SEC-02", "Security", FAIL, f"plaintext secret-like container settings {hits}", "SB-SEC-02")
    if deferred:
        return _r("SB-SEC-02", "Security", DEFERRED, f"container definitions known after apply: {deferred}", "SB-SEC-02")
    return _r("SB-SEC-02", "Security", PASS, "no plaintext secrets in container settings", "SB-SEC-02")


# Resources with an hourly charge and the reason code that must justify them (recommendation-engine §9.6, D-44).
FIXED_COST = {
    "aws_lb": "FIX_ALB_FOR_CONTAINER",
    "aws_ecs_service": "FIX_ALWAYS_ON_COMPUTE",
    "aws_lambda_provisioned_concurrency_config": "FIX_PC_FOR_LATENCY",
    "aws_nat_gateway": None,  # never (D-44)
    "aws_eip": None,          # an idle public IPv4 address is billed; tasks get theirs from Fargate
}


def check_fixed_cost(res, justified: set[str] | None) -> CheckResult:
    """Every fixed-cost resource in the plan is backed by the recommendation's FIX_* reason; interface endpoints
    and NAT are never created by these blueprints."""
    found, bad = [], []
    for r in res:
        if r["type"] == "aws_vpc_endpoint" and r["after"].get("vpc_endpoint_type") != "Gateway":
            bad.append(f"{r['address']}(interface endpoint)")
            continue
        if r["type"] not in FIXED_COST:
            continue
        reason = FIXED_COST[r["type"]]
        found.append(f"{r['address']}:{reason}")
        if reason is None or (justified is not None and reason not in justified):
            bad.append(f"{r['address']}:{reason or 'never allowed'}")
    if bad:
        return _r("PLAN-FIXED-COST", "Cost", FAIL, f"fixed-cost resources without justification: {bad}")
    if not found:
        return _r("PLAN-FIXED-COST", "Cost", PASS, "no fixed-cost resources")
    return _r("PLAN-FIXED-COST", "Cost", PASS, f"fixed cost justified by the recommendation: {found}")


def validate_plan(plan: dict, *, ip_allowlist: list[str] | None = None, blueprint: str | None = None,
                  fixed_cost_reasons: set[str] | None = None) -> list[CheckResult]:
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
    if _by_type(res, "aws_lambda_function"):
        out += [check_role_boundary(res), check_lambda_env_secrets(res), check_api_access_log(res), check_lambda_invoke_scope(res)]
        out += check_ecr(res)
    if blueprint in ("container", "container-shared"):
        out += [check_role_boundary(res), check_container_env_secrets(res), check_task_ingress(res),
                check_alb_internal(res), check_alb_ingress(res), check_fixed_cost(res, fixed_cost_reasons)]
        out += check_ecr(res)
    if blueprint:
        out.append(check_allowed_types(res, blueprint))
    return out
