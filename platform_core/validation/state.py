"""STATE-layer validator for the Static blueprint: reads real AWS resources via boto3 (D-91)."""

from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from .plan import REQUIRED_TAGS, SECURE_VIEWER, _policy_public, _policy_tls
from .result import FAIL, NOT_APPLICABLE, PASS, CheckResult


def _r(check_id, dim, ok, detail, *sb, status=None):
    return CheckResult(check_id, "STATE", dim, status or (PASS if ok else FAIL), detail, tuple(sb))


def _err(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "ClientError")


def wait_distribution_deployed(cloudfront, distribution_id: str, timeout_s: int = 900, interval_s: int = 15) -> str:
    deadline = time.monotonic() + timeout_s
    status = "Unknown"
    while time.monotonic() < deadline:
        status = cloudfront.get_distribution(Id=distribution_id)["Distribution"]["Status"]
        if status == "Deployed":
            return status
        time.sleep(interval_s)
    return status


def check_prod_monitoring(*, cloudwatch, sns, ctx: dict) -> CheckResult:
    """PR-ALL-06: PROD has alarms wired to an SNS topic with at least one subscription."""
    if ctx["tier"] != "PROD":
        return _r("PR-ALL-06", "Observability", True, "DEV: log only (D-62)", status=NOT_APPLICABLE)
    names = ctx.get("alarm_names") or []
    if not names:
        return _r("PR-ALL-06", "Observability", False, "no alarms configured")
    alarms = cloudwatch.describe_alarms(AlarmNames=names)["MetricAlarms"]
    wired = [a for a in alarms if ctx.get("alert_topic_arn") in (a.get("AlarmActions") or [])]
    subs = sns.list_subscriptions_by_topic(TopicArn=ctx["alert_topic_arn"])["Subscriptions"] if ctx.get("alert_topic_arn") else []
    ok = len(wired) == len(names) and len(subs) > 0
    return _r("PR-ALL-06", "Observability", ok, f"alarms={len(alarms)}/{len(names)} wired={len(wired)} subscriptions={len(subs)}")


def validate_static_state(*, s3, cloudfront, cloudtrail, ctx: dict) -> list[CheckResult]:
    """ctx: bucket, distribution_id, distribution_arn, tier, ip_allowlist, log_bucket, trail_name"""
    bucket, dist_id = ctx["bucket"], ctx["distribution_id"]
    out: list[CheckResult] = []

    # SB-PUB-01: BPA + ACLs disabled
    try:
        bpa = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        ok = all(bpa.get(k) for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))
        out.append(_r("SB-PUB-01", "Security", ok, f"BPA={bpa}", "SB-PUB-01"))
    except ClientError as e:
        out.append(_r("SB-PUB-01", "Security", False, f"BPA missing ({_err(e)})", "SB-PUB-01"))
    try:
        rules = s3.get_bucket_ownership_controls(Bucket=bucket)["OwnershipControls"]["Rules"]
        ok = any(r.get("ObjectOwnership") == "BucketOwnerEnforced" for r in rules)
        out.append(_r("SB-PUB-01/acl", "Security", ok, f"ownership={rules}", "SB-PUB-01"))
    except ClientError as e:
        out.append(_r("SB-PUB-01/acl", "Security", False, f"ownership controls missing ({_err(e)})", "SB-PUB-01"))

    # SB-ENC-01
    try:
        rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
        algos = [r["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] for r in rules]
        out.append(_r("SB-ENC-01", "Security", bool(algos), f"SSE={algos}", "SB-ENC-01"))
    except ClientError as e:
        out.append(_r("SB-ENC-01", "Security", False, f"no default encryption ({_err(e)})", "SB-ENC-01"))

    # SB-TLS-04 + SB-PUB-01 policy scope
    try:
        st = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])["Statement"]
        st = st if isinstance(st, list) else [st]
        out.append(_policy_tls(bucket, st, "STATE"))
        pub = _policy_public(bucket, st, "STATE")
        scoped = [s for s in st if s.get("Effect") == "Allow"
                  and (s.get("Condition") or {}).get("StringEquals", {}).get("AWS:SourceArn") not in (None, ctx["distribution_arn"])]
        if scoped:
            pub = CheckResult(pub.check_id, "STATE", "Security", FAIL,
                              f"{bucket}: OAC grant scoped to another distribution", ("SB-PUB-01",))
        out.append(pub)
    except ClientError as e:
        out.append(_r("SB-TLS-04", "Security", False, f"no bucket policy ({_err(e)})", "SB-TLS-04"))

    # CloudFront configuration
    cfg = cloudfront.get_distribution_config(Id=dist_id)["DistributionConfig"]
    behaviors = [cfg["DefaultCacheBehavior"]] + (cfg.get("CacheBehaviors", {}).get("Items") or [])
    viewer = [b["ViewerProtocolPolicy"] for b in behaviors]
    out.append(_r("SB-TLS-01", "Security", all(v in SECURE_VIEWER for v in viewer), f"viewer={viewer}", "SB-TLS-01"))
    out.append(_r("SB-TLS-05", "Security", all(b.get("ResponseHeadersPolicyId") for b in behaviors),
                  "response headers policy on all behaviors", "SB-TLS-05"))
    origins = cfg["Origins"]["Items"]
    oac_ok = all(o.get("OriginAccessControlId") for o in origins if ".s3." in o["DomainName"])
    out.append(_r("SB-PUB-01/oac", "Security", oac_ok, f"origins={[o['DomainName'] for o in origins]}", "SB-PUB-01"))
    log = cfg.get("Logging", {})
    out.append(_r("SB-LOG-02", "Security", bool(log.get("Enabled") and log.get("Bucket")),
                  f"logging={log.get('Bucket')}/{log.get('Prefix')}", "SB-LOG-02"))
    out.append(_r("PR-ALL-02/enabled", "Reachability", bool(cfg.get("Enabled")), f"enabled={cfg.get('Enabled')}"))
    if ctx.get("ip_allowlist"):
        out.append(_r("SB-PUB-08", "Security", bool(cfg.get("WebACLId")), f"web_acl={cfg.get('WebACLId')}", "SB-PUB-08"))
    else:
        out.append(_r("SB-PUB-08", "Security", True, "public exposure", "SB-PUB-08", status=NOT_APPLICABLE))

    # SB-TAG-01 / PR-ALL-09, PR-ALL-08
    try:
        btags = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
    except ClientError:
        btags = {}
    dtags = {t["Key"]: t["Value"] for t in
             cloudfront.list_tags_for_resource(Resource=ctx["distribution_arn"])["Tags"].get("Items", [])}
    lacking = {name: [k for k in REQUIRED_TAGS if not tags.get(k)] for name, tags in (("bucket", btags), ("distribution", dtags))}
    lacking = {k: v for k, v in lacking.items() if v}
    out.append(_r("PR-ALL-09", "Cost", not lacking, f"missing={lacking}" if lacking else "required tags present", "SB-TAG-01"))
    if ctx["tier"] == "DEV":
        out.append(_r("PR-ALL-08", "Operability", bool(btags.get("expires-at")), f"expires-at={btags.get('expires-at')}"))
    else:
        out.append(_r("PR-ALL-08", "Operability", True, "PROD: no TTL", status=NOT_APPLICABLE))

    # SB-LOG-01 (account) and PR-ALL-05 (log retention on the shared log bucket)
    try:
        trail = cloudtrail.get_trail_status(Name=ctx["trail_name"])
        out.append(_r("SB-LOG-01", "Security", bool(trail.get("IsLogging")), f"trail logging={trail.get('IsLogging')}", "SB-LOG-01"))
    except ClientError as e:
        out.append(_r("SB-LOG-01", "Security", False, f"trail missing ({_err(e)})", "SB-LOG-01"))
    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=ctx["log_bucket"])["Rules"]
        ok = any(r.get("Status") == "Enabled" and r.get("Expiration", {}).get("Days") for r in rules)
        out.append(_r("PR-ALL-05", "Observability", ok, "log bucket expiration rule", "SB-LOG-02"))
    except ClientError as e:
        out.append(_r("PR-ALL-05", "Observability", False, f"no lifecycle on log bucket ({_err(e)})", "SB-LOG-02"))
    return out


def enable_distribution(cloudfront, distribution_id: str) -> bool:
    """Re-enable after a redeploy/rollback of an endpoint that a security failure had disabled.
    The new revision is validated again before the environment becomes READY."""
    resp = cloudfront.get_distribution_config(Id=distribution_id)
    cfg = resp["DistributionConfig"]
    if cfg["Enabled"]:
        return False
    cfg["Enabled"] = True
    cloudfront.update_distribution(Id=distribution_id, IfMatch=resp["ETag"], DistributionConfig=cfg)
    return True


def disable_distribution(cloudfront, distribution_id: str) -> None:
    """D-65: a security failure takes the endpoint offline immediately."""
    resp = cloudfront.get_distribution_config(Id=distribution_id)
    cfg = resp["DistributionConfig"]
    if not cfg["Enabled"]:
        return
    cfg["Enabled"] = False
    cloudfront.update_distribution(Id=distribution_id, IfMatch=resp["ETag"], DistributionConfig=cfg)
