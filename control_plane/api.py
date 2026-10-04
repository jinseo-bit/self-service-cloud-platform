"""HTTP API Lambda (API Gateway HTTP API, payload v2, Cognito JWT authorizer)."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import traceback

import boto3
from botocore.exceptions import ClientError

from platform_core import baseline, templates
from platform_core.engine import InputError, RawInput, recommend
from platform_core.engine import constants as C
from platform_core.engine.admission import admit
from platform_core.presets import PRESETS
from platform_core.pricing import PriceTable
from platform_core.production_ready import CHECK_TITLES, checks_for
from platform_core.validation.artifact import MAX_BUNDLE_BYTES, bundle_summary, index_sha256, validate_static_bundle

from .store import Store, new_env_id, now

PRICES = PriceTable.load()
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
PROVISION_SFN = os.environ.get("PROVISION_SFN_ARN", "")
DELETE_SFN = os.environ.get("DELETE_SFN_ARN", "")
REDEPLOY_SFN = os.environ.get("REDEPLOY_SFN_ARN", "")

_s3 = boto3.client("s3")
_sfn = boto3.client("stepfunctions")
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


def caller(event) -> dict:
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    groups = claims.get("cognito:groups", "")
    if isinstance(groups, str):
        groups = [g for g in groups.strip("[]").replace(",", " ").split() if g]
    return {"sub": claims["sub"], "email": claims.get("email", ""), "admin": "admin" in groups}


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
    return rec


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
    if b.get("budget_override"):
        # D-158: only an explicit confirmation of a SOFT budget excess is recorded; hard guards still apply at approval.
        if rec.get("status") != "OVER_BUDGET":
            raise HttpError(400, "예산을 넘지 않는 구성에는 예산 초과 승인이 필요 없습니다.")
        override = {"confirmed": True, "budget_usd": rec["cost"]["budget_usd"], "cost_rep_usd": rec["cost"]["rep_usd"],
                    "cost_upper_usd": rec["cost"]["upper_usd"],
                    "excess_upper_usd": rec["cost"]["upper_usd"] - rec["cost"]["budget_usd"],
                    "confirmed_by": who["email"] or who["sub"], "at": now()}
    env_id = new_env_id()
    env = {
        "env_id": env_id, "name": name, "owner_sub": who["sub"], "owner_email": who["email"],
        "status": "DRAFT", "tier": inp["tier"], "created_at": now(), "updated_at": now(),
        "input": inp, "recommendation": rec, "origin": rec["origin"],
        "cost_upper_usd": int(rec.get("cost", {}).get("upper_usd") or 0),
        "budget_override": override,
        "event_log": [{"at": now(), "status": "DRAFT", "message": "추천 결과와 함께 환경 초안을 만들었습니다."}]
        + ([{"at": now(), "status": "DRAFT", "message": f"예산(월 ${override['budget_usd']})을 넘는 것을 확인하고 진행합니다 "
             f"(최대 월 ${override['cost_upper_usd']})."}] if override else []),
    }
    store().put(env)
    return 201, {"environment": public_view(env), "upload": upload_form(env_id, 1) if rec.get("provisionable") else None}


def post_upload_url(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    if env["status"] == "DRAFT":
        return 200, {"upload": upload_form(env["env_id"], 1), "revision": 1}
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
    if st not in ("READY", "DEGRADED", "FAILED", "DELETE_FAILED"):
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
    if env["recommendation"]["architecture"]["candidate_id"] != "NONE+NONE":
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
                             event="다시 시도합니다.")
    store().update(env["env_id"], execution_arn=_start(updated, PROVISION_SFN, "retry"))
    return 202, {"environment": public_view(updated)}


def post_extend(event, who):
    env = load_env(event["pathParameters"]["env_id"], who)
    if env["tier"] != "DEV" or not env.get("expires_at") or env["status"] not in ("READY", "DEGRADED"):
        raise HttpError(409, "개발 환경(READY/DEGRADED)만 연장할 수 있습니다.")
    base = max(dt.datetime.now(dt.UTC), dt.datetime.fromisoformat(env["expires_at"].replace("Z", "+00:00")))
    limit = dt.datetime.now(dt.UTC) + dt.timedelta(days=4 * C.TTL_DAYS_DEV)
    new = min(base + dt.timedelta(days=C.TTL_DAYS_DEV), limit).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    updated = store().update(env["env_id"], expires_at=new, event=f"만료일을 {new}로 연장했습니다.")
    return 200, {"environment": public_view(updated)}


def list_environments(event, who):
    qs = event.get("queryStringParameters") or {}
    envs = store().list_all() if (who["admin"] and qs.get("all") == "1") else store().list_owner(who["sub"])
    envs.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    slim = [{k: e.get(k) for k in ("env_id", "name", "status", "tier", "created_at", "expires_at", "cost_upper_usd",
                                   "owner_email")} | {"url": (e.get("outputs") or {}).get("url"),
                                                      "candidate_id": e["recommendation"].get("architecture", {}).get("candidate_id")}
            for e in envs if e["status"] != "DELETED" or qs.get("deleted") == "1"]
    return 200, {"environments": slim}


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
    "DELETE /environments/{env_id}": delete_environment,
}


def handler(event, context):
    route = ROUTES.get(event.get("routeKey"))
    if route is None:
        return resp(404, {"error": "not found"})
    try:
        status, payload = route(event, caller(event))
        return resp(status, payload)
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
