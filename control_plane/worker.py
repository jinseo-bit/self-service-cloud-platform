"""Step Functions task Lambda: status transitions, post-deploy validation, failure records, TTL sweep."""

from __future__ import annotations

import datetime as dt
import json
import os

import boto3

from platform_core import drift
from platform_core.blueprints import for_env
from platform_core.validation.http import (
    check_served_revision,
    validate_container_http,
    validate_serverless_http,
    validate_static_http,
)
from platform_core.validation.result import CheckResult, verdict
from platform_core.validation.state import (
    check_prod_monitoring,
    disable_distribution,
    disable_function,
    task_public_ips,
    validate_container_state,
    validate_serverless_state,
    validate_static_state,
)

from .store import Store, now

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
LOG_BUCKET = os.environ.get("LOG_BUCKET", "")
TRAIL_NAME = os.environ.get("TRAIL_NAME", "")
DELETE_SFN = os.environ.get("DELETE_SFN_ARN", "")
ENV_BOUNDARY = os.environ.get("ENV_BOUNDARY", "")
REPAIR_SFN = os.environ.get("REPAIR_SFN_ARN", "")
PLATFORM_PREFIX = os.environ.get("PLATFORM_PREFIX", "sscp")
MAX_DELETE_ATTEMPTS = 3

MESSAGES = {
    "VALIDATING": "리소스 생성이 끝나 보안·동작 검증을 시작합니다.",
    "READY": "모든 검증을 통과했습니다. 환경을 사용할 수 있습니다.",
    "DEGRADED": "보안 기준은 통과했지만 앱이 정상 응답하지 않습니다.",
    "FAILED": "검증에 실패해 환경을 사용할 수 없습니다.",
    "DELETED": "AWS 리소스를 모두 삭제했습니다.",
}


def _store() -> Store:
    return Store()


def mark(event):
    env_id, status = event["env_id"], event["status"]
    _store().update(env_id, status=status, event=event.get("message") or MESSAGES.get(status, status))
    return {"env_id": env_id}


def _serverless_checks(env: dict, event: dict) -> tuple[list[CheckResult], list[CheckResult]]:
    out = env["outputs"]
    art = env.get("artifact") or {}
    ctx = {"function_name": out["function_name"], "role_name": out["role_name"], "api_id": out["api_id"],
           "log_group": out["log_group"], "repository_name": out["repository_name"], "repository_url": out["repository_url"],
           "env_boundary_arn": ENV_BOUNDARY, "tier": env["tier"], "trail_name": TRAIL_NAME,
           "log_retention_days": 30 if env["tier"] == "PROD" else 7,
           "alarm_names": out.get("alarm_names") or [], "alert_topic_arn": out.get("alert_topic_arn")}
    state = validate_serverless_state(lambda_=boto3.client("lambda", region_name=REGION), iam=boto3.client("iam"),
                                      logs=boto3.client("logs", region_name=REGION),
                                      apigw=boto3.client("apigatewayv2", region_name=REGION),
                                      ecr=boto3.client("ecr", region_name=REGION),
                                      cloudtrail=boto3.client("cloudtrail", region_name=REGION), ctx=ctx)
    state.append(check_prod_monitoring(cloudwatch=boto3.client("cloudwatch", region_name=REGION),
                                       sns=boto3.client("sns", region_name=REGION), ctx=ctx))
    http = validate_serverless_http(base_url=out["url"], health_path=art.get("health_path") or "/",
                                    attempts=int(event.get("http_attempts", 20)), delay=float(event.get("http_delay", 6)))
    return state, http


APP_PORT = 8080  # D-34


def _container_checks(env: dict, event: dict) -> tuple[list[CheckResult], list[CheckResult]]:
    out, shared = env["outputs"], env.get("shared") or {}
    arch = env["recommendation"]["architecture"]
    allow = env["input"].get("allowlist_cidrs") or []
    ctx = {k: out[k] for k in ("cluster_name", "service_name", "target_group_arn", "task_security_group_id", "exec_role_name",
                               "task_role_name", "log_group", "repository_name", "repository_url", "distribution_id")}
    ctx |= {"alb_security_group_id": shared.get("alb_security_group_id", ""), "env_boundary_arn": ENV_BOUNDARY, "tier": env["tier"],
            "multi_az": arch.get("multi_az", "SINGLE"), "min_tasks": (arch.get("scaling") or {}).get("ctr_min_tasks", 1),
            "app_port": APP_PORT, "log_retention_days": 30 if env["tier"] == "PROD" else 7, "trail_name": TRAIL_NAME,
            "ip_allowlist": allow, "alarm_names": out.get("alarm_names") or [], "alert_topic_arn": out.get("alert_topic_arn")}
    ecs, ec2 = boto3.client("ecs", region_name=REGION), boto3.client("ec2", region_name=REGION)
    state = validate_container_state(ecs=ecs, elbv2=boto3.client("elbv2", region_name=REGION), ec2=ec2, iam=boto3.client("iam"),
                                     logs=boto3.client("logs", region_name=REGION), ecr=boto3.client("ecr", region_name=REGION),
                                     cloudfront=boto3.client("cloudfront"),
                                     cloudtrail=boto3.client("cloudtrail", region_name=REGION), ctx=ctx)
    state.append(check_prod_monitoring(cloudwatch=boto3.client("cloudwatch", region_name=REGION),
                                       sns=boto3.client("sns", region_name=REGION), ctx=ctx))
    http = validate_container_http(base_url=out["url"], health_path=(env.get("artifact") or {}).get("health_path") or "/",
                                   alb_dns_name=shared.get("alb_dns_name", ""),
                                   task_public_ips=task_public_ips(ecs, ec2, ctx["cluster_name"], ctx["service_name"]),
                                   app_port=APP_PORT, ip_allowlist=allow,
                                   attempts=int(event.get("http_attempts", 20)), delay=float(event.get("http_delay", 15)))
    return state, http


def validate(event):
    s = _store()
    env = s.get(event["env_id"])
    return _finish_validation(s, env, *run_checks(env, event))


def run_checks(env: dict, event: dict) -> tuple[list[CheckResult], list[CheckResult]]:
    """STATE and HTTP checks of the environment's blueprint: the same at approval (validate) and on Day-2 (recheck)."""
    if for_env(env).key == "serverless":
        return _serverless_checks(env, event)
    if for_env(env).key == "container":
        return _container_checks(env, event)
    out = env.get("outputs") or {}
    inp = env["input"]
    allow = inp.get("allowlist_cidrs") or []
    ctx = {"bucket": out["bucket_name"], "distribution_id": out["distribution_id"],
           "distribution_arn": out["distribution_arn"], "tier": env["tier"], "ip_allowlist": allow,
           "log_bucket": LOG_BUCKET, "trail_name": TRAIL_NAME,
           "alarm_names": out.get("alarm_names") or [], "alert_topic_arn": out.get("alert_topic_arn")}
    state = validate_static_state(s3=boto3.client("s3", region_name=REGION), cloudfront=boto3.client("cloudfront"),
                                  cloudtrail=boto3.client("cloudtrail", region_name=REGION), ctx=ctx)
    state.append(check_prod_monitoring(cloudwatch=boto3.client("cloudwatch", region_name="us-east-1"),
                                       sns=boto3.client("sns", region_name="us-east-1"), ctx=ctx))
    http = validate_static_http(
        base_url=out["url"],
        direct_origin_url=f"https://{out['bucket_name']}.s3.{REGION}.amazonaws.com/index.html",
        spa_fallback=bool(inp.get("spa_fallback")), ip_allowlist=allow,
        attempts=int(event.get("http_attempts", 20)), delay=float(event.get("http_delay", 15)))

    art = env.get("artifact") or {}
    if not allow:  # PR-S-03 needs to reach the site, impossible from outside an IP allowlist
        http.append(check_served_revision(base_url=out["url"], expected_sha256=art.get("index_sha256"), revision=art.get("revision"),
                                          attempts=int(event.get("http_attempts", 12)), delay=float(event.get("http_delay", 10))))
    return state, http


def _finish_validation(s: Store, env: dict, state: list[CheckResult], http: list[CheckResult]) -> dict:
    prior = [CheckResult(**{**c, "baseline_ids": tuple(c.get("baseline_ids") or ())})
             for c in (env.get("checks") or []) if c["layer"] == "PLAN"]  # artifact checks come from the current revision only
    prior += [CheckResult(**{**c, "baseline_ids": tuple(c.get("baseline_ids") or ())})
              for c in (env.get("artifact") or {}).get("checks", [])]
    results = prior + state + http
    v = verdict(results)
    if v["security_failed"]:  # D-65: take the endpoint offline right away
        _block_endpoint(env)
        v["endpoint_disabled"] = True
    fields = dict(status=v["verdict"], checks=[r.to_dict() for r in results], verdict=v, validated_at=now())
    if env.get("drift"):  # D-186: validation after an approved repair closes the drift and records the metrics
        d = env["drift"]
        fields.update(drift=None, repair_plan=None, last_repair={
            "detected_at": d.get("detected_at"), "event_time": d.get("event_time"), "repaired_at": fields["validated_at"],
            "verdict": v["verdict"], "detect_seconds": d.get("detect_seconds"),
            "repair_seconds": drift.seconds_between(d.get("detected_at"), fields["validated_at"]),
            # repair_seconds includes the wait for a person; split it so the platform's own time is visible
            "approved_at": env.get("repair_approved_at"),
            "wait_seconds": drift.seconds_between(d.get("detected_at"), env.get("repair_approved_at")),
            "apply_seconds": drift.seconds_between(env.get("repair_approved_at"), fields["validated_at"]),
            "approved_by": env.get("repair_approved_by")})
    fields.update(revision_outcome(env, v["verdict"]))
    if v["verdict"] == "FAILED" and env["tier"] == "PROD" and env.get("current_revision") is None:
        fields["expires_at"] = _in_days(7)  # D-64: never-served failed environments are not left behind
    s.update(env["env_id"], event=MESSAGES[v["verdict"]] + (" 보안 검증 실패로 접속을 차단했습니다." if v.get("endpoint_disabled") else ""),
             **fields)
    return {"env_id": env["env_id"], "verdict": v["verdict"]}


def _block_endpoint(env: dict) -> None:
    out = env.get("outputs") or {}
    if for_env(env).key == "serverless":
        disable_function(boto3.client("lambda", region_name=REGION), out["function_name"])
    else:
        disable_distribution(boto3.client("cloudfront"), out["distribution_id"])


# ------------------------------------------------------------------ Day-2: recheck and drift (D-186)

def recheck(event, context=None):
    """Run the environment's STATE and HTTP checks again. A check that fails now but passed at the last verdict is
    drift: the environment becomes DRIFTED, a security drift is blocked at once (D-65), and a repair plan is
    prepared for a person to approve. Nothing is changed back automatically."""
    s = _store()
    env = s.get(event["env_id"])
    trigger = event.get("trigger", "MANUAL")
    if not env or env["status"] not in drift.RECHECKABLE:
        return {"env_id": event["env_id"], "skipped": env["status"] if env else "missing"}
    state, http = run_checks(env, {**event, "http_attempts": event.get("http_attempts", 3),
                                   "http_delay": event.get("http_delay", 5)})
    results = state + http
    at = now()
    last = {"at": at, "trigger": trigger, "failed": sorted(drift.failed_ids(results))}
    blocked = drift.block_effects(env, results)
    fails = [r for r in drift.new_failures(env, results) if r.check_id not in blocked]
    if fails:
        known = {f["check_id"] for f in (env.get("drift") or {}).get("failed") or []}
        if env["status"] == "DRIFTED" and {r.check_id for r in fails} <= known:
            s.update(env["env_id"], last_recheck=last)
            return {"env_id": env["env_id"], "drift": "unchanged"}
        rec = drift.drift_record(env, fails, trigger, event.get("detail"), at)
        rec["detect_seconds"] = drift.seconds_between(rec["event_time"], at)
        if rec["security"]:
            _block_endpoint(env)
            rec["endpoint_disabled"] = True
        elif blocked:  # still blocked from the earlier security drift
            rec["endpoint_disabled"] = True
        who = f" ({rec['event_name']}, {rec['actor']})" if rec.get("event_name") else ""
        msg = (f"설정이 승인된 구성과 달라졌습니다{who}: {', '.join(r.check_id for r in fails)}."
               + (" 보안 기준 위반이라 접속을 차단했습니다." if rec["security"] else "")
               + " 원래 구성으로 되돌리는 수정안을 만듭니다. 적용은 사람이 승인해야 합니다.")
        s.update(env["env_id"], expect_status=(env["status"],), status="DRIFTED", drift=rec, repair_plan=None,
                 checks=_merge_checks(env, results), last_recheck=last,
                 drift_history=((env.get("drift_history") or []) + [rec])[-10:], event=msg)
        _start_repair(env["env_id"], "plan")
        return {"env_id": env["env_id"], "drift": [r.check_id for r in fails]}
    if blocked:  # the change was undone by hand, but only the approved repair turns the endpoint back on (D-65)
        s.update(env["env_id"], last_recheck=last)
        return {"env_id": env["env_id"], "drift": "unchanged"}
    if env["status"] == "DRIFTED":  # changed back by hand before anyone approved the repair
        back = (env.get("verdict") or {}).get("verdict") or "READY"
        s.update(env["env_id"], expect_status=("DRIFTED",), status=back if back in ("READY", "DEGRADED") else "READY",
                 drift=None, repair_plan=None, checks=_merge_checks(env, results), last_recheck=last,
                 event="다시 검사했더니 승인된 구성으로 돌아와 있습니다. 차이가 사라졌습니다.")
        return {"env_id": env["env_id"], "drift": "resolved"}
    s.update(env["env_id"], last_recheck=last)
    return {"env_id": env["env_id"], "drift": None}


def _merge_checks(env: dict, results: list[CheckResult]) -> list[dict]:
    """Keep PLAN and artifact checks from approval; replace STATE and HTTP with the fresh results."""
    kept = [c for c in env.get("checks") or [] if c.get("layer") not in ("STATE", "HTTP")]
    return kept + [r.to_dict() for r in results]


def _start_repair(env_id: str, phase: str) -> None:
    if not REPAIR_SFN:
        return
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=REPAIR_SFN, name=f"{env_id}-repair-{phase}-{int(dt.datetime.now().timestamp())}",
        input=json.dumps({"env_id": env_id, "phase": phase}))


def drift_event(event, context=None):
    """EventBridge: a CloudTrail write call on a watched service. Recheck the environments it touched."""
    detail = event.get("detail") or {}
    hits = drift.match_environments(detail, _store().list_all(), PLATFORM_PREFIX)
    return {"rechecked": [recheck({"env_id": e, "trigger": "EVENT", "detail": detail}) for e in hits]}


def revision_outcome(env: dict, verdict_state: str) -> dict:
    """Deployment revision record after validation (D-150). Shared with the local simulator."""
    rev = (env.get("artifact") or {}).get("revision")
    if rev is None:
        return {}
    out = {"revisions": [{**r, "status": verdict_state, "deployed_at": now()} if r["revision"] == rev else r
                         for r in env.get("revisions") or []]}
    if verdict_state in ("READY", "DEGRADED"):  # only a servable revision becomes "current"
        out["current_revision"] = rev
    return out


def fail(event):
    env_id = event["env_id"]
    stage = event.get("stage", "PROVISION")
    err = event.get("error") or {}
    cause = err.get("Cause", "")
    try:
        cause = json.loads(cause).get("Build", {}).get("Phases", cause) if cause.startswith("{") else cause
    except (ValueError, AttributeError):  # Cause is free text, not CodeBuild JSON
        cause = str(cause)
    s = _store()
    env = s.get(env_id)
    if stage == "PROVISION" and env.get("plan_rejected"):
        stage = "PLAN"
    elif stage == "PROVISION" and env.get("image_build_failed"):
        stage = "IMAGE"
        cause = env.get("image_build_error") or cause
    elif stage == "PROVISION" and env.get("terraform_prepare_failed"):
        stage = "TERRAFORM_PREPARE"
        cause = env.get("terraform_error") or cause
    elif stage == "PROVISION" and env.get("terraform_error"):  # apply failed: keep the stage, show the real error to admins
        cause = env["terraform_error"]
    status = "DELETE_FAILED" if stage == "DELETE" else "FAILED"
    fields = dict(status=status)
    if stage == "DELETE":
        fields["delete_attempts"] = int(env.get("delete_attempts") or 0) + 1
    elif stage == "REDEPLOY":
        rev = (env.get("artifact") or {}).get("revision")
        fields["revisions"] = [{**r, "status": "FAILED"} if r["revision"] == rev else r for r in env.get("revisions") or []]
    elif env["tier"] == "PROD" and env.get("current_revision") is None:
        fields["expires_at"] = _in_days(7)  # never served: clean up (D-64)
    bp = for_env(env)
    image_msg = ("컨테이너 이미지를 가져오지 못했습니다." if bp and bp.key == "container" else "Lambda용 이미지를 만들지 못했습니다.")
    msg = {"PLAN": "보안 기준을 위반하는 구성이라 생성 전에 차단했습니다.",
           "IMAGE": f"{image_msg} 이미지 주소와 공개 여부를 확인한 뒤 다시 시도해 주세요.",
           "TERRAFORM_PREPARE": "Terraform 준비 단계에서 실패했습니다. 다시 시도하거나 삭제할 수 있습니다.",
           "PROVISION": "AWS 리소스 생성 중 오류가 발생했습니다. 다시 시도하거나 삭제할 수 있습니다.",
           "VALIDATE": "검증을 실행하지 못했습니다.",
           "DELETE": "삭제 중 오류가 발생했습니다. 자동으로 다시 시도합니다.",
           "REDEPLOY": "새 버전을 배포하지 못했습니다. 다시 시도하거나 이전 버전으로 되돌릴 수 있습니다.",
           }.get(stage, "오류가 발생했습니다.")
    # error/cause are internal (Step Functions error, CodeBuild phases, terraform output): admins and logs only
    fields["failure"] = {"stage": stage, "message": msg, "error": err.get("Error", "Unknown"), "cause": str(cause)[:4000],
                         "at": now()}
    s.update(env_id, event=msg, **fields)
    return {"env_id": env_id, "status": status}


def deleted(event):
    env_id = event["env_id"]
    s3 = boto3.client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=UPLOAD_BUCKET, Prefix=f"uploads/{env_id}/"):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=UPLOAD_BUCKET, Delete={"Objects": objs, "Quiet": True})
    _store().update(env_id, status="DELETED", deleted_at=now(), event=MESSAGES["DELETED"])
    return {"env_id": env_id}


def _in_days(days: int) -> str:
    t = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sweep(event, context=None):
    """Hourly: delete expired environments (D-63 TTL) and failed leftovers (D-64)."""
    s = _store()
    sfn = boto3.client("stepfunctions")
    started = []
    for env in s.list_all():
        exp = env.get("expires_at")
        if not exp or exp > now():
            continue
        if env["status"] in ("READY", "DEGRADED", "DRIFTED", "FAILED") or (
                env["status"] == "DELETE_FAILED" and int(env.get("delete_attempts") or 0) < MAX_DELETE_ATTEMPTS):
            s.update(env["env_id"], expect_status=(env["status"],), status="DELETING", event="만료되어 자동으로 삭제합니다.")
            ex = sfn.start_execution(stateMachineArn=DELETE_SFN, name=f"{env['env_id']}-ttl-{int(dt.datetime.now().timestamp())}",
                                     input=json.dumps({"env_id": env["env_id"]}))
            s.update(env["env_id"], execution_arn=ex["executionArn"])
            started.append(env["env_id"])
    # Day-2 (D-186): recheck every running environment, one async invocation each so a slow HTTP probe cannot
    # hold up the others. This also covers what the event rule cannot see (CloudFront and IAM are global services).
    rechecked = []
    fn = getattr(context, "function_name", None)
    for env in s.list_all():
        if env["env_id"] in started or env["status"] not in drift.RECHECKABLE:
            continue
        payload = {"action": "recheck", "env_id": env["env_id"], "trigger": "SCHEDULE"}
        if fn:
            boto3.client("lambda", region_name=REGION).invoke(FunctionName=fn, InvocationType="Event",
                                                              Payload=json.dumps(payload).encode())
        else:
            recheck(payload)
        rechecked.append(env["env_id"])
    return {"started": started, "rechecked": rechecked}


def repair_failed(event):
    """A repair plan or apply failed. The environment is still running with the drift, so it stays DRIFTED."""
    s = _store()
    phase = event.get("phase", "plan")
    msg = ("원래 구성으로 되돌리는 수정안을 만들지 못했습니다. 관리자에게 알려 주세요." if phase == "plan"
           else "복구 적용에 실패했습니다. 최신 상태로 수정안을 다시 만듭니다.")
    s.update(event["env_id"], status="DRIFTED", repair_plan={"error": msg, "at": now()}, event=msg)
    if phase == "apply":
        _start_repair(event["env_id"], "plan")
    return {"env_id": event["env_id"]}


ACTIONS = {"mark": mark, "validate": validate, "fail": fail, "deleted": deleted, "sweep": sweep,
           "recheck": recheck, "drift_event": drift_event, "repair_failed": repair_failed}


def handler(event, context):
    if event.get("detail-type") == "AWS API Call via CloudTrail":  # the EventBridge rule (D-186)
        return drift_event(event, context)
    action = event.get("action", "sweep")
    fn = ACTIONS[action]
    return fn(event, context) if action in ("sweep", "recheck", "drift_event") else fn(event)
