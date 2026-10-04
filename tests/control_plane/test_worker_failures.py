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
