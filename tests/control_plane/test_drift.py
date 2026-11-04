"""Day-2 drift (D-186): detection logic, the worker's recheck/event/sweep flow, and the shared-stack safety rule.
No AWS: the checks, the store, the endpoint block and the repair start are replaced by fakes."""

import copy
import re
from pathlib import Path

import pytest

from control_plane import worker
from platform_core import drift
from platform_core.validation.result import FAIL, PASS, CheckResult

ROOT = Path(__file__).resolve().parents[2]


def chk(cid, status=PASS, dim="Security", layer="STATE"):
    return CheckResult(check_id=cid, dimension=dim, layer=layer, status=status, detail=f"{cid} {status}")


class FakeStore:
    def __init__(self, envs):
        self.envs = {e["env_id"]: e for e in envs}
        self.events = []

    def get(self, env_id):
        return copy.deepcopy(self.envs.get(env_id))

    def list_all(self):
        return [copy.deepcopy(e) for e in self.envs.values()]

    def update(self, env_id, *, expect_status=None, event=None, **fields):
        env = self.envs[env_id]
        if expect_status and env["status"] not in expect_status:
            raise AssertionError(f"status {env['status']} not in {expect_status}")
        env.update(fields)
        if event:
            self.events.append((env_id, event))
        return copy.deepcopy(env)


def env(env_id="env-aaaa1111", status="READY", failed=(), outputs=None, shared=None):
    return {"env_id": env_id, "status": status, "tier": "DEV", "verdict": {"verdict": status, "failed": list(failed)},
            "outputs": outputs or {"bucket_name": f"sscp-{env_id}-site", "distribution_id": "E1ABCDEFGHIJKL"},
            "shared": shared, "checks": [chk("SB-PUB-01", layer="PLAN").to_dict()],
            "recommendation": {"architecture": {"candidate_id": "NONE+NONE", "frontend": "STATIC"}}}


@pytest.fixture
def world(monkeypatch):
    """A fake store, checks whose results the test sets, and recorders for the side effects."""
    state = {"results": [chk("SB-PUB-01"), chk("PR-ALL-02", dim="Reachability", layer="HTTP")], "blocked": [], "repairs": []}
    store = FakeStore([env()])
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setattr(worker, "run_checks", lambda e, ev: (state["results"], []))
    monkeypatch.setattr(worker, "_block_endpoint", lambda e: state["blocked"].append(e["env_id"]))
    monkeypatch.setattr(worker, "_start_repair", lambda env_id, phase: state["repairs"].append((env_id, phase)))
    state["store"] = store
    return state


def test_new_failure_is_drift_and_a_repair_plan_is_requested(world):
    world["results"] = [chk("SB-PUB-01", FAIL), chk("PR-ALL-02", dim="Reachability", layer="HTTP")]
    detail = {"eventName": "DeleteBucketPolicy", "eventTime": "2026-10-27T01:00:00Z",
              "userIdentity": {"arn": "arn:aws:iam::954222410428:user/sscp-deployer"}}
    out = worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT", "detail": detail})
    e = world["store"].envs["env-aaaa1111"]
    assert out["drift"] == ["SB-PUB-01"] and e["status"] == "DRIFTED"
    assert e["drift"]["actor"].endswith("user/sscp-deployer") and e["drift"]["event_name"] == "DeleteBucketPolicy"
    assert e["drift"]["security"] and e["drift"]["endpoint_disabled"] and world["blocked"] == ["env-aaaa1111"], "D-65"
    assert world["repairs"] == [("env-aaaa1111", "plan")], "a plan is prepared; nothing is applied"
    assert e["drift"]["detect_seconds"] is not None
    assert any("사람이 승인" in m for _, m in world["store"].events)


def test_failures_that_existed_at_approval_are_not_drift(world):
    world["store"].envs["env-aaaa1111"] = env(status="DEGRADED", failed=["PR-ALL-02"])
    world["results"] = [chk("SB-PUB-01"), chk("PR-ALL-02", FAIL, dim="Reachability", layer="HTTP")]
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "SCHEDULE"})["drift"] is None
    assert world["store"].envs["env-aaaa1111"]["status"] == "DEGRADED" and world["repairs"] == []


def test_non_security_drift_does_not_block_the_endpoint(world):
    world["results"] = [chk("SB-PUB-01"), chk("SB-LOG-04", FAIL, dim="Observability")]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "SCHEDULE"})
    e = world["store"].envs["env-aaaa1111"]
    assert e["status"] == "DRIFTED" and not e["drift"]["security"] and world["blocked"] == []


def test_same_drift_again_is_not_reported_twice(world):
    world["results"] = [chk("SB-PUB-01", FAIL)]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT"})
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "SCHEDULE"})["drift"] == "unchanged"
    assert world["repairs"] == [("env-aaaa1111", "plan")], "no second plan for the same drift"


BLOCKED = [chk("SB-PUB-01", FAIL), chk("PR-ALL-02/enabled", FAIL, dim="Reachability"),
           chk("PR-ALL-02", FAIL, dim="Reachability", layer="HTTP"), chk("PR-ALL-03", FAIL, layer="HTTP")]


def test_the_endpoint_block_itself_is_not_a_new_drift(world):
    """AWS E2E of 2026-10-28: the hourly sweep after a security drift saw the CloudFront it had disabled (D-65) as a
    second drift. It replaced the drift record, so the actor, the CloudTrail event and detect_seconds were lost."""
    world["results"] = [chk("SB-PUB-01", FAIL), chk("PR-ALL-02", dim="Reachability", layer="HTTP")]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT", "detail": {
        "eventName": "PutBucketPublicAccessBlock", "eventTime": "2026-10-28T07:26:42Z",
        "userIdentity": {"arn": "arn:aws:iam::954222410428:user/sscp-deployer"}}})
    world["results"] = BLOCKED
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "SCHEDULE"})["drift"] == "unchanged"
    e = world["store"].envs["env-aaaa1111"]
    assert e["drift"]["event_name"] == "PutBucketPublicAccessBlock" and e["drift"]["detect_seconds"] is not None
    assert len(e["drift_history"]) == 1 and world["repairs"] == [("env-aaaa1111", "plan")]


def test_a_blocked_endpoint_is_not_resolved_by_fixing_the_change_by_hand(world):
    """Only the approved repair turns the endpoint back on; a recheck must not call a disabled CloudFront READY."""
    world["results"] = [chk("SB-PUB-01", FAIL)]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT"})
    world["results"] = [chk("SB-PUB-01"), *BLOCKED[1:]]
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "MANUAL"})["drift"] == "unchanged"
    assert world["store"].envs["env-aaaa1111"]["status"] == "DRIFTED"


def test_a_different_change_while_blocked_is_still_drift(world):
    world["results"] = [chk("SB-PUB-01", FAIL)]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT"})
    world["results"] = [*BLOCKED, chk("SB-ENC-01", FAIL)]
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT"})["drift"] == ["SB-PUB-01", "SB-ENC-01"]
    e = world["store"].envs["env-aaaa1111"]
    assert e["drift"]["endpoint_disabled"] and len(e["drift_history"]) == 2


def test_drift_reverted_by_hand_goes_back_to_ready(world):
    world["results"] = [chk("SB-PUB-01", FAIL)]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT"})
    world["results"] = [chk("SB-PUB-01")]
    assert worker.recheck({"env_id": "env-aaaa1111", "trigger": "MANUAL"})["drift"] == "resolved"
    e = world["store"].envs["env-aaaa1111"]
    assert e["status"] == "READY" and e["drift"] is None and len(e["drift_history"]) == 1


def test_only_running_environments_are_rechecked(world):
    world["store"].envs["env-aaaa1111"]["status"] = "REPAIRING"
    assert worker.recheck({"env_id": "env-aaaa1111"})["skipped"] == "REPAIRING"


def test_cloudtrail_event_rechecks_the_environment_it_touched(world):
    world["results"] = [chk("SB-PUB-01", FAIL)]
    other = env("env-bbbb2222", outputs={"bucket_name": "sscp-env-bbbb2222-site", "distribution_id": "E2ZZZZZZZZZZZZ"})
    world["store"].envs[other["env_id"]] = other
    event = {"detail-type": "AWS API Call via CloudTrail", "source": "aws.s3", "detail": {
        "eventName": "PutBucketPolicy", "eventTime": "2026-10-27T01:00:00Z",
        "userIdentity": {"arn": "arn:aws:iam::954222410428:user/someone"},
        "requestParameters": {"bucketName": "sscp-env-aaaa1111-site"}}}
    out = worker.handler(event, None)
    assert [r["env_id"] for r in out["rechecked"]] == ["env-aaaa1111"]
    assert world["store"].envs["env-bbbb2222"]["status"] == "READY"


def test_the_platform_changing_its_own_resources_is_not_drift():
    """The runner applying a repair, or the worker blocking an endpoint, must not trigger another recheck."""
    e = env()
    for arn in ("arn:aws:sts::954222410428:assumed-role/sscp-runner/AWSCodeBuild-123",
                "arn:aws:sts::954222410428:assumed-role/sscp-worker/sscp-worker"):
        detail = {"userIdentity": {"arn": arn}, "requestParameters": {"bucketName": "sscp-env-aaaa1111-site"}}
        assert drift.match_environments(detail, [e], "sscp") == []
    human = {"userIdentity": {"arn": "arn:aws:iam::954222410428:user/sscp-deployer"},
             "requestParameters": {"bucketName": "sscp-env-aaaa1111-site"}}
    assert drift.match_environments(human, [e], "sscp") == ["env-aaaa1111"], "a person's console change is drift"


def test_shared_container_infrastructure_change_rechecks_every_container_env():
    shared = {"alb_security_group_id": "sg-0shared0alb000001", "vpc_id": "vpc-0shared000000001"}
    a = env("env-aaaa1111", shared=shared)
    b = env("env-bbbb2222", outputs={"service_name": "sscp-env-bbbb2222"}, shared=shared)
    detail = {"userIdentity": {"arn": "arn:aws:iam::1:user/x"}, "requestParameters": {"groupId": "sg-0shared0alb000001"}}
    assert sorted(drift.match_environments(detail, [a, b], "sscp")) == ["env-aaaa1111", "env-bbbb2222"]


def test_hourly_sweep_rechecks_running_environments(world):
    world["store"].envs["env-cccc3333"] = env("env-cccc3333", status="DELETED")
    out = worker.sweep({}, None)
    assert out["rechecked"] == ["env-aaaa1111"]


def test_repair_failure_keeps_the_environment_drifted(world):
    world["store"].envs["env-aaaa1111"]["status"] = "REPAIRING"
    worker.handler({"action": "repair_failed", "phase": "apply", "env_id": "env-aaaa1111"}, None)
    e = world["store"].envs["env-aaaa1111"]
    assert e["status"] == "DRIFTED" and world["repairs"] == [("env-aaaa1111", "plan")], "a fresh plan replaces the stale one"


def test_validation_after_repair_closes_the_drift_with_metrics(world, monkeypatch):
    world["results"] = [chk("SB-PUB-01", FAIL)]
    worker.recheck({"env_id": "env-aaaa1111", "trigger": "EVENT",
                    "detail": {"eventTime": "2026-10-27T01:00:00Z", "eventName": "PutBucketPolicy"}})
    drifted = world["store"].envs["env-aaaa1111"]
    drifted["drift"]["detected_at"] = "2026-10-28T07:26:51Z"
    drifted.update(status="VALIDATING", repair_approved_by="dev@example.com", repair_approved_at="2026-10-28T08:40:00Z")
    monkeypatch.setattr(worker, "now", lambda: "2026-10-28T08:45:15Z")
    world["results"] = [chk("SB-PUB-01")]
    worker.validate({"env_id": "env-aaaa1111"})
    e = world["store"].envs["env-aaaa1111"]
    assert e["status"] == "READY" and e["drift"] is None
    r = e["last_repair"]
    assert r["approved_by"] == "dev@example.com" and r["repair_seconds"] == 4704
    assert (r["wait_seconds"], r["apply_seconds"]) == (4389, 315), "the wait for a person is not the platform's time"


def test_drifted_and_repairing_envs_still_hold_the_shared_stack():
    """Otherwise deleting another container env would destroy the shared ALB under a drifted one."""
    from runner.main import HOLDS_SHARED
    assert {"DRIFTED", "REPAIRING", "VALIDATING"} <= HOLDS_SHARED


def test_eventbridge_rule_watches_exactly_the_python_list():
    """infra/platform/day2.tf and platform_core.drift.WATCHED_EVENTS are one list."""
    tf = (ROOT / "infra" / "platform" / "day2.tf").read_text(encoding="utf-8")
    block = tf.split("drift_watched = {", 1)[1].split("\n  }\n", 1)[0]
    parsed = {m.group(1): re.findall(r'"([^"]+)"', m.group(2)) for m in re.finditer(r'"(aws\.[a-z0-9]+)"\s*=\s*\[([^\]]*)\]', block)}
    assert parsed == drift.WATCHED_EVENTS
