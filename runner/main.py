"""Terraform runner executed inside CodeBuild (D-102).

ACTION=apply : init → plan → PLAN validator (blocks on FAIL) → apply → publish site → outputs          (Static)
               init → plan → PLAN validator → create ECR → derived image (LWA) → plan → PLAN validator → apply (Serverless)
               shared stack (plan → PLAN validator → apply) → same as Serverless with an image copy      (Container)
ACTION=destroy: init → destroy (Container: then the shared stack too, when no other Container environment uses it)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

from control_plane.store import Store, now
from platform_core.blueprints import for_env
from platform_core.publish import publish_bundle
from platform_core.validation.plan import validate_plan
from platform_core.validation.result import FAIL, NOT_APPLICABLE

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_IMAGE = os.environ.get("LWA_IMAGE", "public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4")
ECR_ONLY = {
    "serverless": ["-target=aws_ecr_repository.app", "-target=aws_ecr_repository_policy.app", "-target=aws_ecr_lifecycle_policy.app"],
    "container": ["-target=aws_ecr_repository.app", "-target=aws_ecr_lifecycle_policy.app"],
}
# Statuses of environments that may still hold resources on the shared stack (refcount, recommendation-engine §12).
HOLDS_SHARED = {"PROVISIONING", "VALIDATING", "READY", "DEGRADED", "FAILED", "DELETING", "DELETE_FAILED", "REDEPLOYING",
                "DRIFTED", "REPAIRING"}  # D-186: a drifted or repairing container env still runs on the shared stack
SHARED_LOCK_TIMEOUT = "-lock-timeout=20m"  # environments created at the same time wait for each other
CLOUDFRONT_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"
# Outputs of infra/shared/container that every Container environment receives as variables.
SHARED_VARS = {"vpc_id": "", "public_subnet_ids": [], "alb_security_group_id": "", "alb_dns_name": "", "alb_arn_suffix": "",
               "listener_arn": "", "cluster_name": "", "vpc_origin_id": ""}


def log(msg: str) -> None:
    print(f"[runner] {msg}", flush=True)


# init/plan/show fail before anything is created: the worker reports them as TERRAFORM_PREPARE, not PROVISION
PREPARE_COMMANDS = ("init", "plan", "show")


class TerraformFailed(SystemExit):
    """A terraform command exited non-zero; `detail` keeps its error output for the admin view and the logs."""

    def __init__(self, command: str, code: int, detail: str):
        super().__init__(f"terraform {command} failed with exit code {code}")
        self.command, self.detail = command, detail


def tf(cwd: Path, *args: str, capture: bool = False) -> str:
    log("terraform " + " ".join(args))
    p = subprocess.run(["terraform", *args, "-no-color"], cwd=cwd, text=True, encoding="utf-8",
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE,
                       env={**os.environ, "TF_IN_AUTOMATION": "1"})
    if p.stderr:
        sys.stderr.write(p.stderr)  # the build log stays complete
    if p.returncode != 0:
        if capture:
            print(p.stdout)
        raise TerraformFailed(args[0], p.returncode, p.stderr or "")
    return p.stdout if capture else ""


def listener_priority(env_id: str) -> int:
    """A stable rule priority on the shared listener (1000-49999); a collision fails apply instead of shadowing."""
    return int(hashlib.sha256(env_id.encode()).hexdigest(), 16) % 49000 + 1000


def tfvars(env: dict, image_uri: str | None = None) -> dict:
    inp = env["input"]
    if for_env(env).key == "container":
        art = env.get("artifact") or {}
        sc = (env.get("recommendation") or {}).get("architecture", {}).get("scaling") or {}
        shared = env.get("shared") or {}
        return {
            "env_id": env["env_id"],
            "account_id": os.environ["ACCOUNT_ID"],
            "owner": env.get("owner_email") or env["owner_sub"],
            "tier": env["tier"],
            "expires_at": env.get("expires_at") or "",
            "region": os.environ.get("AWS_REGION", "ap-northeast-2"),
            "log_bucket": os.environ["LOG_BUCKET"],
            "alert_email": env.get("owner_email") or "",
            "ip_allowlist": list(inp.get("allowlist_cidrs") or []),
            "image_uri": image_uri or (env.get("outputs") or {}).get("image_uri") or f"{repository_url(env)}:sscp-plan-check",
            "env_vars": art.get("env") or {},
            "health_path": art.get("health_path") or "/",
            "task_cpu": round(sc.get("ctr_task_vcpu", 0.25) * 1024),
            "task_memory": round(sc.get("ctr_task_memory_gb", 0.5) * 1024),
            "desired_count": sc.get("ctr_min_tasks", 1),
            "capacity_provider": sc.get("ctr_capacity", "FARGATE"),
            "origin_read_timeout_s": sc.get("edge_origin_response_timeout_s", 30),
            "listener_priority": listener_priority(env["env_id"]),
            "env_boundary_arn": os.environ["ENV_BOUNDARY_ARN"],
            **{k: shared.get(k, default) for k, default in SHARED_VARS.items()},
        }
    if for_env(env).key == "serverless":
        art = env.get("artifact") or {}
        return {
            "env_id": env["env_id"],
            "account_id": os.environ["ACCOUNT_ID"],
            "owner": env.get("owner_email") or env["owner_sub"],
            "tier": env["tier"],
            "expires_at": env.get("expires_at") or "",
            "region": os.environ.get("AWS_REGION", "ap-northeast-2"),
            "alert_email": env.get("owner_email") or "",
            "image_uri": image_uri or (env.get("outputs") or {}).get("image_uri") or f"{repository_url(env)}:sscp-plan-check",
            "env_vars": art.get("env") or {},
            "health_path": art.get("health_path") or "/",
            "env_boundary_arn": os.environ["ENV_BOUNDARY_ARN"],
        }
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


def repository_url(env: dict) -> str:
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    return f"{os.environ['ACCOUNT_ID']}.dkr.ecr.{region}.amazonaws.com/sscp-{env['env_id']}"


def write_vars(workdir: Path, env: dict, image_uri: str | None = None) -> None:
    (workdir / "env.auto.tfvars.json").write_text(json.dumps(tfvars(env, image_uri)))


def fixed_cost_reasons(env: dict) -> set[str]:
    """FIX_* reason codes of the approved recommendation: the only fixed costs a plan may contain (§9.6)."""
    return {r["code"] for r in (env.get("recommendation") or {}).get("reasons") or [] if r.get("code", "").startswith("FIX_")}


def merge_shared(results: list, shared: list) -> list:
    """Checks the environment plan cannot see (the shared ALB, SB-PUB-02/04) take the shared plan's result instead of N/A."""
    from dataclasses import replace
    by_id = {r.check_id: r for r in shared if r.status != NOT_APPLICABLE}
    return [replace(by_id[r.check_id], detail=f"shared: {by_id[r.check_id].detail}")
            if r.status == NOT_APPLICABLE and r.check_id in by_id else r for r in results]


def plan_and_validate(store: Store, env: dict, workdir: Path, blueprint: str, out: str = "plan.bin",
                      phase: str = "", shared_results: list | None = None) -> None:
    tf(workdir, "plan", "-input=false", f"-out={out}", "-var-file=env.auto.tfvars.json")
    plan = json.loads(tf(workdir, "show", "-json", out, capture=True))
    results = validate_plan(plan, ip_allowlist=list(env["input"].get("allowlist_cidrs") or []), blueprint=blueprint,
                            fixed_cost_reasons=fixed_cost_reasons(env))
    results = merge_shared(results, shared_results or [])
    for r in results:
        log(f"PLAN {r.status:15} {r.check_id:18} {r.detail}")
    checks = [r.to_dict() for r in results]
    failed = [r for r in results if r.status == FAIL]
    if failed:
        store.update(env["env_id"], checks=checks, plan_rejected=True,
                     event=f"PLAN 검증{phase} 실패: {', '.join(r.check_id for r in failed)}")
        raise SystemExit(2)
    store.update(env["env_id"], checks=checks, event=f"PLAN 검증{phase}을 통과했습니다.")


def init(workdir: Path, env_id: str, key: str | None = None) -> None:
    tf(workdir, "init", "-input=false", "-reconfigure",
       f"-backend-config=bucket={os.environ['STATE_BUCKET']}",
       f"-backend-config=key={key or f'envs/{env_id}/terraform.tfstate'}",
       f"-backend-config=region={os.environ['AWS_REGION']}",
       "-backend-config=encrypt=true", "-backend-config=use_lockfile=true")


def publish_site(env: dict, outputs: dict) -> dict:
    art = env["artifact"]
    return publish_bundle(s3=boto3.client("s3"), cloudfront=boto3.client("cloudfront"),
                          upload_bucket=os.environ["UPLOAD_BUCKET"], artifact_key=art["key"], root=art.get("root", ""),
                          site_bucket=outputs["bucket_name"], distribution_id=outputs["distribution_id"],
                          reference=f"{env['env_id']}-rev-{art.get('revision', 1)}")


def apply(store: Store, env: dict, workdir: Path) -> None:
    if for_env(env).key == "serverless":
        return apply_serverless(store, env, workdir)
    if for_env(env).key == "container":
        return apply_container(store, env, workdir)
    init(workdir, env["env_id"])
    write_vars(workdir, env)
    plan_and_validate(store, env, workdir, "static")
    store.update(env["env_id"], event="PLAN 검증을 통과해 리소스를 생성합니다.")

    tf(workdir, "apply", "-input=false", "-auto-approve", "plan.bin")
    raw = json.loads(tf(workdir, "output", "-json", capture=True))
    outputs = {k: v["value"] for k, v in raw.items()}
    outputs["distribution_arn"] = f"arn:aws:cloudfront::{os.environ['ACCOUNT_ID']}:distribution/{outputs['distribution_id']}"
    store.update(env["env_id"], outputs=outputs, event=f"리소스를 생성했습니다: {outputs['url']}")
    r = publish_site(env, outputs)
    store.update(env["env_id"], event=f"사이트 파일 {r['uploaded']}개를 배포했습니다 (rev-{env['artifact'].get('revision', 1)}).")


def apply_serverless(store: Store, env: dict, workdir: Path) -> None:
    """The whole configuration is validated before anything exists; then only the ECR repository is created,
    the derived image is pushed, and the final plan (pinned to the image digest) is validated again and applied."""
    from runner.image_build import ImageBuildError, build_derived_image

    env_id = env["env_id"]
    init(workdir, env_id)
    write_vars(workdir, env)
    plan_and_validate(store, env, workdir, "serverless", out="precheck.bin", phase="(1/2 · 전체 구성, 이미지 확정 전)")
    store.update(env_id, event="이미지 저장소를 만들고 Lambda용 이미지를 준비합니다.")
    tf(workdir, "apply", "-input=false", "-auto-approve", "-var-file=env.auto.tfvars.json", *ECR_ONLY["serverless"])

    art = env["artifact"]
    tag = f"rev{art.get('revision', 1)}-{int(time.time())}"  # tags are immutable; a retry gets a new one
    try:
        image_uri = build_derived_image(source=art["image"], repository_url=repository_url(env), tag=tag,
                                        adapter_image=ADAPTER_IMAGE, ecr=boto3.client("ecr"))
    except ImageBuildError as e:
        store.update(env_id, image_build_failed=True, image_build_error=str(e), event=f"Lambda용 이미지를 만들지 못했습니다: {e}")
        raise SystemExit(3) from None
    store.update(env_id, event=f"Lambda용 이미지를 만들었습니다 ({image_uri.rsplit('@', 1)[-1][:19]}…). 원본 이미지는 바꾸지 않았습니다.")

    write_vars(workdir, env, image_uri)
    plan_and_validate(store, env, workdir, "serverless", phase="(2/2 · 최종 구성, 이미지 digest 고정)")
    tf(workdir, "apply", "-input=false", "-auto-approve", "plan.bin")
    raw = json.loads(tf(workdir, "output", "-json", capture=True))
    outputs = {k: v["value"] for k, v in raw.items()}
    store.update(env_id, outputs=outputs, event=f"리소스를 생성했습니다: {outputs['url']}")


# ---------------------------------------------------------------- shared stack (Container)

def shared_dir(name: str) -> Path:
    return ROOT / "infra" / "shared" / name


def shared_tfvars() -> dict:
    ec2 = boto3.client("ec2")
    lists = ec2.describe_managed_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": [CLOUDFRONT_PREFIX_LIST]}])
    return {"owner": os.environ.get("PLATFORM_OWNER") or "platform", "region": os.environ.get("AWS_REGION", "ap-northeast-2"),
            "cloudfront_prefix_list_id": lists["PrefixLists"][0]["PrefixListId"]}


def _shared_init(name: str) -> Path:
    d = shared_dir(name)
    init(d, "", key=f"shared/{name}/terraform.tfstate")
    (d / "shared.auto.tfvars.json").write_text(json.dumps(shared_tfvars()))
    return d


def ensure_shared(store: Store, env: dict, name: str) -> tuple[dict, list]:
    """Creates (first environment) or confirms the shared stack, validating its plan like any environment's.
    Returns its outputs and PLAN results."""
    d = _shared_init(name)
    tf(d, "plan", "-input=false", SHARED_LOCK_TIMEOUT, "-out=shared.bin", "-var-file=shared.auto.tfvars.json")
    plan = json.loads(tf(d, "show", "-json", "shared.bin", capture=True))
    results = validate_plan(plan, blueprint=f"{name}-shared", fixed_cost_reasons=fixed_cost_reasons(env))
    for r in results:
        log(f"SHARED PLAN {r.status:15} {r.check_id:18} {r.detail}")
    failed = [r for r in results if r.status == FAIL]
    if failed:
        store.update(env["env_id"], checks=[r.to_dict() for r in results], plan_rejected=True,
                     event=f"공유 인프라 PLAN 검증 실패: {', '.join(r.check_id for r in failed)}")
        raise SystemExit(2)
    # Not the saved plan: an environment created at the same moment may have applied it first; this re-plans under the lock.
    tf(d, "apply", "-input=false", "-auto-approve", SHARED_LOCK_TIMEOUT, "-var-file=shared.auto.tfvars.json")
    raw = json.loads(tf(d, "output", "-json", capture=True))
    return {k: v["value"] for k, v in raw.items()}, results


def release_shared(store: Store, env: dict, name: str) -> None:
    """Destroys the shared stack when no other environment of the blueprint may hold resources on it."""
    others = [e["env_id"] for e in store.list_all()
              if e["env_id"] != env["env_id"] and e.get("status") in HOLDS_SHARED and getattr(for_env(e), "shared", None) == name]
    if others:
        store.update(env["env_id"], event=f"다른 컨테이너 환경 {len(others)}개가 공유 인프라를 쓰고 있어 그대로 둡니다.")
        return
    store.update(env["env_id"], event="공유 컨테이너 인프라(내부 ALB·VPC·클러스터)를 쓰는 환경이 없어 함께 삭제합니다.")
    d = _shared_init(name)
    tf(d, "destroy", "-input=false", "-auto-approve", SHARED_LOCK_TIMEOUT, "-var-file=shared.auto.tfvars.json")
    store.update(env["env_id"], event="공유 컨테이너 인프라를 삭제했습니다. 컨테이너 고정비가 더 이상 발생하지 않습니다.")


def apply_container(store: Store, env: dict, workdir: Path) -> None:
    """Shared stack first (validated and applied), then the environment exactly like Serverless: full plan validated,
    ECR only, the image copied and pinned by digest, final plan validated and applied (wait_for_steady_state)."""
    from runner.image_build import ImageBuildError, copy_image

    env_id = env["env_id"]
    store.update(env_id, event="공유 컨테이너 인프라(VPC·내부 ALB·클러스터)를 확인합니다. 처음 만들 때는 15분 정도 걸릴 수 있습니다.")
    shared, shared_results = ensure_shared(store, env, "container")
    store.update(env_id, shared=shared, event="공유 컨테이너 인프라가 준비됐습니다.")
    env = {**env, "shared": shared}

    init(workdir, env_id)
    write_vars(workdir, env)
    plan_and_validate(store, env, workdir, "container", out="precheck.bin", phase="(1/2 · 전체 구성, 이미지 확정 전)",
                      shared_results=shared_results)
    store.update(env_id, event="이미지 저장소를 만들고 컨테이너 이미지를 복사합니다.")
    tf(workdir, "apply", "-input=false", "-auto-approve", "-var-file=env.auto.tfvars.json", *ECR_ONLY["container"])

    art = env["artifact"]
    tag = f"rev{art.get('revision', 1)}-{int(time.time())}"
    try:
        image_uri = copy_image(source=art["image"], repository_url=repository_url(env), tag=tag, ecr=boto3.client("ecr"))
    except ImageBuildError as e:
        store.update(env_id, image_build_failed=True, image_build_error=str(e), event=f"컨테이너 이미지를 가져오지 못했습니다: {e}")
        raise SystemExit(3) from None
    store.update(env_id, event=f"컨테이너 이미지를 복사했습니다 ({image_uri.rsplit('@', 1)[-1][:19]}…). 원본 이미지는 바꾸지 않았습니다.")

    write_vars(workdir, env, image_uri)
    plan_and_validate(store, env, workdir, "container", phase="(2/2 · 최종 구성, 이미지 digest 고정)", shared_results=shared_results)
    store.update(env_id, event="컨테이너를 시작하고 상태 확인을 기다립니다. 보통 5~10분 걸립니다.")
    tf(workdir, "apply", "-input=false", "-auto-approve", "plan.bin")
    raw = json.loads(tf(workdir, "output", "-json", capture=True))
    outputs = {k: v["value"] for k, v in raw.items()}
    store.update(env_id, outputs=outputs, event=f"리소스를 생성했습니다: {outputs['url']}")


def destroy(store: Store, env: dict, workdir: Path) -> None:
    init(workdir, env["env_id"])
    tf(workdir, "destroy", "-input=false", "-auto-approve", "-var-file=env.auto.tfvars.json")
    store.update(env["env_id"], event="terraform destroy가 완료되었습니다.")
    bp = for_env(env)
    if bp and bp.shared:
        release_shared(store, env, bp.shared)


# ------------------------------------------------------------------ Day-2 repair (D-186)
# A drifted environment gets a plan back to its approved configuration. The plan is validated like any other
# (PLAN checks), saved, and summarised for a person; only after they approve is exactly that saved plan applied.
# Plans live under prefixes the runner can already reach: envs/<id>/repair/ and shared/<name>/repair/.

def _plan_changes(plan: dict) -> list[dict]:
    return [{"address": rc["address"], "actions": rc["change"]["actions"]}
            for rc in plan.get("resource_changes") or [] if rc["change"]["actions"] not in (["no-op"], ["read"])]


def _repair_stacks(env: dict, workdir: Path) -> list[tuple[str, Path, str, list[str]]]:
    """(stack, dir, saved-plan key, var/lock args) -- the shared stack first, as at creation."""
    bp = for_env(env)
    out = []
    if bp.shared:
        out.append(("shared", _shared_init(bp.shared), f"shared/{bp.shared}/repair/{env['env_id']}.bin",
                    [SHARED_LOCK_TIMEOUT, "-var-file=shared.auto.tfvars.json"]))
    init(workdir, env["env_id"])
    write_vars(workdir, env)  # image_uri falls back to the running, digest-pinned image in outputs
    out.append(("env", workdir, f"envs/{env['env_id']}/repair/env.bin", ["-var-file=env.auto.tfvars.json"]))
    return out


def repair_plan(store: Store, env: dict, workdir: Path) -> None:
    s3 = boto3.client("s3")
    bp = for_env(env)
    stacks, failed = [], []
    for stack, d, key, args in _repair_stacks(env, workdir):
        tf(d, "plan", "-input=false", "-out=repair.bin", *[a for a in args if a.startswith("-var-file")],
           *[a for a in args if a.startswith("-lock")])
        plan = json.loads(tf(d, "show", "-json", "repair.bin", capture=True))
        results = validate_plan(plan, ip_allowlist=list(env["input"].get("allowlist_cidrs") or []),
                                blueprint=f"{bp.shared}-shared" if stack == "shared" else bp.key,
                                fixed_cost_reasons=fixed_cost_reasons(env))
        bad = [r.check_id for r in results if r.status == FAIL]
        failed += bad
        s3.upload_file(str(d / "repair.bin"), os.environ["STATE_BUCKET"], key)
        stacks.append({"stack": stack, "key": key, "changes": _plan_changes(plan), "failed_checks": bad})
    changes = [c for s in stacks for c in s["changes"]]
    replaces = [c["address"] for c in changes if "delete" in c["actions"]]
    store.update(env["env_id"], repair_plan={"created_at": now(), "stacks": stacks, "changes": len(changes),
                                             "replaces": replaces, "passed": not failed},
                 event=(f"수정안을 만들었습니다: 리소스 {len(changes)}개를 승인된 구성으로 되돌립니다." if changes
                        else "Terraform으로 되돌릴 차이가 없습니다. Terraform이 관리하지 않는 부분이 바뀌었을 수 있어 "
                             "관리자 확인이 필요합니다.")
                 + (f" 다시 만들어지는 리소스: {', '.join(replaces)}." if replaces else "")
                 + (f" 수정안이 보안 검사를 통과하지 못했습니다: {', '.join(failed)}." if failed else ""))


def repair_apply(store: Store, env: dict, workdir: Path) -> None:
    s3 = boto3.client("s3")
    plan = env.get("repair_plan") or {}
    keys = {s["stack"]: s["key"] for s in plan.get("stacks") or []}
    for stack, d, key, args in _repair_stacks(env, workdir):
        if keys.get(stack) != key:
            raise SystemExit(f"no approved repair plan for the {stack} stack")
        s3.download_file(os.environ["STATE_BUCKET"], key, str(d / "repair.bin"))
        tf(d, "apply", "-input=false", "-auto-approve", *[a for a in args if a.startswith("-lock")], "repair.bin")
        if stack == "env":
            raw = json.loads(tf(d, "output", "-json", capture=True))
            outputs = {**(env.get("outputs") or {}), **{k: v["value"] for k, v in raw.items()}}
            store.update(env["env_id"], outputs=outputs)
    store.update(env["env_id"], event="승인된 수정안을 적용해 원래 구성으로 되돌렸습니다. 다시 검증합니다.")


def main() -> None:
    env_id, action = os.environ["ENV_ID"], os.environ["ACTION"]
    store = Store()
    env = store.get(env_id)
    if env is None:
        raise SystemExit(f"unknown environment {env_id}")
    bp = for_env(env)
    if bp is None:
        raise SystemExit(f"blueprint {env['recommendation']['architecture']['candidate_id']} is not provisionable")
    workdir = ROOT / "infra" / "blueprints" / bp.terraform_dir
    write_vars(workdir, env)
    try:
        {"apply": apply, "destroy": destroy, "repair_plan": repair_plan, "repair_apply": repair_apply}[action](store, env, workdir)
    except TerraformFailed as e:
        # the error text is kept for admins on every terraform failure; only init/plan/show change the stage
        store.update(env_id, terraform_prepare_failed=e.command in PREPARE_COMMANDS,
                     terraform_error=f"terraform {e.command}: {e.detail.strip()[-3000:]}")
        raise


if __name__ == "__main__":
    sys.exit(main())
