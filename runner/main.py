"""Terraform runner executed inside CodeBuild (D-102).

ACTION=apply : init → plan → PLAN validator (blocks on FAIL) → apply → publish site → outputs
ACTION=destroy: init → destroy
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

from control_plane.store import Store
from platform_core.publish import publish_bundle
from platform_core.validation.plan import validate_plan
from platform_core.validation.result import FAIL

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_DIR = {"NONE+NONE": ROOT / "infra" / "blueprints" / "static"}


def log(msg: str) -> None:
    print(f"[runner] {msg}", flush=True)


def tf(cwd: Path, *args: str, capture: bool = False) -> str:
    log("terraform " + " ".join(args))
    p = subprocess.run(["terraform", *args, "-no-color"], cwd=cwd, text=True, encoding="utf-8",
                       capture_output=capture, env={**os.environ, "TF_IN_AUTOMATION": "1"})
    if p.returncode != 0:
        if capture:
            print(p.stdout, p.stderr)
        raise SystemExit(f"terraform {args[0]} failed with exit code {p.returncode}")
    return p.stdout if capture else ""


def tfvars(env: dict) -> dict:
    inp = env["input"]
    return {
        "env_id": env["env_id"],
        "account_id": os.environ["ACCOUNT_ID"],
        "owner": env.get("owner_email") or env["owner_sub"],
        "tier": env["tier"],
        "expires_at": env.get("expires_at") or "",
        "log_bucket": os.environ["LOG_BUCKET"],
        "alert_email": env.get("owner_email") or "",
        "spa_fallback": bool(inp.get("spa_fallback")),
        "ip_allowlist": list(inp.get("allowlist_cidrs") or []),
    }


def init(workdir: Path, env_id: str) -> None:
    tf(workdir, "init", "-input=false", "-reconfigure",
       f"-backend-config=bucket={os.environ['STATE_BUCKET']}",
       f"-backend-config=key=envs/{env_id}/terraform.tfstate",
       f"-backend-config=region={os.environ['AWS_REGION']}",
       "-backend-config=encrypt=true", "-backend-config=use_lockfile=true")


def publish_site(env: dict, outputs: dict) -> dict:
    art = env["artifact"]
    return publish_bundle(s3=boto3.client("s3"), cloudfront=boto3.client("cloudfront"),
                          upload_bucket=os.environ["UPLOAD_BUCKET"], artifact_key=art["key"], root=art.get("root", ""),
                          site_bucket=outputs["bucket_name"], distribution_id=outputs["distribution_id"],
                          reference=f"{env['env_id']}-rev-{art.get('revision', 1)}")


def apply(store: Store, env: dict, workdir: Path) -> None:
    init(workdir, env["env_id"])
    tf(workdir, "plan", "-input=false", "-out=plan.bin", "-var-file=env.auto.tfvars.json")
    plan = json.loads(tf(workdir, "show", "-json", "plan.bin", capture=True))
    results = validate_plan(plan, ip_allowlist=tfvars(env)["ip_allowlist"])
    checks = [r.to_dict() for r in results]
    failed = [r for r in results if r.status == FAIL]
    for r in results:
        log(f"PLAN {r.status:15} {r.check_id:18} {r.detail}")
    if failed:
        store.update(env["env_id"], checks=checks, plan_rejected=True,
                     event=f"PLAN 검증 실패: {', '.join(r.check_id for r in failed)}")
        raise SystemExit(2)
    store.update(env["env_id"], checks=checks, event="PLAN 검증을 통과해 리소스를 생성합니다.")

    tf(workdir, "apply", "-input=false", "-auto-approve", "plan.bin")
    raw = json.loads(tf(workdir, "output", "-json", capture=True))
    outputs = {k: v["value"] for k, v in raw.items()}
    outputs["distribution_arn"] = f"arn:aws:cloudfront::{os.environ['ACCOUNT_ID']}:distribution/{outputs['distribution_id']}"
    store.update(env["env_id"], outputs=outputs, event=f"리소스를 생성했습니다: {outputs['url']}")
    r = publish_site(env, outputs)
    store.update(env["env_id"], event=f"사이트 파일 {r['uploaded']}개를 배포했습니다 (rev-{env['artifact'].get('revision', 1)}).")


def destroy(store: Store, env: dict, workdir: Path) -> None:
    init(workdir, env["env_id"])
    tf(workdir, "destroy", "-input=false", "-auto-approve", "-var-file=env.auto.tfvars.json")
    store.update(env["env_id"], event="terraform destroy가 완료되었습니다.")


def main() -> None:
    env_id, action = os.environ["ENV_ID"], os.environ["ACTION"]
    store = Store()
    env = store.get(env_id)
    if env is None:
        raise SystemExit(f"unknown environment {env_id}")
    candidate = env["recommendation"]["architecture"]["candidate_id"]
    workdir = BLUEPRINT_DIR.get(candidate)
    if workdir is None:
        raise SystemExit(f"blueprint {candidate} is not provisionable")
    (workdir / "env.auto.tfvars.json").write_text(json.dumps(tfvars(env)))
    {"apply": apply, "destroy": destroy}[action](store, env, workdir)


if __name__ == "__main__":
    sys.exit(main())
