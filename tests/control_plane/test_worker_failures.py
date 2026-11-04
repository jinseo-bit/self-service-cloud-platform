"""worker.fail semantics (DynamoDB only, moto)."""

import importlib
import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def worker():
    os.environ.update(AWS_DEFAULT_REGION="ap-northeast-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", ENV_TABLE="envs")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="envs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "env_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "env_id", "KeyType": "HASH"}])
        import control_plane.worker as w
        yield importlib.reload(w)


def put(worker, **item):
    from control_plane.store import Store
    Store().put({"env_id": "env-0000aaaa", "tier": "PROD", "status": "REDEPLOYING", "event_log": [], **item})
    return Store()


def test_failed_redeploy_of_served_prod_env_is_not_scheduled_for_deletion(worker):
    s = put(worker, current_revision=1, artifact={"revision": 2},
            revisions=[{"revision": 1, "status": "READY"}, {"revision": 2, "status": "DEPLOYING"}])
    worker.fail({"env_id": "env-0000aaaa", "stage": "REDEPLOY", "error": {"Error": "X", "Cause": "boom"}})
    env = s.get("env-0000aaaa")
    assert env["status"] == "FAILED" and env["failure"]["stage"] == "REDEPLOY"
    assert "expires_at" not in env
    assert [r["status"] for r in env["revisions"]] == ["READY", "FAILED"]


def test_never_served_prod_failure_is_cleaned_up_later(worker):
    s = put(worker, status="PROVISIONING")
    worker.fail({"env_id": "env-0000aaaa", "stage": "PROVISION", "error": {"Error": "X", "Cause": "boom"}})
    assert s.get("env-0000aaaa")["expires_at"]


def test_image_build_failure_is_reported_as_its_own_stage(worker):
    put(worker, env_id="env-00000002", status="PROVISIONING", tier="DEV", image_build_failed=True,
        image_build_error="could not build from docker.io/x/y:1 (crane append failed: MANIFEST_UNKNOWN)")
    worker.fail({"env_id": "env-00000002", "stage": "PROVISION", "error": {"Error": "States.TaskFailed", "Cause": "{}"}})
    env = worker._store().get("env-00000002")
    assert env["status"] == "FAILED" and env["failure"]["stage"] == "IMAGE"
    assert "MANIFEST_UNKNOWN" in env["failure"]["cause"]
    assert "Lambda용 이미지" in env["event_log"][-1]["message"]


CODEBUILD_CAUSE = '{"Build": {"Phases": [{"PhaseType": "BUILD", "PhaseStatus": "FAILED"}]}}'


def test_terraform_init_failure_is_reported_as_prepare_stage(worker):
    put(worker, env_id="env-00000003", status="PROVISIONING", tier="DEV", terraform_prepare_failed=True,
        terraform_error="terraform init: Error while installing hashicorp/aws v6.67.0: doesn't match any of the checksums")
    worker.fail({"env_id": "env-00000003", "stage": "PROVISION", "error": {"Error": "States.TaskFailed", "Cause": CODEBUILD_CAUSE}})
    env = worker._store().get("env-00000003")
    f = env["failure"]
    assert env["status"] == "FAILED" and f["stage"] == "TERRAFORM_PREPARE"
    assert f["message"].startswith("Terraform 준비 단계에서 실패했습니다") and env["event_log"][-1]["message"] == f["message"]
    assert "checksums" in f["cause"] and f["error"] == "States.TaskFailed"  # root cause kept for admins
    assert "States.TaskFailed" not in f["message"] and "Phase" not in f["message"]


@pytest.mark.parametrize("flags,stage", [
    ({}, "PROVISION"),
    ({"plan_rejected": True, "terraform_prepare_failed": True}, "PLAN"),
    ({"image_build_failed": True, "image_build_error": "pull failed", "terraform_prepare_failed": True}, "IMAGE"),
    ({"terraform_prepare_failed": True}, "TERRAFORM_PREPARE"),
])
def test_failure_stage_mapping_and_messages(worker, flags, stage):
    put(worker, env_id="env-00000004", status="PROVISIONING", tier="DEV", **flags)
    worker.fail({"env_id": "env-00000004", "stage": "PROVISION", "error": {"Error": "States.TaskFailed", "Cause": CODEBUILD_CAUSE}})
    f = worker._store().get("env-00000004")["failure"]
    assert f["stage"] == stage and f["message"] and "States." not in f["message"]


def test_delete_failure_is_not_relabelled_as_prepare(worker):
    put(worker, env_id="env-00000005", status="DELETING", tier="DEV", terraform_prepare_failed=True)
    worker.fail({"env_id": "env-00000005", "stage": "DELETE", "error": {"Error": "States.TaskFailed", "Cause": CODEBUILD_CAUSE}})
    env = worker._store().get("env-00000005")
    assert env["status"] == "DELETE_FAILED" and env["failure"]["stage"] == "DELETE"


def test_apply_failure_stays_provision_but_keeps_the_terraform_error_for_admins(worker):
    put(worker, env_id="env-00000006", status="PROVISIONING", tier="DEV", terraform_prepare_failed=False,
        terraform_error="terraform apply: Error: creating API Gateway v2 Stage ($default): AccessDeniedException: "
                        "not authorized to perform: apigateway:TagResource")
    worker.fail({"env_id": "env-00000006", "stage": "PROVISION", "error": {"Error": "States.TaskFailed", "Cause": CODEBUILD_CAUSE}})
    f = worker._store().get("env-00000006")["failure"]
    assert f["stage"] == "PROVISION" and f["message"].startswith("AWS 리소스 생성 중 오류")
    assert "apigateway:TagResource" in f["cause"] and "PhaseType" not in f["cause"]


def test_container_image_failure_says_container_not_lambda(worker):
    arch = {"candidate_id": "CTR+NONE", "frontend": "NONE", "compute": "CTR"}
    put(worker, env_id="env-00000007", status="PROVISIONING", tier="DEV", image_build_failed=True,
        image_build_error="could not copy docker.io/x/y:1", recommendation={"architecture": arch})
    worker.fail({"env_id": "env-00000007", "stage": "PROVISION", "error": {"Error": "States.TaskFailed", "Cause": CODEBUILD_CAUSE}})
    f = worker._store().get("env-00000007")["failure"]
    assert f["stage"] == "IMAGE" and f["message"].startswith("컨테이너 이미지를 가져오지 못했습니다") and "Lambda" not in f["message"]
