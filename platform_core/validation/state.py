"""STATE-layer validators: read real AWS resources via boto3 (D-91). Static, Serverless and Container blueprints."""

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


# ---------------------------------------------------------------- Serverless (D-165)

def validate_serverless_state(*, lambda_, iam, logs, apigw, ecr, cloudtrail, ctx: dict) -> list[CheckResult]:
    """ctx: function_name, role_name, api_id, log_group, repository_name, repository_url, env_boundary_arn,
    tier, log_retention_days, trail_name"""
    from .image import SECRET_KEY
    out: list[CheckResult] = []
    fn = lambda_.get_function(FunctionName=ctx["function_name"])
    cfg = fn["Configuration"]

    # PR-L-01: the function is deployable and is the image the platform built
    active = cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") in (None, "Successful")
    out.append(_r("PR-L-01", "Reachability", active and cfg.get("PackageType") == "Image",
                  f"state={cfg.get('State')}/{cfg.get('LastUpdateStatus')} package={cfg.get('PackageType')}"))
    image = (fn.get("Code") or {}).get("ImageUri", "")
    out.append(_r("PR-L-02", "Security", image.startswith(ctx["repository_url"] + "@sha256:"),
                  f"image={image}", "SB-PUB-07"))
    out.append(_r("PR-L-03", "Operability", 0 < cfg.get("Timeout", 0) < 30 and cfg.get("MemorySize", 0) >= 128,
                  f"timeout={cfg.get('Timeout')}s memory={cfg.get('MemorySize')}MB"))

    # SB-IAM-01 / SB-IAM-04 / SB-IAM-02: dedicated role, boundary attached, no wildcard grants
    role_arn = cfg.get("Role", "")
    role = iam.get_role(RoleName=ctx["role_name"])["Role"]
    out.append(_r("SB-IAM-01", "Security", role_arn == role["Arn"] and ctx["role_name"] == ctx["function_name"],
                  f"role={role_arn.rsplit('/', 1)[-1]}", "SB-IAM-01"))
    boundary = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
    out.append(_r("SB-IAM-04", "Security", boundary == ctx["env_boundary_arn"], f"boundary={boundary}", "SB-IAM-04"))
    wide = []
    for name in iam.list_role_policies(RoleName=ctx["role_name"])["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=ctx["role_name"], PolicyName=name)["PolicyDocument"]
        doc = json.loads(doc) if isinstance(doc, str) else doc
        st = doc["Statement"] if isinstance(doc["Statement"], list) else [doc["Statement"]]
        for s in st:
            acts = s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")]
            res = s.get("Resource") if isinstance(s.get("Resource"), list) else [s.get("Resource")]
            if s.get("Effect") == "Allow" and ("*" in acts or "*" in res):
                wide.append(name)
    out.append(_r("SB-IAM-02", "Security", not wide, f"wildcard statements in {wide}" if wide else "role policy scoped", "SB-IAM-02"))

    # SB-SEC-02 / SB-SEC-01 / SB-ENC-04 / SB-SEC-05: no plaintext secrets; Secrets Manager is not used in the MVP
    env_vars = (cfg.get("Environment") or {}).get("Variables") or {}
    secretish = sorted(k for k in env_vars if SECRET_KEY.search(k))
    out.append(_r("SB-SEC-02", "Security", not secretish, f"secret-like variables {secretish}" if secretish
                  else f"{len(env_vars)} plain variables", "SB-SEC-02"))
    for sid in ("SB-SEC-01", "SB-ENC-04", "SB-SEC-05"):
        out.append(_r(sid, "Security", True, "no secrets: the Serverless MVP does not accept secret values (D-167)", sid,
                      status=NOT_APPLICABLE))

    # SB-PUB-06: no Function URL, invoke permission only for this API
    urls = lambda_.list_function_url_configs(FunctionName=ctx["function_name"]).get("FunctionUrlConfigs", [])
    out.append(_r("SB-PUB-06", "Security", not urls, f"function URLs={len(urls)}", "SB-PUB-06"))
    try:
        stmts = json.loads(lambda_.get_policy(FunctionName=ctx["function_name"])["Policy"])["Statement"]
    except ClientError:
        stmts = []
    api_arn = f":{ctx['api_id']}/"
    scoped = all((s.get("Principal") or {}).get("Service") == "apigateway.amazonaws.com"
                 and api_arn in json.dumps(s.get("Condition") or {}) for s in stmts) and bool(stmts)
    out.append(_r("SB-PUB-06/permission", "Security", scoped, f"{len(stmts)} invoke statement(s)", "SB-PUB-06"))

    # SB-LOG-04 / SB-LOG-03: retention and API access log
    groups = logs.describe_log_groups(logGroupNamePrefix=ctx["log_group"])["logGroups"]
    days = next((g.get("retentionInDays") for g in groups if g["logGroupName"] == ctx["log_group"]), None)
    out.append(_r("SB-LOG-04", "Observability", days == ctx["log_retention_days"], f"retention={days} days", "SB-LOG-04"))
    stage = apigw.get_stage(ApiId=ctx["api_id"], StageName="$default")
    access = (stage.get("AccessLogSettings") or {}).get("DestinationArn")
    out.append(_r("SB-LOG-03", "Security", bool(access), f"access log={access}", "SB-LOG-03"))
    out.append(_r("PR-L-04", "Cost", bool((stage.get("DefaultRouteSettings") or {}).get("ThrottlingRateLimit")),
                  f"throttle={(stage.get('DefaultRouteSettings') or {}).get('ThrottlingRateLimit')} rps"))

    # SB-PUB-07 / SB-ENC-05: private, scanned, encrypted repository
    repo = ecr.describe_repositories(repositoryNames=[ctx["repository_name"]])["repositories"][0]
    out.append(_r("SB-PUB-07", "Security", repo.get("imageScanningConfiguration", {}).get("scanOnPush") is True
                  and repo.get("imageTagMutability") == "IMMUTABLE", "scan on push, immutable tags", "SB-PUB-07"))
    enc = (repo.get("encryptionConfiguration") or {}).get("encryptionType")
    out.append(_r("SB-ENC-05", "Security", enc in ("AES256", "KMS", "KMS_DSSE"),
                  f"ECR encryption={enc}, CloudWatch Logs encrypted by default", "SB-ENC-05"))

    # SB-TAG-01 / PR-ALL-09, PR-ALL-08, SB-LOG-01
    tags = lambda_.list_tags(Resource=cfg["FunctionArn"]).get("Tags", {})
    lacking = [k for k in REQUIRED_TAGS if not tags.get(k)]
    out.append(_r("PR-ALL-09", "Cost", not lacking, f"missing={lacking}" if lacking else "required tags present", "SB-TAG-01"))
    if ctx["tier"] == "DEV":
        out.append(_r("PR-ALL-08", "Operability", bool(tags.get("expires-at")), f"expires-at={tags.get('expires-at')}"))
    else:
        out.append(_r("PR-ALL-08", "Operability", True, "PROD: no TTL", status=NOT_APPLICABLE))
    try:
        trail = cloudtrail.get_trail_status(Name=ctx["trail_name"])
        out.append(_r("SB-LOG-01", "Security", bool(trail.get("IsLogging")), f"trail logging={trail.get('IsLogging')}", "SB-LOG-01"))
    except ClientError as e:
        out.append(_r("SB-LOG-01", "Security", False, f"trail missing ({_err(e)})", "SB-LOG-01"))
    return out


def disable_function(lambda_, function_name: str) -> None:
    """D-65 for Serverless: reserved concurrency 0 takes the endpoint offline (requests are throttled)."""
    lambda_.put_function_concurrency(FunctionName=function_name, ReservedConcurrentExecutions=0)


# ---------------------------------------------------------------- Container (D-33, D-44, D-170)

def task_public_ips(ecs, ec2, cluster: str, service: str) -> list[str]:
    """Public IPs of the service's running tasks (D-44), for the HTTP-layer direct-access probe (PR-C-02)."""
    arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING").get("taskArns") or []
    if not arns:
        return []
    enis = [d["value"] for t in ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"] for a in t.get("attachments") or []
            for d in a.get("details") or [] if d.get("name") == "networkInterfaceId"]
    if not enis:
        return []
    nis = ec2.describe_network_interfaces(NetworkInterfaceIds=enis)["NetworkInterfaces"]
    return [n["Association"]["PublicIp"] for n in nis if (n.get("Association") or {}).get("PublicIp")]


def _wide_statements(iam, role: str) -> list[str]:
    """Allow statements with Action * or Resource * (ecr:GetAuthorizationToken is not resource-scoped by AWS)."""
    wide = []
    for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
        doc = json.loads(doc) if isinstance(doc, str) else doc
        for s in doc["Statement"] if isinstance(doc["Statement"], list) else [doc["Statement"]]:
            acts = s.get("Action") if isinstance(s.get("Action"), list) else [s.get("Action")]
            res = s.get("Resource") if isinstance(s.get("Resource"), list) else [s.get("Resource")]
            if s.get("Effect") == "Allow" and ("*" in acts or ("*" in res and set(acts) != {"ecr:GetAuthorizationToken"})):
                wide.append(f"{role}/{name}")
    return wide


def validate_container_state(*, ecs, elbv2, ec2, iam, logs, ecr, cloudfront, cloudtrail, ctx: dict) -> list[CheckResult]:
    """ctx: cluster_name, service_name, target_group_arn, task_security_group_id, alb_security_group_id,
    exec_role_name, task_role_name, log_group, repository_name, repository_url, distribution_id, env_boundary_arn,
    tier, multi_az, min_tasks, app_port, log_retention_days, trail_name, ip_allowlist"""
    from .image import SECRET_KEY
    out: list[CheckResult] = []
    cluster, service = ctx["cluster_name"], ctx["service_name"]

    # ECS service and task definition (PR-C-04, PR-C-05, PR-C-06)
    svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    running, desired = svc.get("runningCount", 0), svc.get("desiredCount", 0)
    arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING").get("taskArns") or []
    azs = sorted({t.get("availabilityZone") or "?" for t in ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"]}) if arns else []
    spread = ctx.get("multi_az", "SINGLE") == "SINGLE" or len(azs) >= 2
    out.append(_r("PR-C-04", "Resilience", svc.get("status") == "ACTIVE" and running >= ctx["min_tasks"] and running == desired
                  and spread, f"running={running}/{desired} (min {ctx['min_tasks']}), AZs={azs}"))
    cb = (svc.get("deploymentConfiguration") or {}).get("deploymentCircuitBreaker") or {}
    out.append(_r("PR-C-05", "Operability", cb.get("enable") is True and cb.get("rollback") is True,
                  f"circuit breaker enable={cb.get('enable')} rollback={cb.get('rollback')}"))
    out.append(_r("PR-C-06", "Cost", desired == ctx["min_tasks"],
                  f"desired={desired}, recommended min={ctx['min_tasks']} (autoscaling is not provisioned in the MVP)"))
    td = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
    containers = td.get("containerDefinitions") or []

    # SB-PUB-07 / SB-ENC-05: the task runs this environment's scanned, immutable, digest-pinned copy
    repo = ecr.describe_repositories(repositoryNames=[ctx["repository_name"]])["repositories"][0]
    pinned = bool(containers) and all(c.get("image", "").startswith(ctx["repository_url"] + "@sha256:") for c in containers)
    scanned = repo.get("imageScanningConfiguration", {}).get("scanOnPush") is True and repo.get("imageTagMutability") == "IMMUTABLE"
    out.append(_r("SB-PUB-07", "Security", scanned and pinned, f"scan on push + immutable={scanned}, task image pinned={pinned}",
                  "SB-PUB-07"))
    enc = (repo.get("encryptionConfiguration") or {}).get("encryptionType")
    out.append(_r("SB-ENC-05", "Security", enc in ("AES256", "KMS", "KMS_DSSE"),
                  f"ECR encryption={enc}, CloudWatch Logs encrypted by default", "SB-ENC-05"))

    # SB-IAM-01 / SB-IAM-04 / SB-IAM-02: dedicated execution and task roles, both bounded, nothing wildcard
    roles = {k: iam.get_role(RoleName=ctx[k])["Role"] for k in ("exec_role_name", "task_role_name")}
    dedicated = (td.get("executionRoleArn") == roles["exec_role_name"]["Arn"]
                 and td.get("taskRoleArn") == roles["task_role_name"]["Arn"])
    out.append(_r("SB-IAM-01", "Security", dedicated, f"execution/task roles={[r['RoleName'] for r in roles.values()]}", "SB-IAM-01"))
    bounds = {r["RoleName"]: (r.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn") for r in roles.values()}
    out.append(_r("SB-IAM-04", "Security", all(b == ctx["env_boundary_arn"] for b in bounds.values()), f"boundaries={bounds}",
                  "SB-IAM-04"))
    wide = [w for r in roles.values() for w in _wide_statements(iam, r["RoleName"])]
    out.append(_r("SB-IAM-02", "Security", not wide, f"wildcard statements in {wide}" if wide else "role policies scoped", "SB-IAM-02"))

    # SB-SEC-02 and the secrets items (no secrets in the MVP, D-167)
    names = [e["name"] for c in containers for e in c.get("environment") or []]
    secretish = sorted(n for n in names if SECRET_KEY.search(n)) + (["secrets"] if any(c.get("secrets") for c in containers) else [])
    out.append(_r("SB-SEC-02", "Security", not secretish, f"secret-like settings {secretish}" if secretish
                  else f"{len(names)} plain variables", "SB-SEC-02"))
    for sid in ("SB-SEC-01", "SB-ENC-04", "SB-SEC-05"):
        out.append(_r(sid, "Security", True, "no secrets: the Container MVP does not accept secret values (D-167)", sid,
                      status=NOT_APPLICABLE))

    # Load balancer: internal, in subnets without an internet route; this environment's targets healthy
    tg = elbv2.describe_target_groups(TargetGroupArns=[ctx["target_group_arn"]])["TargetGroups"][0]
    lbs = tg.get("LoadBalancerArns") or []
    lb = elbv2.describe_load_balancers(LoadBalancerArns=lbs[:1])["LoadBalancers"][0] if lbs else {}
    subnets = [z["SubnetId"] for z in lb.get("AvailabilityZones") or []]
    rts = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": subnets}])["RouteTables"] if subnets else []
    # One route table may serve several subnets (the shared VPC: 1 private table, 2 subnets). Every ALB subnet must
    # be explicitly associated; an unassociated one falls back to the VPC main table, which this query cannot see.
    associated = {a.get("SubnetId") for t in rts for a in t.get("Associations") or []}
    unassociated = sorted(set(subnets) - associated)
    igw = [r.get("GatewayId") for t in rts for r in t.get("Routes") or [] if str(r.get("GatewayId", "")).startswith("igw-")]
    internal = lb.get("Scheme") == "internal"
    out.append(_r("SB-PUB-02", "Security", internal and bool(subnets) and not unassociated and not igw,
                  f"scheme={lb.get('Scheme')} subnets={len(subnets)} route tables={len(rts)} "
                  f"unassociated={unassociated} internet routes={igw}", "SB-PUB-02"))
    health = elbv2.describe_target_health(TargetGroupArn=ctx["target_group_arn"])["TargetHealthDescriptions"]
    states = [h["TargetHealth"]["State"] for h in health]
    out.append(_r("PR-C-03", "Reachability", bool(states) and all(s == "healthy" for s in states) and len(states) >= ctx["min_tasks"],
                  f"targets={states}"))

    # Security groups (SB-PUB-03, SB-PUB-04, SB-PUB-10)
    sgs = {g["GroupId"]: g for g in ec2.describe_security_groups(
        GroupIds=[ctx["task_security_group_id"], ctx["alb_security_group_id"]])["SecurityGroups"]}
    task_in = sgs[ctx["task_security_group_id"]].get("IpPermissions") or []
    from_alb = all(p.get("FromPort") == ctx["app_port"] == p.get("ToPort") and not p.get("IpRanges") and not p.get("Ipv6Ranges")
                   and not p.get("PrefixListIds")
                   and [g["GroupId"] for g in p.get("UserIdGroupPairs") or []] == [ctx["alb_security_group_id"]] for p in task_in)
    out.append(_r("SB-PUB-03", "Security", bool(task_in) and from_alb, f"task inbound rules={len(task_in)}, ALB only={from_alb}",
                  "SB-PUB-03"))
    alb_in = sgs[ctx["alb_security_group_id"]].get("IpPermissions") or []
    cf_only = all(p.get("PrefixListIds") and not p.get("IpRanges") and not p.get("Ipv6Ranges") for p in alb_in)
    out.append(_r("SB-PUB-04", "Security", bool(alb_in) and cf_only, f"ALB inbound rules={len(alb_in)}, CloudFront only={cf_only}",
                  "SB-PUB-04"))
    open_rules = [g for g, sg in sgs.items() for p in sg.get("IpPermissions") or []
                  if any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges") or [])
                  or any(r.get("CidrIpv6") == "::/0" for r in p.get("Ipv6Ranges") or [])]
    out.append(_r("SB-PUB-10", "Security", not open_rules, f"open ingress on {open_rules}" if open_rules else "no 0.0.0.0/0 ingress",
                  "SB-PUB-10"))

    # Edge: CloudFront reaches the ALB only through the VPC origin; logging; allowlist
    cfg = cloudfront.get_distribution_config(Id=ctx["distribution_id"])["DistributionConfig"]
    log = cfg.get("Logging", {})
    out.append(_r("SB-LOG-02", "Security", bool(log.get("Enabled") and log.get("Bucket")),
                  f"logging={log.get('Bucket')}/{log.get('Prefix')}", "SB-LOG-02"))
    via_vpc_origin = all((o.get("VpcOriginConfig") or {}).get("VpcOriginId") for o in cfg["Origins"]["Items"])
    out.append(_r("PR-C-01", "Security", via_vpc_origin and internal,
                  f"origins via VPC origin={via_vpc_origin}, ALB scheme={lb.get('Scheme')}", "SB-PUB-02"))
    if ctx.get("ip_allowlist"):
        out.append(_r("SB-PUB-08", "Security", bool(cfg.get("WebACLId")), f"web_acl={cfg.get('WebACLId')}", "SB-PUB-08"))
    else:
        out.append(_r("SB-PUB-08", "Security", True, "public exposure", "SB-PUB-08", status=NOT_APPLICABLE))

    # Logs retention, tags, TTL, CloudTrail
    groups = logs.describe_log_groups(logGroupNamePrefix=ctx["log_group"])["logGroups"]
    days = next((g.get("retentionInDays") for g in groups if g["logGroupName"] == ctx["log_group"]), None)
    out.append(_r("SB-LOG-04", "Observability", days == ctx["log_retention_days"], f"retention={days} days", "SB-LOG-04"))
    tags = {t["key"]: t["value"] for t in ecs.list_tags_for_resource(resourceArn=svc["serviceArn"]).get("tags") or []}
    lacking = [k for k in REQUIRED_TAGS if not tags.get(k)]
    out.append(_r("PR-ALL-09", "Cost", not lacking, f"missing={lacking}" if lacking else "required tags present", "SB-TAG-01"))
    if ctx["tier"] == "DEV":
        out.append(_r("PR-ALL-08", "Operability", bool(tags.get("expires-at")), f"expires-at={tags.get('expires-at')}"))
    else:
        out.append(_r("PR-ALL-08", "Operability", True, "PROD: no TTL", status=NOT_APPLICABLE))
    try:
        trail = cloudtrail.get_trail_status(Name=ctx["trail_name"])
        out.append(_r("SB-LOG-01", "Security", bool(trail.get("IsLogging")), f"trail logging={trail.get('IsLogging')}", "SB-LOG-01"))
    except ClientError as e:
        out.append(_r("SB-LOG-01", "Security", False, f"trail missing ({_err(e)})", "SB-LOG-01"))
    return out
