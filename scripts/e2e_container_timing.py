"""Container cold vs warm creation time against the REAL deployed platform (backlog: Container warm timing).

    python scripts/e2e_container_timing.py

1. environment A while no Container environment exists: the shared stack (VPC, internal ALB, cluster) is created
   too -> cold time
2. environment B while A is READY: only the per-environment stack -> warm time
3. both are deleted (B, then A); the shared stack must be destroyed with the last one (runner HOLDS_SHARED rule)

Each environment's verdict is kept, including SB-PUB-07/findings (the image scan read once at validation, D-192).
Writes build/e2e-container-report.json. Uses the same test-user login as e2e_static.py (web client, not agent).
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_static import REGION, ROOT, Api, ensure_user, tf_outputs, wait_status  # noqa: E402

INPUT = {"app_form": "SERVER_APP", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "downtime_tolerance": "OK",
         "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 100,
         "capabilities": {"realtime_connection": True}}  # a persistent connection -> the always-on container blueprint
IMAGE = {"image": "traefik/whoami:v1.10.3", "env": {"WHOAMI_PORT_NUMBER": "8080"}, "health_path": "/"}


HOLDS_SHARED = {"PROVISIONING", "VALIDATING", "READY", "DEGRADED", "FAILED", "DELETING", "DELETE_FAILED", "REDEPLOYING",
                "DRIFTED", "REPAIRING"}


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table(tf_outputs()["environment_table"])


def shared_stack_exists() -> bool:
    """Whether any Container environment holds the shared stack (read from the platform's own records; the deployer
    user has no elasticloadbalancing read permission, and we do not widen it for a test)."""
    items = _table().scan(ProjectionExpression="env_id, #s, recommendation", ExpressionAttributeNames={"#s": "status"})["Items"]
    def ctr(e):
        rec = e.get("recommendation")
        rec = json.loads(rec) if isinstance(rec, str) else (rec or {})
        return str((rec.get("architecture") or {}).get("candidate_id", "")).startswith("CTR")
    return any(e["status"] in HOLDS_SHARED and ctr(e) for e in items)


def events(env_id: str) -> list[str]:
    item = _table().get_item(Key={"env_id": env_id})["Item"]
    log = item.get("event_log")
    log = json.loads(log) if isinstance(log, str) else (log or [])
    return [e.get("message", "") if isinstance(e, dict) else str(e) for e in log]


def create(api: Api, name: str, report: dict) -> str:
    code, created = api.call("POST", "/environments", {"name": name, "input": INPUT, "image": IMAGE})
    assert code == 201, created
    env_id = created["environment"]["env_id"]
    arch = created["environment"]["recommendation"]["architecture"]["candidate_id"]
    assert arch.startswith("CTR"), arch
    print(f"{name}: {env_id} {arch}, max ${created['environment']['cost_upper_usd']}/month, shared stack before: {shared_stack_exists()}")
    t0 = time.monotonic()
    code, approved = api.call("POST", f"/environments/{env_id}/approve")
    assert code == 202, approved
    env = wait_status(api, env_id, {"READY", "DEGRADED", "FAILED"}, timeout_s=45 * 60)
    secs = int(time.monotonic() - t0)
    findings = next((c for c in env.get("checks") or [] if c["check_id"] == "SB-PUB-07/findings"), None)
    report[name] = {"env_id": env_id, "status": env["status"], "provision_seconds": secs,
                    "failed": (env.get("verdict") or {}).get("failed"), "image_findings": findings}
    print(f"{name}: {env['status']} in {secs // 60}m{secs % 60:02d}s; failed={report[name]['failed']}; image scan={findings}")
    return env_id


def delete(api: Api, env_id: str) -> int:
    t0 = time.monotonic()
    code, d = api.call("DELETE", f"/environments/{env_id}")
    assert code == 202, d
    env = wait_status(api, env_id, {"DELETED", "DELETE_FAILED"}, timeout_s=45 * 60)
    assert env["status"] == "DELETED", env.get("event_log", [])[-3:]
    return int(time.monotonic() - t0)


def main() -> int:
    out = tf_outputs()
    pool = out["user_pool_id"]
    clients = boto3.client("cognito-idp", region_name=REGION).list_user_pool_clients(UserPoolId=pool)["UserPoolClients"]
    client_id = next(c["ClientId"] for c in clients if c["ClientId"] != out.get("agent_client_id"))
    password = "E2e-" + secrets.token_urlsafe(12) + "9a"
    api = Api(out["api_url"], ensure_user(pool, client_id, "e2e-container@example.com", password))
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "shared_stack_at_start": shared_stack_exists()}
    created: list[str] = []
    try:
        created.append(create(api, "cold", report))
        if report["cold"]["status"] != "READY":
            raise AssertionError(f"cold environment is {report['cold']['status']}; not measuring warm")
        created.append(create(api, "warm", report))
    finally:
        for env_id in reversed(created):  # B first, so A is the last holder of the shared stack
            secs = delete(api, env_id)
            report.setdefault("delete_seconds", {})[env_id] = secs
            print(f"deleted {env_id} in {secs // 60}m{secs % 60:02d}s")
        report["shared_stack_after_delete"] = shared_stack_exists()
        if created:
            last = [m for m in events(created[0]) if "공유" in m]
            report["shared_teardown_events"] = last
            print(f"shared-stack events on the last environment: {last}")
        print(f"shared stack after deleting all: {report['shared_stack_after_delete']}")
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "build" / "e2e-container-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    assert not report["shared_stack_after_delete"], "no Container environment may still hold the shared stack"
    assert any("공유 컨테이너 인프라를 삭제했습니다" in m for m in report.get("shared_teardown_events", [])),         "the runner must record destroying the shared stack with the last Container environment"
    print("CONTAINER TIMING E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
