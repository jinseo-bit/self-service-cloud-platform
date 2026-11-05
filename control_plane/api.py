"""HTTP API Lambda (API Gateway HTTP API, payload v2, Cognito JWT authorizer)."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import traceback

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from platform_core import baseline, templates
from platform_core.blueprints import for_architecture, for_env
from platform_core.engine import InputError, RawInput, recommend
from platform_core.engine import constants as C
from platform_core.engine.admission import ACTIVE, admit
from platform_core.presets import PRESETS
from platform_core.pricing import PriceTable
from platform_core.production_ready import CHECK_TITLES, checks_for
from platform_core.validation.artifact import MAX_BUNDLE_BYTES, bundle_summary, index_sha256, validate_static_bundle
from platform_core.validation.image import validate_image_spec

from .store import Store, new_env_id, now

PRICES = PriceTable.load()
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
PROVISION_SFN = os.environ.get("PROVISION_SFN_ARN", "")
DELETE_SFN = os.environ.get("DELETE_SFN_ARN", "")
REDEPLOY_SFN = os.environ.get("REDEPLOY_SFN_ARN", "")
REPAIR_SFN = os.environ.get("REPAIR_SFN_ARN", "")
WORKER_FUNCTION = os.environ.get("WORKER_FUNCTION", "")

# Browsers upload straight to S3 with the presigned POST, so the URL must be the Regional virtual-hosted endpoint.
# The default (global s3.amazonaws.com) is 307-redirected for new buckets outside us-east-1, and a cross-origin
# upload that is redirected fails CORS in the browser ("Failed to fetch").
_s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
                   config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
_sfn = boto3.client("stepfunctions")
_lambda = boto3.client("lambda")
_store = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def resp(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False, default=str)}


AGENT_CLIENT_ID = os.environ.get("AGENT_CLIENT_ID", "")
# D-181: what a token issued to the agent's Cognito client may call. Everything else (approve, delete, retry, redeploy,
# extend) changes AWS or spends money and stays with a person in the UI. Allowlist, so a new route is human-only.
AGENT_ROUTES = {"GET /presets", "GET /me", "POST /recommendations", "POST /environments", "GET /environments",
                "GET /environments/{env_id}", "POST /environments/{env_id}/upload-url",
                "POST /environments/{env_id}/recheck"}  # a recheck only reads; repair stays human (D-186)


def caller(event) -> dict:
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    groups = claims.get("cognito:groups", "")
    if isinstance(groups, str):
        groups = [g for g in groups.strip("[]").replace(",", " ").split() if g]
    agent = bool(AGENT_CLIENT_ID) and AGENT_CLIENT_ID in (claims.get("aud"), claims.get("client_id"))
    # An agent token never carries admin power, even when the person behind it is an admin.
    return {"sub": claims["sub"], "email": claims.get("email", ""), "admin": "admin" in groups and not agent, "agent": agent}


def body(event) -> dict:
    raw = event.get("body") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HttpError(400, "invalid JSON body") from None


def upload_key(env_id: str, revision: int = 1) -> str:
    """One object per revision, so a redeploy never overwrites what is currently served (D-150)."""
    return f"uploads/{env_id}/rev-{int(revision)}.zip"


def upload_prefix(env_id: str) -> str:
    return f"uploads/{env_id}/"


def next_revision(env: dict) -> int:
    return max([r["revision"] for r in env.get("revisions") or []] + [0]) + 1


def load_env(env_id: str, who: dict) -> dict:
    env = store().get(env_id)
    if not env or (env["owner_sub"] != who["sub"] and not who["admin"]):
        raise HttpError(404, "environment not found")
    return env


def public_view(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in ("execution_arn",)}


INTERNAL_FAILURE_FIELDS = ("image_build_error", "terraform_error")


def viewer_payload(payload: dict, who: dict) -> dict:
    """Raw failure detail (Step Functions error, CodeBuild phases, terraform output) is for admins and logs;
    the owner sees the stage and the plain message."""
    env = payload.get("environment")
    if who["admin"] or not isinstance(env, dict):
        return payload
    env = {k: v for k, v in env.items() if k not in INTERNAL_FAILURE_FIELDS}
    if env.get("failure"):
        env["failure"] = {k: env["failure"][k] for k in ("stage", "message", "at") if k in env["failure"]}
    return {**payload, "environment": env}


def _recommend(inp: dict) -> dict:
    try:
        return _with_checks(recommend(RawInput.from_dict(inp), PRICES))
    except InputError as e:
        raise HttpError(400, str(e)) from None


def _requirements(b: dict) -> tuple[dict, dict]:
    """Custom flow sends `input`; Quick Start sends `template` {id, values} (D-163). Both end in the same engine."""
    t = b.get("template")
    if t:
        try:
            inp = templates.build_input(t.get("id", ""), t.get("values") or {})
        except InputError as e:
            raise HttpError(400, str(e)) from None
        rec = _recommend(inp)
        rec["origin"] = templates.origin(t["id"], rec)
        rec["requirements"] = inp
        return inp, rec
    inp = b.get("input") or {}
    rec = _recommend(inp)
    rec["origin"] = {"kind": "CUSTOM"}
    return inp, rec


def upload_form(env_id: str, revision: int = 1) -> dict:
    return _s3.generate_presigned_post(
        UPLOAD_BUCKET, upload_key(env_id, revision),
        Fields={"Content-Type": "application/zip"},
        Conditions=[{"Content-Type": "application/zip"}, ["content-length-range", 1, MAX_BUNDLE_BYTES]],
        ExpiresIn=900,
    )


# ------------------------------------------------------------------ routes

def get_presets(event, who):
    items = [{"id": i.id, "title": i.title, "plain": i.plain, "layers": sorted(i.layers)} for i in baseline.ITEMS]
    return 200, {"presets": PRESETS, "templates": templates.public_templates(), "baseline": items, "check_titles": CHECK_TITLES}


def _with_checks(rec: dict) -> dict:
    rec["production_ready_checks"] = checks_for(rec.get("architecture", {}).get("candidate_id", ""))
    bp = for_architecture(rec.get("architecture")) if rec.get("provisionable") else None
    rec["artifact_kind"] = bp.artifact if bp else None  # what the UI asks for: a site zip or a container image
    rec["blueprint"] = bp.key if bp else None
    return rec


def image_artifact(spec: dict, revision: int = 1) -> dict:
    """Serverless input (D-166): validated at creation, there is no upload to inspect later."""
    checks, norm = validate_image_spec(spec or {})
    return {"kind": "IMAGE", "revision": int(revision), "image": norm["image"], "env": norm["env"],
            "health_path": norm["health_path"], "checks": [c.to_dict() for c in checks],
            "passed": all(c.status != "FAIL" for c in checks), "validated_at": now()}


def get_me(event, who):
    mine = store().list_owner(who["sub"])
    return 200, {**who, "quota": C.QUOTA_PER_USER, "platform_cap_usd": C.PLATFORM_MONTHLY_CAP_USD,
                 "environments": len([e for e in mine if e["status"] != "DELETED"])}


def post_recommendation(event, who):
    return 200, _requirements(body(event))[1]


def post_environment(event, who):
    b = body(event)
    name = (b.get("name") or "").strip()[:60]
    if not name:
        raise HttpError(400, "name is required")
    inp, rec = _requirements(b)
    override = None
    if b.get("budget_override") and who.get("agent"):
        raise HttpError(403, "예산 초과 동의는 사람이 화면에서 해야 합니다.", human_action_required=True)
    if b.get("budget_override"):
        # D-158: only an explicit confirmation of a SOFT budget excess is recorded; hard guards still apply at approval.
        if rec.get("status") != "OVER_BUDGET":
            raise HttpError(400, "예산을 넘지 않는 구성에는 예산 초과 승인이 필요 없습니다.")
        override = {"confirmed": True, "budget_usd": rec["cost"]["budget_usd"], "cost_rep_usd": rec["cost"]["rep_usd"],
                    "cost_upper_usd": rec["cost"]["upper_usd"],
                    "excess_upper_usd": rec["cost"]["upper_usd"] - rec["cost"]["budget_usd"],
                    "confirmed_by": who["email"] or who["sub"], "at": now()}
    bp = for_architecture(rec.get("architecture")) if rec.get("provisionable") else None
    artifact = None
    if bp and bp.artifact == "IMAGE":
        artifact = image_artifact(b.get("image") or {})
        if not artifact["passed"]:
            raise HttpError(400, "컨테이너 이미지 설정이 검사를 통과하지 못했습니다.", artifact=artifact)
    env_id = new_env_id()
    env = {
        "env_id": env_id, "name": name, "owner_sub": who["sub"], "owner_email": who["email"], "artifact": artifact,
        "status": "DRAFT", "tier": inp["tier"], "created_at": now(), "updated_at": now(),
        "input": inp, "recommendation": rec, "origin": rec["origin"],
        "cost_upper_usd": int(rec.get("cost", {}).get("upper_usd") or 0),
        "budget_override": override,
        "created_via": "agent" if who.get("agent") else "ui",
        "event_log": [{"at": now(), "status": "DRAFT", "message": "Claude Code agent가 추천 결과와 함께 환경 초안을 만들었습니다. "
                       "사람이 화면에서 승인해야 AWS 리소스가 만들어집니다." if who.get("agent")
                       else "추천 결과와 함께 환경 초안을 만들었습니다."}]
        + ([{"at": now(), "status": "DRAFT", "message": f"예산(월 ${override['budget_usd']})을 넘는 것을 확인하고 진행합니다 "
             f"(최대 월 ${override['cost_upper_usd']})."}] if override else []),
    }
    store().put(env)
    return 201, {"environment": public_view(env), "upload": upload_form(env_id, 1) if bp and bp.artifact == "STATIC_ZIP" else None}


def post_upload_url(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    bp = for_env(env)
    if bp is None or bp.artifact != "STATIC_ZIP":
        raise HttpError(409, "이 환경은 파일 업로드가 아니라 컨테이너 이미지를 사용합니다.")
    if env["status"] == "DRAFT":
        return 200, {"upload": upload_form(env["env_id"], 1), "revision": 1}
    if who.get("agent"):  # a new revision of a running site is a redeploy: a person does that
        raise HttpError(403, "운영 중인 환경의 새 버전은 사람이 화면에서 올려야 합니다.", human_action_required=True)
    if env["status"] not in ("READY", "DEGRADED") and not _redeploy_failed(env):
        raise HttpError(409, f"cannot upload in status {env['status']}")
    rev = next_revision(env)
    return 200, {"upload": upload_form(env["env_id"], rev), "revision": rev}


def _validate_artifact(env_id: str, revision: int = 1) -> dict:
    key = upload_key(env_id, revision)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "site.zip")
        try:
            _s3.download_file(UPLOAD_BUCKET, key, path)
        except ClientError:
            raise HttpError(409, "정적 사이트 파일(zip)을 먼저 업로드해 주세요.") from None
        results, root = validate_static_bundle(path)
        fingerprint = index_sha256(path, root) if all(r.status != "FAIL" for r in results) else None
        summary = bundle_summary(path)
    return {"key": key, "revision": int(revision), "root": root, "index_sha256": fingerprint, "checks": [r.to_dict() for r in results],
            "summary": summary,
            "passed": all(r.status != "FAIL" for r in results), "validated_at": now()}


def _revision_record(artifact: dict, who: dict, note: str = "") -> dict:
    if artifact.get("kind") == "IMAGE":
        return {"revision": artifact["revision"], "key": artifact["image"], "root": "", "uploaded_by": who["email"] or who["sub"],
                "uploaded_at": artifact["validated_at"], "files": artifact["image"], "status": "DEPLOYING", "note": note}
    files = next((c["detail"] for c in artifact["checks"] if c["check_id"] == "ART-SIZE"), "")
    return {"revision": artifact["revision"], "key": artifact["key"], "root": artifact["root"],
            "uploaded_by": who["email"] or who["sub"], "uploaded_at": artifact["validated_at"], "files": files,
            "status": "DEPLOYING", "note": note}


def _redeploy_failed(env: dict) -> bool:
    """FAILED after a revision had already been served (redeploy publish or post-redeploy validation failed).
    Such an environment can retry, redeploy or roll back; a never-served failure cannot."""
    if env["status"] != "FAILED":
        return False
    if (env.get("failure") or {}).get("stage") == "REDEPLOY":
        return True
    return any(r.get("status") in ("READY", "DEGRADED") for r in env.get("revisions") or [])


def _delete_uploads(env_id: str) -> None:
    for page in _s3.get_paginator("list_objects_v2").paginate(Bucket=UPLOAD_BUCKET, Prefix=upload_prefix(env_id)):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            _s3.delete_objects(Bucket=UPLOAD_BUCKET, Delete={"Objects": objs, "Quiet": True})


def _start(env: dict, sfn_arn: str, kind: str) -> str:
    ex = _sfn.start_execution(stateMachineArn=sfn_arn, name=f"{env['env_id']}-{kind}-{int(dt.datetime.now().timestamp())}",
                              input=json.dumps({"env_id": env["env_id"]}))
    return ex["executionArn"]


def post_approve(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    rec = env["recommendation"]
    if env["status"] == "PENDING_ADMIN_APPROVAL":
        if not who["admin"]:
            raise HttpError(403, "관리자 승인을 기다리고 있습니다.")
    elif env["status"] != "DRAFT":
        raise HttpError(409, f"cannot approve in status {env['status']}")

    bp = for_env(env)
    if bp is not None and bp.artifact == "IMAGE":
        artifact = image_artifact({"image": (env.get("artifact") or {}).get("image"), "env": (env.get("artifact") or {}).get("env"),
                                   "health_path": (env.get("artifact") or {}).get("health_path")})
        store().update(env["env_id"], artifact=artifact)
        if not artifact["passed"]:
            raise HttpError(422, "컨테이너 이미지 설정이 검사를 통과하지 못했습니다.", artifact=artifact)
    else:
        artifact = _validate_artifact(env["env_id"], 1)
        store().update(env["env_id"], artifact=artifact)
        if not artifact["passed"]:
            raise HttpError(422, "업로드한 파일이 검사를 통과하지 못했습니다.", artifact=artifact)

    s = store()
    blocks = admit(rec, owner_envs=s.list_owner(env["owner_sub"]), all_envs=s.list_all(), env_id=env["env_id"],
                   budget_override=bool((env.get("budget_override") or {}).get("confirmed")))
    if blocks:
        s.update(env["env_id"], admission={"blocked": blocks, "at": now()})
        raise HttpError(409, "승인할 수 없습니다.", blocked=blocks)

    if env["tier"] == "PROD" and not who["admin"]:
        updated = s.update(env["env_id"], expect_status=("DRAFT",), status="PENDING_ADMIN_APPROVAL",
                           event="운영 환경이라 관리자 승인을 요청했습니다.")
        return 202, {"environment": public_view(updated)}

    expires = None
    if env["tier"] == "DEV":
        expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=C.TTL_DAYS_DEV)).replace(microsecond=0)
        expires = expires.isoformat().replace("+00:00", "Z")
    updated = s.update(env["env_id"], expect_status=("DRAFT", "PENDING_ADMIN_APPROVAL"), status="PROVISIONING",
                       approved_by=who["email"] or who["sub"], approved_at=now(), expires_at=expires,
                       admission={"blocked": [], "at": now()}, revisions=[_revision_record(artifact, who, "최초 배포")],
                       event="승인되어 AWS 리소스 생성을 시작합니다.")
    arn = _start(updated, PROVISION_SFN, "provision")
    s.update(env["env_id"], execution_arn=arn)
    return 202, {"environment": public_view(updated)}


def delete_environment(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    st = env["status"]
    if st in ("DRAFT", "PENDING_ADMIN_APPROVAL"):
        _delete_uploads(env["env_id"])
        updated = store().update(env["env_id"], expect_status=(st,), status="DELETED", event="생성 전에 삭제했습니다.")
        return 200, {"environment": public_view(updated)}
    if st not in ("READY", "DEGRADED", "DRIFTED", "FAILED", "DELETE_FAILED"):
        raise HttpError(409, f"cannot delete in status {st}")
    updated = store().update(env["env_id"], expect_status=(st,), status="DELETING", event="삭제를 시작합니다.")
    store().update(env["env_id"], execution_arn=_start(updated, DELETE_SFN, "delete"))
    return 202, {"environment": public_view(updated)}


def post_redeploy(event, who):
    """D-150: new static revision into the same environment — no terraform, infrastructure unchanged.

    body {} deploys the newest uploaded revision; body {"revision": n} rolls back to an earlier one."""
    env = load_env(event["pathParameters"]["env_id"], who)
    st = env["status"]
    if st not in ("READY", "DEGRADED") and not _redeploy_failed(env):
        raise HttpError(409, f"사용 중인 환경(READY/DEGRADED)만 다시 배포할 수 있습니다. 현재 상태: {st}")
    bp = for_env(env)
    if bp is None or not bp.redeploy:
        raise HttpError(409, "재배포는 정적 사이트 환경만 지원합니다.")
    b = body(event)
    known = {r["revision"]: r for r in env.get("revisions") or []}
    rollback = b.get("revision") is not None
    if rollback:
        rev = int(b["revision"])
        if rev not in known:
            raise HttpError(404, f"rev-{rev}을(를) 찾을 수 없습니다.")
        if rev == (env.get("artifact") or {}).get("revision") and st != "FAILED":
            raise HttpError(409, f"rev-{rev}은(는) 이미 배포되어 있습니다.")
    else:
        rev = next_revision(env)
    artifact = _validate_artifact(env["env_id"], rev)  # re-checked even for rollbacks
    if not artifact["passed"]:
        raise HttpError(422, "업로드한 파일이 검사를 통과하지 못했습니다.", artifact=artifact)
    revisions = [r for r in env.get("revisions") or [] if r["revision"] != rev]
    record = known.get(rev) or _revision_record(artifact, who)
    revisions.append({**record, "status": "DEPLOYING", "note": f"rev-{rev}(으)로 되돌리기" if rollback else record.get("note", "")})
    revisions.sort(key=lambda r: r["revision"])
    updated = store().update(env["env_id"], expect_status=(st,), status="REDEPLOYING", artifact=artifact, revisions=revisions,
                             previous_revision=(env.get("artifact") or {}).get("revision"),
                             event=(f"rev-{rev}(으)로 되돌립니다." if rollback else f"새 버전 rev-{rev}을(를) 배포합니다.")
                             + " 인프라는 바꾸지 않습니다.")
    store().update(env["env_id"], execution_arn=_start(updated, REDEPLOY_SFN, f"redeploy-r{rev}"))
    return 202, {"environment": public_view(updated)}


def post_retry(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    failure = env.get("failure") or {}
    if _redeploy_failed(env):  # a served environment retries the redeploy, never a re-provision
        updated = store().update(env["env_id"], expect_status=("FAILED",), status="REDEPLOYING", event="재배포를 다시 시도합니다.")
        store().update(env["env_id"], execution_arn=_start(updated, REDEPLOY_SFN, "redeploy-retry"))
        return 202, {"environment": public_view(updated)}
    if env["status"] != "FAILED" or failure.get("stage") == "PLAN":
        raise HttpError(409, "생성 단계에서 실패한 환경만 다시 시도할 수 있습니다(보안 기준 위반은 재시도 대상이 아님).")
    updated = store().update(env["env_id"], expect_status=("FAILED",), status="PROVISIONING", plan_rejected=False,
                             image_build_failed=False, terraform_prepare_failed=False, terraform_error="",
                             event="다시 시도합니다.")
    store().update(env["env_id"], execution_arn=_start(updated, PROVISION_SFN, "retry"))
    return 202, {"environment": public_view(updated)}


def post_extend(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    if env["tier"] != "DEV" or not env.get("expires_at") or env["status"] not in ("READY", "DEGRADED", "DRIFTED"):
        raise HttpError(409, "개발 환경(READY/DEGRADED)만 연장할 수 있습니다.")
    base = max(dt.datetime.now(dt.UTC), dt.datetime.fromisoformat(env["expires_at"].replace("Z", "+00:00")))
    limit = dt.datetime.now(dt.UTC) + dt.timedelta(days=4 * C.TTL_DAYS_DEV)
    new = min(base + dt.timedelta(days=C.TTL_DAYS_DEV), limit).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    updated = store().update(env["env_id"], expires_at=new, event=f"만료일을 {new}로 연장했습니다.")
    return 200, {"environment": public_view(updated)}


def post_recheck(event, who):
    """Day-2 (D-186): run the environment's checks again now, instead of waiting for the hourly sweep."""
    env = load_env(event["pathParameters"]["env_id"], who)
    if env["status"] not in ("READY", "DEGRADED", "DRIFTED"):
        raise HttpError(409, f"사용 중인 환경만 다시 검사할 수 있습니다. 현재 상태: {env['status']}")
    _lambda.invoke(FunctionName=WORKER_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"action": "recheck", "env_id": env["env_id"], "trigger": "MANUAL"}).encode())
    store().update(env["env_id"], event=f"{'agent' if who.get('agent') else (who['email'] or who['sub'])}의 요청으로 다시 검사합니다.")
    return 202, {"environment": public_view(store().get(env["env_id"]))}


def post_repair(event, who):
    """Day-2 (D-186): a person approves the prepared repair plan; exactly that saved plan is applied, then validated."""
    env = load_env(event["pathParameters"]["env_id"], who)
    if env["status"] != "DRIFTED":
        raise HttpError(409, f"설정 변경이 감지된 환경만 복구할 수 있습니다. 현재 상태: {env['status']}")
    plan = env.get("repair_plan") or {}
    if not plan.get("stacks"):
        raise HttpError(409, "수정안을 아직 만들고 있습니다. 잠시 뒤 다시 시도해 주세요." if not plan.get("error") else plan["error"])
    if not plan.get("passed"):
        raise HttpError(409, "수정안이 보안 검사를 통과하지 못해 적용할 수 없습니다. 관리자에게 알려 주세요.")
    if not plan.get("changes"):
        raise HttpError(409, "Terraform으로 되돌릴 차이가 없습니다. 관리자 확인이 필요합니다.")
    if plan.get("created_at", "") < (env.get("drift") or {}).get("detected_at", ""):
        raise HttpError(409, "마지막 변경 감지 이후의 수정안이 아닙니다. 새 수정안을 기다려 주세요.")
    updated = store().update(env["env_id"], expect_status=("DRIFTED",), status="REPAIRING",
                             repair_approved_by=who["email"] or who["sub"], repair_approved_at=now(),
                             event=f"수정안(리소스 {plan['changes']}개)을 승인했습니다. 원래 구성으로 되돌립니다.")
    ex = _sfn.start_execution(stateMachineArn=REPAIR_SFN, name=f"{env['env_id']}-repair-apply-{int(dt.datetime.now().timestamp())}",
                              input=json.dumps({"env_id": env["env_id"], "phase": "apply"}))
    store().update(env["env_id"], execution_arn=ex["executionArn"])
    return 202, {"environment": public_view(updated)}


def list_environments(event, who):
    qs = event.get("queryStringParameters") or {}
    envs = store().list_all() if (who["admin"] and qs.get("all") == "1") else store().list_owner(who["sub"])
    envs.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    slim = [{k: e.get(k) for k in ("env_id", "name", "status", "tier", "created_at", "expires_at", "cost_upper_usd",
                                   "owner_email", "created_via")} | {"url": (e.get("outputs") or {}).get("url"),
                                                      "candidate_id": e["recommendation"].get("architecture", {}).get("candidate_id")}
            | _health(e)
            for e in envs if e["status"] != "DELETED" or qs.get("deleted") == "1"]
    return 200, {"environments": slim, "summary": _fleet_summary(slim, who)}


def _health(e: dict) -> dict:
    """D-194 overview: what the last check said, without sending every check result."""
    v, d = e.get("verdict") or {}, e.get("drift") or {}
    return {"failed_count": len(v.get("failed") or []),
            "security_failed": bool(v.get("security_failed") or d.get("security")),
            "endpoint_disabled": bool(v.get("endpoint_disabled") or d.get("endpoint_disabled")),
            "drift_since": d.get("detected_at"),
            "checked_at": (e.get("last_recheck") or {}).get("at") or e.get("validated_at")}


def _fleet_summary(envs: list[dict], who: dict) -> dict:
    """Totals for the overview (D-194). The cost total uses the same statuses as the platform cap (admission.ACTIVE)."""
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    active = [e for e in envs if e["status"] in ACTIVE]
    return {"total": len(envs), "active": len(active),
            "attention": sum(1 for e in envs if e["status"] in ("FAILED", "DEGRADED", "DRIFTED", "DELETE_FAILED")),
            "security": sum(1 for e in envs if e["security_failed"] and e["status"] not in ("DELETED", "DRAFT")),
            "expiring": sum(1 for e in active if e.get("expires_at") and e["expires_at"] <= soon),
            "committed_usd": sum(int(e.get("cost_upper_usd") or 0) for e in active),
            "cap_usd": C.PLATFORM_MONTHLY_CAP_USD, "scope": "all" if who["admin"] else "mine"}


def get_environment(event, who):
    return 200, {"environment": public_view(load_env(event["pathParameters"]["env_id"], who))}


ROUTES = {
    "GET /presets": get_presets,
    "GET /me": get_me,
    "POST /recommendations": post_recommendation,
    "POST /environments": post_environment,
    "GET /environments": list_environments,
    "GET /environments/{env_id}": get_environment,
    "POST /environments/{env_id}/upload-url": post_upload_url,
    "POST /environments/{env_id}/approve": post_approve,
    "POST /environments/{env_id}/extend": post_extend,
    "POST /environments/{env_id}/retry": post_retry,
    "POST /environments/{env_id}/redeploy": post_redeploy,
    "POST /environments/{env_id}/recheck": post_recheck,
    "POST /environments/{env_id}/repair": post_repair,
    "DELETE /environments/{env_id}": delete_environment,
}


def handler(event, context):
    route = ROUTES.get(event.get("routeKey"))
    if route is None:
        return resp(404, {"error": "not found"})
    try:
        who = caller(event)
        if who["agent"] and event.get("routeKey") not in AGENT_ROUTES:
            raise HttpError(403, "이 작업은 사람이 화면에서 직접 해야 합니다. agent는 초안 생성과 조회까지만 할 수 있습니다.",
                            human_action_required=True)
        status, payload = route(event, who)
        return resp(status, viewer_payload(payload, who))
    except HttpError as e:
        return resp(e.status, {"error": e.message, **e.extra})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return resp(409, {"error": "상태가 바뀌었습니다. 새로고침 후 다시 시도해 주세요."})
        traceback.print_exc()
        return resp(500, {"error": "internal error"})
    except Exception:
        traceback.print_exc()
        return resp(500, {"error": "internal error"})
