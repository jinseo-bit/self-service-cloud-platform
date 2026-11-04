"""Local development server — runs the real Web UI and the real control-plane API handler
without touching AWS.

    python scripts/local_dev.py            # http://localhost:8000
    python scripts/local_dev.py --plan     # also run a real offline `terraform plan` + PLAN validator

What is REAL here: web/ files, control_plane.api.handler (routing, engine, admission,
artifact validation, DynamoDB record logic), control_plane.worker.mark/fail/deleted,
platform_core validators (ARTIFACT, PLAN with --plan) and verdict().

AWS BOUNDARY — replaced locally (never deployed):
  * DynamoDB / S3 / Step Functions  → moto, in-process, in-memory (lost on exit)
  * Cognito login                   → POST /local/auth issues an UNSIGNED dev token
  * S3 presigned POST upload        → POST /local/upload/<env_id> (same origin)
  * CodeBuild terraform apply       → not executed; a local simulator advances status
  * Publisher Lambda (redeploy)     → not executed; the local site switches to the new revision
  * STATE / HTTP validators         → reported as NOT_APPLICABLE ("로컬 모드"), except a
                                      real HTTP GET of the locally served site (PR-ALL-02)
Outcome injection for UI testing: an environment name containing "fail" gets an injected
security failure, "degraded" an injected reachability failure (both labelled [로컬 시뮬레이션]).
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import email.policy
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
WEB = ROOT / "web"

# Fake, local-only settings. Set BEFORE importing boto3-using modules.
os.environ.update(
    AWS_DEFAULT_REGION="ap-northeast-2", AWS_REGION="ap-northeast-2",
    AWS_ACCESS_KEY_ID="local-dev-not-a-key", AWS_SECRET_ACCESS_KEY="local-dev-not-a-secret",  # noqa: S106 — fake, moto only
    ENV_TABLE="sscp-local-environments", UPLOAD_BUCKET="sscp-local-uploads", LOG_BUCKET="sscp-local-logs",
    TRAIL_NAME="sscp-local-trail",
)
for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
    os.environ.pop(k, None)

from moto import mock_aws  # noqa: E402

MOCK = mock_aws()
MOCK.start()

import boto3  # noqa: E402

from control_plane import api as API  # noqa: E402
from control_plane import worker as WORKER  # noqa: E402
from control_plane.store import Store  # noqa: E402

LOCAL_AGENT_CLIENT = "local-agent"  # /local/auth {"client": "agent"} issues tokens with this audience (D-181)
from platform_core.blueprints import for_env  # noqa: E402
from platform_core.production_ready import checks_for  # noqa: E402
from platform_core.validation.plan import validate_plan  # noqa: E402
from platform_core.validation.result import FAIL, NOT_APPLICABLE, PASS, CheckResult  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000
STEP_DELAY = 2.0
APP_ID = "self-service-cloud-platform"
SW_HELP = ("이 origin에 다른 프로젝트가 등록한 Service Worker가 남아 있습니다. 브라우저가 예전 사이트를 보여줄 수 있어요. "
           "http://127.0.0.1:<port> 로 열거나, Chrome DevTools > Application > Service Workers 에서 해당 origin을 Unregister 하세요.")
RUN_PLAN = False
LOCAL_NOTE = "로컬 모드 — 실제 AWS 배포 후 검사합니다"


def bootstrap_aws() -> None:
    ddb = boto3.client("dynamodb")
    ddb.create_table(
        TableName=os.environ["ENV_TABLE"], BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "env_id", "AttributeType": "S"},
                              {"AttributeName": "owner_sub", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "env_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{"IndexName": "by-owner", "KeySchema": [{"AttributeName": "owner_sub", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}])
    s3 = boto3.client("s3")
    for b in (os.environ["UPLOAD_BUCKET"], os.environ["LOG_BUCKET"]):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
    sfn = boto3.client("stepfunctions")
    definition = json.dumps({"StartAt": "LocalSimulator", "States": {"LocalSimulator": {"Type": "Succeed"}}})
    role = "arn:aws:iam::000000000000:role/local"
    API.PROVISION_SFN = sfn.create_state_machine(name="sscp-provision", definition=definition, roleArn=role)["stateMachineArn"]
    API.DELETE_SFN = sfn.create_state_machine(name="sscp-delete", definition=definition, roleArn=role)["stateMachineArn"]
    API.REDEPLOY_SFN = sfn.create_state_machine(name="sscp-redeploy", definition=definition, roleArn=role)["stateMachineArn"]
    API.REPAIR_SFN = sfn.create_state_machine(name="sscp-repair", definition=definition, roleArn=role)["stateMachineArn"]
    WORKER.UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
    # Day-2 (D-186): the real worker detects, records and closes drift; only its AWS edges are local.
    WORKER.run_checks = lambda env, event: _local_checks(env)
    WORKER._block_endpoint = _local_block
    WORKER._start_repair = lambda env_id, phase: threading.Thread(target=_simulate_repair_plan, args=(env_id,), daemon=True).start()
    API._lambda = _LocalLambda()

    # AWS boundary: presigned S3 POST → same-origin local upload endpoint.
    def local_upload_form(env_id: str, revision: int = 1) -> dict:
        key = API.upload_key(env_id, revision)
        return {"url": f"/local/upload/{env_id}", "fields": {"key": key, "Content-Type": "application/zip"}}

    API.upload_form = local_upload_form
    API.UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
    API.AGENT_CLIENT_ID = LOCAL_AGENT_CLIENT  # tokens from /local/auth {"client": "agent"} act as the agent (D-181)


# ------------------------------------------------------------------ local provisioning simulator

_handled: set[tuple[str, str]] = set()
_plan_dirs: dict[str, Path] = {}
_plan_lock = threading.Lock()


def _offline_plan(env: dict) -> dict:
    """Real terraform plan of the environment's blueprint with fake credentials (no AWS calls)."""
    from tests.validation.conftest import OFFLINE_OVERRIDE, OFFLINE_OVERRIDE_SINGLE, offline_plan
    bp = for_env(env)
    with _plan_lock:
        if bp.key not in _plan_dirs:
            dst = Path(tempfile.mkdtemp(prefix="sscp-local-plan-")) / "infra"
            shutil.copytree(ROOT / "infra", dst, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
            _plan_dirs[bp.key] = dst / "blueprints" / bp.terraform_dir
            (_plan_dirs[bp.key] / "offline_override.tf").write_text(OFFLINE_OVERRIDE_SINGLE if bp.key == "serverless" else OFFLINE_OVERRIDE)
            if bp.shared:
                (dst / "shared" / bp.shared / "offline_override.tf").write_text(OFFLINE_OVERRIDE_SINGLE)
        inp = env["input"]
        if bp.key == "container":
            from tests.validation.conftest import CONTAINER_VARS
            art = env.get("artifact") or {}
            sc = env["recommendation"]["architecture"].get("scaling") or {}
            return offline_plan(_plan_dirs[bp.key], {
                **CONTAINER_VARS, "env_id": env["env_id"], "account_id": "000000000000", "owner": env.get("owner_email") or "local",
                "tier": env["tier"], "expires_at": env.get("expires_at") or "", "alert_email": env.get("owner_email") or "",
                "log_bucket": os.environ["LOG_BUCKET"], "ip_allowlist": list(inp.get("allowlist_cidrs") or []),
                "image_uri": f"000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-{env['env_id']}@sha256:" + "0" * 64,
                "env_boundary_arn": "arn:aws:iam::000000000000:policy/sscp-env-boundary",
                "health_path": art.get("health_path") or "/", "desired_count": sc.get("ctr_min_tasks", 1),
                "capacity_provider": sc.get("ctr_capacity", "FARGATE"),
                "origin_read_timeout_s": sc.get("edge_origin_response_timeout_s", 30),
            }, out=f"{env['env_id']}.bin")
        if bp.key == "serverless":
            art = env.get("artifact") or {}
            return offline_plan(_plan_dirs[bp.key], {
                "env_id": env["env_id"], "account_id": "000000000000", "owner": env.get("owner_email") or "local",
                "tier": env["tier"], "expires_at": env.get("expires_at") or "", "alert_email": env.get("owner_email") or "",
                "image_uri": f"000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-{env['env_id']}@sha256:" + "0" * 64,
                "env_boundary_arn": "arn:aws:iam::000000000000:policy/sscp-env-boundary",
                "health_path": art.get("health_path") or "/",
            }, out=f"{env['env_id']}.bin")
        return offline_plan(_plan_dirs[bp.key], {
            "env_id": env["env_id"], "account_id": "000000000000", "owner": env.get("owner_email") or "local",
            "tier": env["tier"], "expires_at": env.get("expires_at") or "", "log_bucket": os.environ["LOG_BUCKET"],
            "alert_email": env.get("owner_email") or "", "spa_fallback": str(bool(inp.get("spa_fallback"))).lower(),
            "ip_allowlist": list(inp.get("allowlist_cidrs") or []),
        }, out=f"{env['env_id']}.bin")


def _simulate_provision(env_id: str) -> None:
    s = Store()
    try:
        time.sleep(STEP_DELAY)
        env = s.get(env_id)
        bp = for_env(env)
        if RUN_PLAN:
            s.update(env_id, event="[로컬] 실제 terraform plan(오프라인)과 PLAN 검사를 실행합니다.")
            fixed = {r["code"] for r in env["recommendation"].get("reasons") or [] if r["code"].startswith("FIX_")}
            results = validate_plan(_offline_plan(env), ip_allowlist=env["input"].get("allowlist_cidrs") or [], blueprint=bp.key,
                                    fixed_cost_reasons=fixed)
            if bp.shared:
                from tests.validation.conftest import SHARED_CONTAINER_VARS, offline_plan
                shared_dir = _plan_dirs[bp.key].parents[1] / "shared" / bp.shared
                shared = validate_plan(offline_plan(shared_dir, SHARED_CONTAINER_VARS), blueprint=f"{bp.shared}-shared",
                                       fixed_cost_reasons=fixed)
                from runner.main import merge_shared
                results = merge_shared(results, shared)
        else:
            results = [CheckResult(c["id"], "PLAN", c["dimension"], NOT_APPLICABLE, "로컬 모드 — --plan 옵션으로 실제 plan 검사 실행", ())
                       for c in checks_for(bp.candidate_id) if c["layer"] == "PLAN"]
        checks = [r.to_dict() for r in results]
        if any(r.status == FAIL for r in results):
            s.update(env_id, checks=checks, plan_rejected=True, event="PLAN 검증 실패")
            WORKER.fail({"env_id": env_id, "stage": "PROVISION", "error": {"Error": "PlanRejected", "Cause": "local"}})
            return
        s.update(env_id, checks=checks, event="[로컬 시뮬레이션] terraform apply는 실행하지 않습니다(AWS 경계).")
        time.sleep(STEP_DELAY)
        if bp.key == "container":
            s.update(env_id, event="[로컬 시뮬레이션] 공유 인프라(VPC·내부 ALB·클러스터), 이미지 복사(crane), "
                                   "ECS 실행은 하지 않습니다(AWS 경계).")
            url = f"http://{HOST}:{PORT}/local-apps/{env_id}"
            s.update(env_id, outputs={"url": url, "service_name": f"sscp-{env_id}", "cluster_name": "sscp-shared",
                                      "distribution_id": "LOCAL",
                                      "image_uri": f"000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-{env_id}@sha256:local"},
                     event=f"[로컬] 앱 대신 로컬 응답 페이지를 제공합니다: {url}")
        elif bp.key == "serverless":
            s.update(env_id, event="[로컬 시뮬레이션] Lambda용 이미지 빌드(crane)와 ECR push는 실행하지 않습니다(AWS 경계).")
            url = f"http://{HOST}:{PORT}/local-apps/{env_id}"
            s.update(env_id, outputs={"url": url, "function_name": f"sscp-{env_id}", "api_id": "LOCAL",
                                      "image_uri": f"000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-{env_id}@sha256:local"},
                     event=f"[로컬] 앱 대신 로컬 응답 페이지를 제공합니다: {url}")
        else:
            url = f"http://{HOST}:{PORT}/local-sites/{env_id}/"
            s.update(env_id, outputs={"url": url, "bucket_name": f"sscp-{env_id}-000000000000", "distribution_id": "LOCAL",
                                      "distribution_arn": "arn:aws:cloudfront::000000000000:distribution/LOCAL"},
                     event=f"[로컬] 사이트를 로컬 주소로 제공합니다: {url}")
        WORKER.mark({"env_id": env_id, "status": "VALIDATING"})
        time.sleep(STEP_DELAY)
        _simulate_validate(env_id)
    except Exception as e:  # keep the simulator alive; record like the real orchestrator would
        WORKER.fail({"env_id": env_id, "stage": "VALIDATE", "error": {"Error": type(e).__name__, "Cause": str(e)}})


def _simulate_validate(env_id: str) -> None:
    """The worker's own _finish_validation (verdict, D-65 block, revisions, closing a repaired drift) on local checks."""
    s = Store()
    env = s.get(env_id)
    v = WORKER._finish_validation(s, env, *_local_checks(env))
    s.update(env_id, event=f"검증 결과 {v['verdict']} (로컬 시뮬레이션: AWS 상태 검사는 실행하지 않음)")


def _local_checks(env: dict) -> tuple[list[CheckResult], list[CheckResult]]:
    """STATE and HTTP results the local platform can know: a real HTTP check against the local site, injected
    failures (owner names with 'fail'/'degraded'), and drift injected with POST /local/drift/<env_id>."""
    local = []
    bp = for_env(env)
    for c in checks_for(bp.candidate_id):
        if c["layer"] not in ("STATE", "HTTP"):
            continue
        if c["id"] == "PR-ALL-02":
            from platform_core.validation.http import fetch
            r = fetch(env["outputs"]["url"] + ((env.get("artifact") or {}).get("health_path") or "/" if bp.artifact == "IMAGE" else ""))
            local.append(CheckResult("PR-ALL-02", "HTTP", "Reachability", PASS if r.status == 200 else FAIL,
                                     f"[로컬] GET {env['outputs']['url']} -> {r.status}"))
        elif c["id"] != "PR-S-03":  # PR-S-03 runs for real below (HTTP against the local site)
            local.append(CheckResult(c["id"], c["layer"], c["dimension"], NOT_APPLICABLE, LOCAL_NOTE))
    from platform_core.validation.http import check_served_revision
    art = env.get("artifact") or {}
    if bp.key == "static":
        local.append(check_served_revision(base_url=env["outputs"]["url"].rstrip("/"), expected_sha256=art.get("index_sha256"),
                                           revision=art.get("revision"), attempts=3, delay=0.3))
    name = env["name"].lower()
    if "fail" in name:
        local.append(CheckResult("SB-TLS-04", "STATE", "Security", FAIL, "[로컬 시뮬레이션] 주입된 보안 실패", ("SB-TLS-04",)))
    if "degraded" in name:
        local.append(CheckResult("PR-ALL-02", "HTTP", "Reachability", FAIL, "[로컬 시뮬레이션] 주입된 접속 실패"))
    if env["env_id"] in _LOCAL_BLOCKED:  # D-188: the block has the effects it has on AWS, so rechecks see them
        local = [r for r in local if r.check_id not in BLOCKED_FAILS]
        local += [CheckResult(cid, layer, dim, FAIL, "[로컬 시뮬레이션] 접속 차단 중") for cid, (dim, layer) in BLOCKED_FAILS.items()
                  if cid != "PR-ALL-02/enabled" or bp.key != "serverless"]
    for cid in _LOCAL_DRIFT.get(env["env_id"], []):
        dim, layer, _, _ = DRIFT_CATALOG[cid]
        local = [r for r in local if r.check_id != cid]
        local.append(CheckResult(cid, layer, dim, FAIL, f"[로컬 시뮬레이션] 콘솔에서 바뀐 설정: {cid}"))
    return [r for r in local if r.layer == "STATE"], [r for r in local if r.layer != "STATE"]


# ------------------------------------------------------------------ Day-2 drift (D-186), local AWS edges

# check id -> (dimension, layer, Terraform address that brings it back, CloudTrail event a person's change makes)
DRIFT_CATALOG = {
    "SB-PUB-01/policy": ("Security", "STATE", "aws_s3_bucket_policy.origin", "PutBucketPolicy"),
    "SB-LOG-04": ("Observability", "STATE", "aws_cloudwatch_log_group.fn", "PutRetentionPolicy"),
    "SB-PUB-03": ("Security", "STATE", "aws_security_group.task", "AuthorizeSecurityGroupIngress"),
}
DRIFT_DEFAULT = {"static": "SB-PUB-01/policy", "serverless": "SB-LOG-04", "container": "SB-PUB-03"}
_LOCAL_DRIFT: dict[str, list[str]] = {}
# what a blocked endpoint (D-65) fails on AWS: CloudFront disabled (Lambda concurrency 0 has no CDN check) and every probe
BLOCKED_FAILS = {"PR-ALL-02/enabled": ("Reachability", "STATE"), "PR-ALL-02": ("Reachability", "HTTP"),
                 "PR-S-03": ("Operability", "HTTP")}
_LOCAL_BLOCKED: set[str] = set()


def _local_block(env: dict) -> None:
    _LOCAL_BLOCKED.add(env["env_id"])
    Store().update(env["env_id"], event="[로컬 시뮬레이션] 접속을 차단했습니다(실제로는 CloudFront 비활성화 또는 Lambda 동시성 0).")


class _LocalLambda:
    """POST /environments/{id}/recheck invokes the worker asynchronously; here, a thread runs the real handler."""

    def invoke(self, FunctionName=None, InvocationType=None, Payload=b"{}"):  # noqa: N803 - boto3 argument names
        threading.Thread(target=WORKER.handler, args=(json.loads(Payload), None), daemon=True).start()
        return {"StatusCode": 202}


def inject_drift(env_id: str, check_id: str | None = None) -> dict:
    """A person changes a setting in the console: the local 'AWS' changes and CloudTrail emits a write event."""
    env = Store().get(env_id)
    cid = check_id or DRIFT_DEFAULT[for_env(env).key]
    _LOCAL_DRIFT.setdefault(env_id, []).append(cid)
    ident = next((v for v in (env.get("outputs") or {}).values() if isinstance(v, str) and env_id in v), env_id)
    detail = {"eventName": DRIFT_CATALOG[cid][3], "eventTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "userIdentity": {"arn": "arn:aws:iam::000000000000:user/local-console-user"},
              "sourceIPAddress": "203.0.113.10", "requestParameters": {"resource": ident}}
    return WORKER.handler({"detail-type": "AWS API Call via CloudTrail", "detail": detail}, None)


def _simulate_repair_plan(env_id: str) -> None:
    time.sleep(STEP_DELAY)
    changes = [{"address": DRIFT_CATALOG[c][2], "actions": ["update"]} for c in _LOCAL_DRIFT.get(env_id, [])]
    Store().update(env_id, repair_plan={"created_at": WORKER.now(), "changes": len(changes), "replaces": [], "passed": True,
                                        "stacks": [{"stack": "env", "key": "local", "changes": changes, "failed_checks": []}]},
                   event=f"[로컬 시뮬레이션] 수정안을 만들었습니다: 리소스 {len(changes)}개를 승인된 구성으로 되돌립니다 "
                         "(terraform plan은 실행하지 않음).")


def _simulate_repair_apply(env_id: str) -> None:
    time.sleep(STEP_DELAY)
    _LOCAL_DRIFT.pop(env_id, None)
    _LOCAL_BLOCKED.discard(env_id)  # the approved plan turns the endpoint back on
    Store().update(env_id, event="[로컬 시뮬레이션] 승인된 수정안을 적용했습니다(terraform apply는 실행하지 않음). 다시 검증합니다.")
    WORKER.mark({"env_id": env_id, "status": "VALIDATING"})
    time.sleep(STEP_DELAY)
    _simulate_validate(env_id)


def _simulate_redeploy(env_id: str) -> None:
    """AWS boundary: the publisher Lambda (S3 sync + CloudFront invalidation) is not executed locally;
    the locally served site switches to the new revision's bundle instead."""
    try:
        time.sleep(STEP_DELAY)
        rev = Store().get(env_id)["artifact"]["revision"]
        Store().update(env_id, event=(f"[로컬 시뮬레이션] rev-{rev} 파일로 교체했습니다. "
                                      "S3 업로드·CDN invalidation은 실행하지 않습니다(AWS 경계)."))
        WORKER.mark({"env_id": env_id, "status": "VALIDATING"})
        time.sleep(STEP_DELAY)
        _simulate_validate(env_id)
    except Exception as e:
        WORKER.fail({"env_id": env_id, "stage": "REDEPLOY", "error": {"Error": type(e).__name__, "Cause": str(e)}})


def _simulate_delete(env_id: str) -> None:
    time.sleep(STEP_DELAY)
    Store().update(env_id, event="[로컬 시뮬레이션] terraform destroy는 실행하지 않습니다(AWS 경계).")
    time.sleep(STEP_DELAY)
    WORKER.deleted({"env_id": env_id})


_active: set[str] = set()  # env_ids with a simulation in flight
_active_lock = threading.Lock()
STATE_MACHINE = {"PROVISIONING": "sscp-provision", "REDEPLOYING": "sscp-redeploy", "DELETING": "sscp-delete",
                 "REPAIRING": "sscp-repair"}


def _run_once(env_id: str, fn) -> None:
    try:
        fn(env_id)
    finally:
        with _active_lock:
            _active.discard(env_id)


def simulator_loop() -> None:
    """Mirrors one Step Functions execution per start: an execution ARN is simulated exactly once,
    and never two simulations for the same environment at the same time."""
    s = Store()
    while True:
        try:
            for env in s.list_all():
                fn = SIMULATORS.get(env["status"])
                arn = env.get("execution_arn")
                if fn is None or not arn or arn in _handled:
                    continue
                if f":execution:{STATE_MACHINE[env['status']]}:" not in arn:
                    continue  # API writes status first, execution ARN second: wait for the ARN of *this* run
                with _active_lock:
                    if env["env_id"] in _active:
                        continue
                    _active.add(env["env_id"])
                _handled.add(arn)
                threading.Thread(target=_run_once, args=(env["env_id"], fn), daemon=True).start()
        except Exception as e:
            print("[simulator]", e)
        time.sleep(0.5)


SIMULATORS = {"PROVISIONING": _simulate_provision, "REDEPLOYING": _simulate_redeploy, "DELETING": _simulate_delete,
              "REPAIRING": _simulate_repair_apply}


# ------------------------------------------------------------------ HTTP server

ROUTE_PATTERNS = [(re.compile("^" + re.sub(r"\{env_id\}", r"(?P<env_id>env-[a-z0-9]{8})", k.split(" ", 1)[1]) + "$"), k)
                  for k in API.ROUTES]


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def _claims(token: str) -> dict | None:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return None


def _parse_multipart(content_type: str, body: bytes) -> dict:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = part.get_payload(decode=True)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "sscp-local"

    def log_message(self, fmt, *args):
        if "/api/" in self.path or "/local/" in self.path:
            sys.stderr.write("[local] " + fmt % args + "\n")

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("X-SSCP-App", APP_ID)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_HEAD(self):
        self._route("GET")

    def _route(self, method: str):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if self.headers.get("Service-Worker") == "script":
            # Browsers send this header only when (re)fetching a registered Service Worker script.
            # This app never registers one, so it belongs to another project that used this origin.
            sys.stderr.write(f"[local] WARNING: browser requested Service Worker script {path}. {SW_HELP}\n")
            return self._send(404, b"no service worker in this app", "text/plain")
        if path == "/__sscp/health":
            return self._json(200, {"app": APP_ID, "static_root": str(WEB.resolve()), "aws_called": False})
        if path == "/config.js":
            cfg = {"apiUrl": "/api", "region": "ap-northeast-2", "clientId": "local", "authMode": "local", "localMode": True}
            return self._send(200, f"window.SSCP_CONFIG = {json.dumps(cfg)};\n".encode(), "application/javascript; charset=utf-8")
        if path == "/local/auth" and method == "POST":
            b = json.loads(self._body() or b"{}")
            email_ = (b.get("email") or "").strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_):
                return self._json(400, {"error": "올바른 이메일을 입력해 주세요."})
            claims = {"sub": "local-" + email_, "email": email_, "cognito:groups": "[admin]" if b.get("admin") else "[]"}
            claims["aud"] = LOCAL_AGENT_CLIENT if b.get("client") == "agent" else "local"
            token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}.local-unsigned"
            return self._json(200, {"IdToken": token, "AccessToken": token})
        m = re.fullmatch(r"/local/drift/(env-[a-z0-9]{8})", path)  # D-186: "someone changed it in the console"
        if m and method == "POST":
            b = json.loads(self._body() or b"{}")
            return self._json(200, inject_drift(m.group(1), b.get("check")))
        m = re.fullmatch(r"/local/upload/(env-[a-z0-9]{8})", path)
        if m and method == "POST":
            fields = _parse_multipart(self.headers.get("Content-Type", ""), self._body())
            data = fields.get("file") or b""
            key = (fields.get("key") or b"").decode()
            if not data or len(data) > 50 * 1024 * 1024 or not key.startswith(API.upload_prefix(m.group(1))):
                return self._send(400, b"bad upload", "text/plain")
            boto3.client("s3").put_object(Bucket=os.environ["UPLOAD_BUCKET"], Key=key, Body=data)
            return self._send(204, b"")
        m = re.fullmatch(r"/local-sites/(env-[a-z0-9]{8})/(.*)", path)
        if m:
            return self._serve_site(m.group(1), m.group(2))
        m = re.fullmatch(r"/local-apps/(env-[a-z0-9]{8})(/.*)?", path)
        if m:  # AWS boundary: stands in for Lambda + API Gateway running the user's image
            env = Store().get(m.group(1))
            if not env or env["status"] in ("DELETED", "DELETING"):
                return self._send(404, b"not found", "text/plain")
            art = env.get("artifact") or {}
            page = (f"<!doctype html><title>local app</title><h1>sscp-local-app</h1><p>{art.get('image', '')}</p>"
                    f"<p>path={m.group(2) or '/'}</p>").encode()
            return self._send(200, page, "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self._api(method, path[4:], query)
        return self._static(path)

    def _api(self, method: str, path: str, query: str):
        route_key, params = None, None
        for rx, key in ROUTE_PATTERNS:
            mm = rx.match(path)
            if mm and key.startswith(method + " "):
                route_key, params = key, mm.groupdict() or None
                break
        if route_key is None:
            return self._json(404, {"error": "not found"})
        claims = _claims(self.headers.get("Authorization", ""))
        if not claims:
            return self._json(401, {"message": "Unauthorized"})
        qs = dict(p.split("=", 1) for p in query.split("&") if "=" in p) or None
        event = {"routeKey": route_key, "pathParameters": params, "queryStringParameters": qs,
                 "body": self._body().decode() or None,
                 "requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        r = API.handler(event, None)
        self._send(r["statusCode"], r["body"].encode(), r["headers"]["content-type"])

    def _serve_site(self, env_id: str, rel: str):
        env = Store().get(env_id)
        if not env or env["status"] not in ("READY", "DEGRADED", "VALIDATING", "REDEPLOYING"):
            return self._send(404, b"not deployed", "text/plain")
        obj = boto3.client("s3").get_object(Bucket=os.environ["UPLOAD_BUCKET"], Key=env["artifact"]["key"])
        root = (env.get("artifact") or {}).get("root", "")
        with zipfile.ZipFile(io.BytesIO(obj["Body"].read())) as z:
            name = root + (rel or "index.html")
            if name not in z.namelist():
                if env["input"].get("spa_fallback"):
                    name = root + "index.html"
                else:
                    return self._send(404, b"not found", "text/plain")
            data = z.read(name)
        self._send(200, data, mimetypes.guess_type(name)[0] or "application/octet-stream")

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB.resolve() not in f.parents and f != WEB.resolve() or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuse to share a port. HTTPServer enables SO_REUSEADDR, which on Windows lets a second process
    bind a port that another program already serves — the browser may then reach the other program."""

    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(port: int, step_delay: float = 2.0, run_plan: bool = False) -> ThreadingHTTPServer:
    global PORT, STEP_DELAY, RUN_PLAN
    PORT, STEP_DELAY, RUN_PLAN = port, step_delay, run_plan
    srv = ExclusiveHTTPServer((HOST, port), Handler)  # bind first: fail fast if the port is taken
    bootstrap_aws()
    threading.Thread(target=simulator_loop, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--plan", action="store_true", help="run a real offline terraform plan + PLAN validator per environment")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds per simulated provisioning step")
    a = ap.parse_args()
    try:
        serve(a.port, a.delay, a.plan)
    except OSError as e:
        print(f"[local] port {a.port} is already in use ({e}). Stop the other program or run: "
              f"python scripts/local_dev.py --port 8081", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"Local UI:    http://{HOST}:{a.port}/   (open this URL)")
    print(f"Static root: {WEB.resolve()}")
    print(f"Health:      http://{HOST}:{a.port}/__sscp/health")
    print("AWS is NOT called — see the docstring of scripts/local_dev.py for the simulated boundary.")
    print(f"Note: http://localhost:{a.port} is a different browser origin; if it shows another site, a Service Worker "
          "from an old project is registered there (see README).", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
