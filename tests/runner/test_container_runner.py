"""Container runner pieces without AWS: image copy (crane faked), variables, listener priority, the shared stack's
PLAN gate and refcount (terraform faked)."""

import json
import os

import pytest

import runner.main as rm
from runner.image_build import ImageBuildError, copy_image

from .test_image_build import DIGEST, REPO, FakeEcr, fake_crane

SCALING = {"ctr_task_vcpu": 0.25, "ctr_task_memory_gb": 0.5, "ctr_min_tasks": 2, "ctr_capacity": "FARGATE",
           "edge_origin_response_timeout_s": 60, "alb_idle_timeout_s": 70}
SHARED = {"vpc_id": "vpc-1", "public_subnet_ids": ["subnet-a", "subnet-b"], "alb_security_group_id": "sg-alb",
          "alb_dns_name": "internal-sscp-shared", "alb_arn_suffix": "app/sscp-shared/1", "listener_arn": "arn:listener",
          "cluster_name": "sscp-shared", "vpc_origin_id": "vo_1", "alb_arn": "arn:lb"}


def container_env(env_id="env-abcd1234", status="PROVISIONING", **kw):
    return {"env_id": env_id, "owner_sub": "u", "owner_email": "u@example.com", "tier": "DEV", "expires_at": "x", "status": status,
            "input": {"allowlist_cidrs": []},
            "recommendation": {"architecture": {"candidate_id": "CTR+NONE", "frontend": "NONE", "compute": "CTR", "scaling": SCALING},
                               "reasons": [{"code": "FIX_ALWAYS_ON_COMPUTE"}, {"code": "FIX_ALB_FOR_CONTAINER"}, {"code": "SEL_X"}]},
            "artifact": {"kind": "IMAGE", "image": "docker.io/traefik/whoami:v1.10.3", "env": {"WHOAMI_PORT_NUMBER": "8080"},
                         "health_path": "/health", "revision": 1}, **kw}


@pytest.fixture(autouse=True)
def runner_env(monkeypatch):
    for k, v in {"ACCOUNT_ID": "111122223333", "AWS_REGION": "ap-northeast-2", "LOG_BUCKET": "sscp-logs",
                 "ENV_BOUNDARY_ARN": "arn:aws:iam::111122223333:policy/sscp-env-boundary", "STATE_BUCKET": "sscp-state"}.items():
        monkeypatch.setenv(k, v)


def test_image_is_copied_unchanged_and_pinned_by_digest():
    run, calls = fake_crane()
    uri = copy_image(source="docker.io/traefik/whoami:v1.10.3", repository_url=REPO, tag="rev1-1", ecr=FakeEcr(), run=run)
    assert uri == f"{REPO}@{DIGEST}"
    verbs = [c[0][1] for c in calls]
    assert verbs == ["auth", "copy", "digest"], "no append: the container runs the user's image as is"
    copy = calls[1][0]
    assert copy[2:] == ["--platform", "linux/amd64", "docker.io/traefik/whoami:v1.10.3", f"{REPO}:rev1-1"]


def test_unpullable_image_is_a_clear_error():
    run, _ = fake_crane(fail_on="copy")
    with pytest.raises(ImageBuildError, match="could not copy docker.io/x/y:1"):
        copy_image(source="docker.io/x/y:1", repository_url=REPO, tag="t", ecr=FakeEcr(), run=run)


def test_container_tfvars_follow_the_recommendation_and_the_shared_stack():
    v = rm.tfvars(container_env(shared=SHARED))
    assert (v["task_cpu"], v["task_memory"], v["desired_count"], v["capacity_provider"], v["origin_read_timeout_s"]) == (
        256, 512, 2, "FARGATE", 60)
    assert v["env_vars"] == {"WHOAMI_PORT_NUMBER": "8080"} and v["health_path"] == "/health"
    assert v["image_uri"] == f"{REPO}:sscp-plan-check" and v["log_bucket"] == "sscp-logs"
    assert {k: v[k] for k in rm.SHARED_VARS} == {k: SHARED[k] for k in rm.SHARED_VARS}
    assert "alb_arn" not in v, "only the variables the blueprint declares"
    pending = rm.tfvars(container_env())  # before the shared stack exists (first write / empty destroy)
    assert pending["public_subnet_ids"] == [] and pending["vpc_id"] == ""


def test_listener_priority_is_stable_and_in_range():
    p = rm.listener_priority("env-abcd1234")
    assert p == rm.listener_priority("env-abcd1234") and 1000 <= p <= 49999
    assert len({rm.listener_priority(f"env-{i:08d}") for i in range(200)}) > 190  # collisions are rare


def test_fixed_cost_reasons_come_from_the_recommendation():
    assert rm.fixed_cost_reasons(container_env()) == {"FIX_ALWAYS_ON_COMPUTE", "FIX_ALB_FOR_CONTAINER"}


class FakeStore:
    def __init__(self, envs=()):
        self.envs, self.updates = list(envs), []

    def list_all(self):
        return self.envs

    def update(self, env_id, **fields):
        self.updates.append(fields)


class Calls(list):
    plan: dict = {"resource_changes": []}  # what `terraform show -json` returns


@pytest.fixture
def fake_tf(monkeypatch, tmp_path):
    calls = Calls()

    def tf(cwd, *args, capture=False):
        calls.append(args)
        if args[0] == "show":
            return json.dumps(calls.plan)
        if args[0] == "output":
            return json.dumps({k: {"value": v} for k, v in SHARED.items()})
        return ""
    monkeypatch.setattr(rm, "tf", tf)
    monkeypatch.setattr(rm, "shared_dir", lambda name: tmp_path)
    monkeypatch.setattr(rm, "shared_tfvars", lambda: {"owner": "platform", "cloudfront_prefix_list_id": "pl-1"})
    return calls


@pytest.mark.parametrize("other_status,destroyed", [("READY", False), ("FAILED", False), ("PROVISIONING", False),
                                                    ("DELETED", True), ("DRAFT", True)])
def test_shared_stack_is_destroyed_only_by_the_last_container_environment(fake_tf, other_status, destroyed):
    me = container_env(status="DELETING")
    other = container_env(env_id="env-other001", status=other_status)
    static = {"env_id": "env-static01", "status": "READY",
              "recommendation": {"architecture": {"candidate_id": "NONE+NONE", "frontend": "STATIC"}}}
    rm.release_shared(FakeStore([me, other, static]), me, "container")
    assert any(a[0] == "destroy" for a in fake_tf) is destroyed
    if destroyed:
        init = next(a for a in fake_tf if a[0] == "init")
        assert "-backend-config=key=shared/container/terraform.tfstate" in init


def test_shared_plan_violation_blocks_before_anything_is_applied(fake_tf):
    fake_tf.plan = {"resource_changes": [{"address": "aws_lb.this", "type": "aws_lb", "change": {
        "actions": ["create"], "after": {"internal": False, "tags_all": {}}, "after_unknown": {}}}]}
    store = FakeStore()
    with pytest.raises(SystemExit) as e:
        rm.ensure_shared(store, container_env(), "container")
    assert e.value.code == 2 and not any(a[0] == "apply" for a in fake_tf)
    assert store.updates[-1]["plan_rejected"] is True and "SB-PUB-02" in store.updates[-1]["event"]


def test_shared_stack_applies_under_a_lock_and_returns_outputs(fake_tf):
    out, results = rm.ensure_shared(FakeStore(), container_env(), "container")
    assert out == SHARED
    apply = next(a for a in fake_tf if a[0] == "apply")
    assert rm.SHARED_LOCK_TIMEOUT in apply and "shared.bin" not in apply  # re-plans under the lock (concurrent creates)
    assert not [r for r in results if r.status == "FAIL"]
    assert os.environ["STATE_BUCKET"] == "sscp-state"
