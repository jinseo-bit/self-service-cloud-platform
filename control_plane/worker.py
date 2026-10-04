"""Step Functions task Lambda: status transitions, post-deploy validation, failure records, TTL sweep."""

from __future__ import annotations

import datetime as dt
import json
import os

import boto3

from platform_core.validation.http import check_served_revision, validate_static_http
from platform_core.validation.result import CheckResult, verdict
from platform_core.validation.state import check_prod_monitoring, disable_distribution, validate_static_state

from .store import Store, now

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
LOG_BUCKET = os.environ.get("LOG_BUCKET", "")
TRAIL_NAME = os.environ.get("TRAIL_NAME", "")
DELETE_SFN = os.environ.get("DELETE_SFN_ARN", "")
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


def validate(event):
    s = _store()
    env = s.get(event["env_id"])
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
    prior = [CheckResult(**{**c, "baseline_ids": tuple(c.get("baseline_ids") or ())})
             for c in (env.get("checks") or []) if c["layer"] == "PLAN"]  # artifact checks come from the current revision only
    prior += [CheckResult(**{**c, "baseline_ids": tuple(c.get("baseline_ids") or ())})
              for c in (env.get("artifact") or {}).get("checks", [])]
    results = prior + state + http
    v = verdict(results)
    if v["security_failed"]:
        disable_distribution(boto3.client("cloudfront"), out["distribution_id"])
        v["endpoint_disabled"] = True
    fields = dict(status=v["verdict"], checks=[r.to_dict() for r in results], verdict=v, validated_at=now())
    fields.update(revision_outcome(env, v["verdict"]))
    if v["verdict"] == "FAILED" and env["tier"] == "PROD" and env.get("current_revision") is None:
        fields["expires_at"] = _in_days(7)  # D-64: never-served failed environments are not left behind
    s.update(env["env_id"], event=MESSAGES[v["verdict"]] + (" 보안 검증 실패로 접속을 차단했습니다." if v.get("endpoint_disabled") else ""),
             **fields)
    return {"env_id": env["env_id"], "verdict": v["verdict"]}


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
    status = "DELETE_FAILED" if stage == "DELETE" else "FAILED"
    fields = dict(status=status, failure={"stage": stage, "error": err.get("Error", "Unknown"),
                                          "cause": str(cause)[:4000], "at": now()})
    if stage == "DELETE":
        fields["delete_attempts"] = int(env.get("delete_attempts") or 0) + 1
    elif stage == "REDEPLOY":
        rev = (env.get("artifact") or {}).get("revision")
        fields["revisions"] = [{**r, "status": "FAILED"} if r["revision"] == rev else r for r in env.get("revisions") or []]
    elif env["tier"] == "PROD" and env.get("current_revision") is None:
        fields["expires_at"] = _in_days(7)  # never served: clean up (D-64)
    msg = {"PLAN": "보안 기준을 위반하는 구성이라 생성 전에 차단했습니다.",
           "PROVISION": "AWS 리소스 생성 중 오류가 발생했습니다. 다시 시도하거나 삭제할 수 있습니다.",
           "VALIDATE": "검증을 실행하지 못했습니다.",
           "DELETE": "삭제 중 오류가 발생했습니다. 자동으로 다시 시도합니다.",
           "REDEPLOY": "새 버전을 배포하지 못했습니다. 다시 시도하거나 이전 버전으로 되돌릴 수 있습니다.",
           }.get(stage, "오류가 발생했습니다.")
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


def sweep(event):
    """Hourly: delete expired environments (D-63 TTL) and failed leftovers (D-64)."""
    s = _store()
    sfn = boto3.client("stepfunctions")
    started = []
    for env in s.list_all():
        exp = env.get("expires_at")
        if not exp or exp > now():
            continue
        if env["status"] in ("READY", "DEGRADED", "FAILED") or (
                env["status"] == "DELETE_FAILED" and int(env.get("delete_attempts") or 0) < MAX_DELETE_ATTEMPTS):
            s.update(env["env_id"], expect_status=(env["status"],), status="DELETING", event="만료되어 자동으로 삭제합니다.")
            ex = sfn.start_execution(stateMachineArn=DELETE_SFN, name=f"{env['env_id']}-ttl-{int(dt.datetime.now().timestamp())}",
                                     input=json.dumps({"env_id": env["env_id"]}))
            s.update(env["env_id"], execution_arn=ex["executionArn"])
            started.append(env["env_id"])
    return {"started": started}


ACTIONS = {"mark": mark, "validate": validate, "fail": fail, "deleted": deleted, "sweep": sweep}


def handler(event, context):
    return ACTIONS[event.get("action", "sweep")](event)
