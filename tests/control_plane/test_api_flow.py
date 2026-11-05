"""Control-plane API flow against moto (in-memory AWS). The real flow is exercised by scripts/e2e_static.py."""

import importlib
import io
import json
import os
import zipfile

import boto3
import pytest
from moto import mock_aws

from platform_core.presets import PRESETS

STATIC_INPUT = PRESETS[0]["input"]


@pytest.fixture
def api():
    os.environ.update(AWS_DEFAULT_REGION="ap-northeast-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test",
                      ENV_TABLE="envs", UPLOAD_BUCKET="uploads")
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="envs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "env_id", "AttributeType": "S"},
                                  {"AttributeName": "owner_sub", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "env_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "by-owner", "KeySchema": [{"AttributeName": "owner_sub", "KeyType": "HASH"}],
                                     "Projection": {"ProjectionType": "ALL"}}])
        boto3.client("s3").create_bucket(Bucket="uploads", CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
        sfn = boto3.client("stepfunctions")
        role = "arn:aws:iam::123456789012:role/sfn"
        definition = json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}})
        os.environ["PROVISION_SFN_ARN"] = sfn.create_state_machine(name="p", definition=definition, roleArn=role)["stateMachineArn"]
        os.environ["DELETE_SFN_ARN"] = sfn.create_state_machine(name="d", definition=definition, roleArn=role)["stateMachineArn"]
        os.environ["REDEPLOY_SFN_ARN"] = sfn.create_state_machine(name="r", definition=definition, roleArn=role)["stateMachineArn"]
        import control_plane.api as mod
        mod = importlib.reload(mod)
        mod._store = None
        yield mod


def call(mod, route, user="u1", admin=False, body=None, env_id=None, qs=None):
    groups = "[admin]" if admin else "[]"
    event = {"routeKey": route, "body": json.dumps(body) if body is not None else None,
             "pathParameters": {"env_id": env_id} if env_id else None, "queryStringParameters": qs,
             "requestContext": {"authorizer": {"jwt": {"claims": {"sub": user, "email": f"{user}@example.com",
                                                                  "cognito:groups": groups}}}}}
    r = mod.handler(event, None)
    return r["statusCode"], json.loads(r["body"])


def upload(env_id, files=None, revision=1):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in (files or {"index.html": "<h1>hello</h1>"}).items():
            z.writestr(k, v)
    boto3.client("s3").put_object(Bucket="uploads", Key=f"uploads/{env_id}/rev-{revision}.zip", Body=buf.getvalue())


def create(mod, user="u1", inp=None, name="site"):
    code, b = call(mod, "POST /environments", user=user, body={"name": name, "input": inp or STATIC_INPUT})
    assert code == 201, b
    return b


def test_recommendation_preview_is_static(api):
    code, b = call(api, "POST /recommendations", body={"input": STATIC_INPUT})
    assert code == 200 and b["architecture"]["candidate_id"] == "NONE+NONE" and b["provisionable"]


def test_invalid_input_is_400(api):
    code, b = call(api, "POST /recommendations", body={"input": {**STATIC_INPUT, "tier": "STAGING"}})
    assert code == 400 and "tier" in b["error"]


def test_create_upload_approve_starts_provisioning(api):
    b = create(api)
    env_id = b["environment"]["env_id"]
    assert b["upload"]["fields"]["key"] == f"uploads/{env_id}/rev-1.zip"
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 202, r
    env = r["environment"]
    assert env["status"] == "PROVISIONING" and env["expires_at"]
    stored = api.store().get(env_id)
    assert stored["execution_arn"] and stored["artifact"]["passed"]


def test_approve_without_upload_is_rejected(api):
    env_id = create(api)["environment"]["env_id"]
    code, b = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409


def test_secret_in_bundle_blocks_approval(api):
    env_id = create(api)["environment"]["env_id"]
    upload(env_id, {"index.html": "x", "app.js": "k='AKIAABCDEFGHIJKLMNOP'"})
    code, b = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 422
    assert any(c["check_id"] == "SB-SEC-04" and c["status"] == "FAIL" for c in b["artifact"]["checks"])
    assert api.store().get(env_id)["status"] == "DRAFT"


def test_non_provisionable_blueprint_cannot_be_approved(api):
    # A data layer is recommendation-only; Serverless (D-165) and Container (D-170) without a DB are provisionable.
    inp = {**STATIC_INPUT, "app_form": "SERVER_APP", "data": {"needed": True, "declared": "DYNAMODB"}, "budget_usd_month": 100}
    b = create(api, inp=inp)
    assert b["upload"] is None
    env_id = b["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409 and any(x["code"] == "UNSUP_BLUEPRINT_NOT_YET" for x in r["blocked"])


def test_budget_blocked_recommendation_cannot_be_approved(api):
    inp = {**STATIC_INPUT, "traffic_scale": "T3", "traffic_pattern": "STEADY", "budget_usd_month": 1}
    env_id = create(api, inp=inp)["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409 and any(x["code"] == "BUD_OVER_USER_LIMIT" for x in r["blocked"])


def test_dev_quota_is_enforced(api):
    for i in range(2):
        env_id = create(api, name=f"s{i}")["environment"]["env_id"]
        upload(env_id)
        assert call(api, "POST /environments/{env_id}/approve", env_id=env_id)[0] == 202
    env_id = create(api, name="third")["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409 and any(x["code"] == "BUD_QUOTA_EXCEEDED" for x in r["blocked"])


def test_prod_requires_admin_approval(api):
    env_id = create(api, inp={**STATIC_INPUT, "tier": "PROD"})["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "PENDING_ADMIN_APPROVAL"
    assert call(api, "POST /environments/{env_id}/approve", env_id=env_id)[0] == 403
    code, r = call(api, "POST /environments/{env_id}/approve", user="coach", admin=True, env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "PROVISIONING"
    assert r["environment"].get("expires_at") is None


def test_other_users_cannot_see_environment(api):
    env_id = create(api)["environment"]["env_id"]
    assert call(api, "GET /environments/{env_id}", user="intruder", env_id=env_id)[0] == 404
    assert call(api, "GET /environments/{env_id}", user="coach", admin=True, env_id=env_id)[0] == 200


def test_delete_draft_and_list(api):
    env_id = create(api)["environment"]["env_id"]
    code, r = call(api, "DELETE /environments/{env_id}", env_id=env_id)
    assert code == 200 and r["environment"]["status"] == "DELETED"
    assert call(api, "GET /environments")[1]["environments"] == []


def test_delete_ready_starts_destroy(api):
    env_id = create(api)["environment"]["env_id"]
    api.store().update(env_id, status="READY")
    code, r = call(api, "DELETE /environments/{env_id}", env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "DELETING"


def test_retry_only_for_provision_failures(api):
    env_id = create(api)["environment"]["env_id"]
    api.store().update(env_id, status="FAILED", failure={"stage": "PLAN"})
    assert call(api, "POST /environments/{env_id}/retry", env_id=env_id)[0] == 409
    api.store().update(env_id, failure={"stage": "PROVISION"})
    code, r = call(api, "POST /environments/{env_id}/retry", env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "PROVISIONING"


# ------------------------------------------------------------------ redeploy (D-150)

def _ready_env(api):
    env_id = create(api)["environment"]["env_id"]
    upload(env_id)
    assert call(api, "POST /environments/{env_id}/approve", env_id=env_id)[0] == 202
    stored = api.store().get(env_id)  # what the worker records after a successful first validation
    api.store().update(env_id, status="READY", current_revision=1, revisions=[{**stored["revisions"][0], "status": "READY"}],
                       outputs={"bucket_name": "b", "distribution_id": "E1", "url": "https://x"})
    return env_id


def test_first_approval_records_revision_1(api):
    env = api.store().get(_ready_env(api))
    assert env["artifact"]["key"].endswith("/rev-1.zip") and env["artifact"]["revision"] == 1
    assert [r["revision"] for r in env["revisions"]] == [1]


def test_redeploy_new_revision_without_terraform(api):
    env_id = _ready_env(api)
    code, b = call(api, "POST /environments/{env_id}/upload-url", env_id=env_id)
    assert code == 200 and b["revision"] == 2 and b["upload"]["fields"]["key"] == f"uploads/{env_id}/rev-2.zip"
    upload(env_id, {"index.html": "<h1>v2</h1>"}, revision=2)
    code, r = call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})
    assert code == 202, r
    env = api.store().get(env_id)
    assert env["status"] == "REDEPLOYING" and env["artifact"]["revision"] == 2
    assert [x["revision"] for x in env["revisions"]] == [1, 2] and env["revisions"][1]["status"] == "DEPLOYING"
    assert ":stateMachine:r" in env["execution_arn"].replace(":execution:", ":stateMachine:")  # redeploy machine, not provision
    assert boto3.client("s3").head_object(Bucket="uploads", Key=f"uploads/{env_id}/rev-1.zip")  # served bundle untouched


def test_redeploy_requires_upload_and_valid_bundle(api):
    env_id = _ready_env(api)
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})[0] == 409
    upload(env_id, {"index.html": "x", "a.js": "k='AKIAABCDEFGHIJKLMNOP'"}, revision=2)
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})[0] == 422
    assert api.store().get(env_id)["status"] == "READY"


def test_redeploy_only_from_served_states(api):
    draft = create(api)["environment"]["env_id"]
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=draft, body={})[0] == 409
    env_id = _ready_env(api)
    api.store().update(env_id, status="DELETING")
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})[0] == 409


def test_rollback_to_previous_revision(api):
    env_id = _ready_env(api)
    upload(env_id, {"index.html": "v2"}, revision=2)
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})[0] == 202
    api.store().update(env_id, status="FAILED", failure={"stage": "REDEPLOY"})
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={"revision": 9})[0] == 404
    code, r = call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={"revision": 1})
    assert code == 202, r
    env = api.store().get(env_id)
    assert env["artifact"]["revision"] == 1 and env["status"] == "REDEPLOYING"


def test_retry_after_failed_redeploy_reuses_redeploy_path(api):
    env_id = _ready_env(api)
    upload(env_id, {"index.html": "v2"}, revision=2)
    call(api, "POST /environments/{env_id}/redeploy", env_id=env_id, body={})
    api.store().update(env_id, status="FAILED", failure={"stage": "REDEPLOY"})
    code, r = call(api, "POST /environments/{env_id}/retry", env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "REDEPLOYING"


def test_delete_draft_removes_all_revision_bundles(api):
    env_id = create(api)["environment"]["env_id"]
    upload(env_id)
    upload(env_id, revision=2)
    assert call(api, "DELETE /environments/{env_id}", env_id=env_id)[0] == 200
    assert boto3.client("s3").list_objects_v2(Bucket="uploads", Prefix=f"uploads/{env_id}/").get("KeyCount") == 0


def test_revision_numbers_are_integers_after_a_dynamodb_round_trip(api):
    env_id = _ready_env(api)
    env = api.store().get(env_id)
    assert isinstance(env["current_revision"], int) and env["current_revision"] == env["revisions"][0]["revision"]
    body = json.loads(api.handler({"routeKey": "GET /environments/{env_id}", "pathParameters": {"env_id": env_id}, "body": None,
                                   "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u1@example.com",
                                                                                         "cognito:groups": "[]"}}}}}, None)["body"])
    assert body["environment"]["current_revision"] == 1



# ------------------------------------------------------------------ budget override (D-158)

OVER = {**STATIC_INPUT, "traffic_scale": "T2", "traffic_pattern": "STEADY", "budget_usd_month": 1}


def test_budget_override_is_recorded_and_admits(api):
    code, b = call(api, "POST /environments", body={"name": "ovr", "input": OVER, "budget_override": True})
    assert code == 201, b
    ovr = b["environment"]["budget_override"]
    assert ovr["confirmed"] and ovr["budget_usd"] == 1 and ovr["cost_upper_usd"] > 1 and ovr["confirmed_by"] == "u1@example.com"
    env_id = b["environment"]["env_id"]
    upload(env_id)
    assert call(api, "POST /environments/{env_id}/approve", env_id=env_id)[0] == 202
    assert api.store().get(env_id)["budget_override"]["excess_upper_usd"] == ovr["cost_upper_usd"] - 1


def test_over_budget_without_override_is_blocked(api):
    env_id = create(api, inp=OVER)["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409 and {x["code"] for x in r["blocked"]} == {"BUD_OVER_USER_LIMIT"}


def test_override_is_rejected_when_not_over_budget(api):
    code, _ = call(api, "POST /environments", body={"name": "x", "input": STATIC_INPUT, "budget_override": True})
    assert code == 400


def test_override_cannot_bypass_platform_cap(api):
    big = {**OVER, "traffic_scale": "T3"}  # max cost above the platform-wide monthly cap
    code, b = call(api, "POST /environments", body={"name": "big", "input": big, "budget_override": True})
    assert code == 201
    env_id = b["environment"]["env_id"]
    upload(env_id)
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 409 and "BUD_PLATFORM_CAP" in {x["code"] for x in r["blocked"]}



# ------------------------------------------------------------------ Quick Start templates (D-163)

QUICK = {"id": "STATIC_WEBSITE", "values": {"tier": "DEV"}}


def test_catalog_lists_only_enabled_templates(api):
    code, b = call(api, "GET /presets")
    assert code == 200 and [t["template_id"] for t in b["templates"]] == ["STATIC_WEBSITE"]
    assert all(t["enabled"] for t in b["templates"])


def test_template_recommendation_runs_the_same_engine(api):
    code, t = call(api, "POST /recommendations", body={"template": QUICK})
    assert code == 200 and t["architecture"]["candidate_id"] == "NONE+NONE" and t["provisionable"]
    assert t["origin"] == {"kind": "TEMPLATE", "template_id": "STATIC_WEBSITE", "expected_candidate": "NONE+NONE",
                           "matches_expectation": True}
    code, c = call(api, "POST /recommendations", body={"input": t["requirements"]})
    assert c["origin"] == {"kind": "CUSTOM"}
    strip = lambda r: {k: v for k, v in r.items() if k not in ("origin", "requirements")}  # noqa: E731
    assert strip(t) == strip(c), "template and custom answers with the same requirements must give the same result"


def test_template_environment_records_origin_and_requirements(api):
    code, b = call(api, "POST /environments", body={"name": "quick", "template": {"id": "STATIC_WEBSITE",
                                                    "values": {"tier": "PROD", "budget_usd_month": 9, "spa_fallback": True}}})
    assert code == 201, b
    env = b["environment"]
    assert env["origin"]["kind"] == "TEMPLATE" and env["tier"] == "PROD"
    assert env["input"]["app_form"] == "STATIC_SITE" and env["input"]["budget_usd_month"] == 9 and env["input"]["spa_fallback"]
    upload(env["env_id"])
    assert call(api, "POST /environments/{env_id}/approve", env_id=env["env_id"])[0] == 202  # PROD → admin approval
    assert api.store().get(env["env_id"])["status"] == "PENDING_ADMIN_APPROVAL"


def test_template_rejects_non_editable_fields_and_unknown_templates(api):
    code, b = call(api, "POST /recommendations", body={"template": {"id": "STATIC_WEBSITE", "values": {"app_form": "SERVER_APP"}}})
    assert code == 400 and "not editable" in b["error"]
    assert call(api, "POST /recommendations", body={"template": {"id": "WEB_API"}})[0] == 400


# ------------------------------------------------------------------ failed upload is explained and replaceable (D-164)

def test_failed_upload_keeps_a_summary_and_can_be_replaced_in_draft(api):
    env_id = create(api)["environment"]["env_id"]
    upload(env_id, {"router.exe": "MZ", "libcef.dll": "x", "locales/ko.pak": "x"})
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env_id)
    assert code == 422
    sm = r["artifact"]["summary"]
    assert sm["html_files"] == 0 and sm["not_web_files"] == 3 and [".dll", 1] in sm["extensions"]
    assert api.store().get(env_id)["artifact"]["summary"] == sm, "kept on the environment for the detail page"
    code, u = call(api, "POST /environments/{env_id}/upload-url", env_id=env_id)
    assert code == 200 and u["revision"] == 1 and u["upload"]["fields"]["key"] == f"uploads/{env_id}/rev-1.zip"
    upload(env_id, {"dist/index.html": "<h1>ok</h1>"})
    assert call(api, "POST /environments/{env_id}/approve", env_id=env_id)[0] == 202
    assert api.store().get(env_id)["artifact"]["root"] == "dist/"


# ------------------------------------------------------------------ presigned upload endpoint (first AWS deploy, 2026-10-05)

def test_presigned_upload_uses_regional_virtual_hosted_endpoint(api):
    """The global s3.amazonaws.com host is 307-redirected for new non-us-east-1 buckets; browsers fail CORS on that."""
    b = create(api)
    url = b["upload"]["url"]
    assert url == "https://uploads.s3.ap-northeast-2.amazonaws.com/", url
    assert b["upload"]["fields"]["x-amz-algorithm"] == "AWS4-HMAC-SHA256"  # SigV4 POST policy
    code, u = call(api, "POST /environments/{env_id}/upload-url", env_id=b["environment"]["env_id"])
    assert code == 200 and u["upload"]["url"] == url


# ------------------------------------------------------------------ Serverless blueprint (D-165, D-166)

SLS_INPUT = {**STATIC_INPUT, "app_form": "SERVER_APP", "budget_usd_month": 10}
GOOD_IMAGE = {"image": "traefik/whoami:v1.10.3", "env": {"WHOAMI_PORT_NUMBER": "8080"}, "health_path": "/"}


def test_serverless_recommendation_asks_for_an_image(api):
    code, r = call(api, "POST /recommendations", body={"input": SLS_INPUT})
    assert code == 200 and r["architecture"]["candidate_id"] == "SLS+NONE" and r["provisionable"]
    assert r["artifact_kind"] == "IMAGE" and r["production_ready_checks"]
    _, s = call(api, "POST /recommendations", body={"input": STATIC_INPUT})
    assert s["artifact_kind"] == "STATIC_ZIP"


def test_serverless_environment_is_created_with_a_validated_image_and_approved_without_upload(api):
    code, b = call(api, "POST /environments", body={"name": "app", "input": SLS_INPUT, "image": GOOD_IMAGE})
    assert code == 201, b
    env = b["environment"]
    assert b["upload"] is None
    assert env["artifact"]["kind"] == "IMAGE" and env["artifact"]["passed"]
    assert env["artifact"]["image"] == "docker.io/traefik/whoami:v1.10.3"
    assert call(api, "POST /environments/{env_id}/upload-url", env_id=env["env_id"])[0] == 409
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env["env_id"])
    assert code == 202 and r["environment"]["status"] == "PROVISIONING"
    stored = api.store().get(env["env_id"])
    assert stored["revisions"][0]["key"] == "docker.io/traefik/whoami:v1.10.3"
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env["env_id"], body={})[0] == 409


def test_serverless_rejects_missing_or_unsafe_image_input(api):
    for image in (None, {"image": "traefik/whoami"}, {"image": "nginx:1", "env": {"DB_PASSWORD": "x"}},
                  {"image": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/app:1"}):
        code, r = call(api, "POST /environments", body={"name": "x", "input": SLS_INPUT, **({"image": image} if image else {})})
        assert code == 400, (image, r)
        assert r["artifact"]["passed"] is False


# ------------------------------------------------------------------ failure detail visibility

def test_failure_detail_is_for_admins_only(api):
    env_id = create(api)["environment"]["env_id"]
    api.store().update(env_id, status="FAILED", terraform_prepare_failed=True,
                       terraform_error="terraform init: checksum mismatch for hashicorp/aws",
                       failure={"stage": "TERRAFORM_PREPARE", "message": "Terraform 준비 단계에서 실패했습니다.",
                                "error": "States.TaskFailed", "cause": "[{'PhaseType': 'BUILD', 'PhaseStatus': 'FAILED'}]",
                                "at": "2026-10-07T05:53:28Z"})
    code, owner = call(api, "GET /environments/{env_id}", env_id=env_id)
    assert code == 200
    f = owner["environment"]["failure"]
    assert f == {"stage": "TERRAFORM_PREPARE", "message": "Terraform 준비 단계에서 실패했습니다.", "at": "2026-10-07T05:53:28Z"}
    raw = json.dumps(owner, ensure_ascii=False)
    assert "States.TaskFailed" not in raw and "PhaseType" not in raw and "checksum" not in raw
    code, admin = call(api, "GET /environments/{env_id}", user="coach", admin=True, env_id=env_id)
    assert code == 200 and admin["environment"]["failure"]["error"] == "States.TaskFailed"
    assert "PhaseType" in admin["environment"]["failure"]["cause"] and "checksum" in admin["environment"]["terraform_error"]


def test_retry_after_prepare_failure_clears_the_failure_flags(api):
    env_id = create(api)["environment"]["env_id"]
    api.store().update(env_id, status="FAILED", terraform_prepare_failed=True, image_build_failed=True,
                       failure={"stage": "TERRAFORM_PREPARE", "message": "m"})
    code, r = call(api, "POST /environments/{env_id}/retry", env_id=env_id)
    assert code == 202 and r["environment"]["status"] == "PROVISIONING"
    env = api.store().get(env_id)
    assert env["terraform_prepare_failed"] is False and env["image_build_failed"] is False
    assert not env["terraform_error"]


# ------------------------------------------------------------------ Container (D-170)

CTR_INPUT = {**SLS_INPUT, "capabilities": {"realtime_connection": True}, "budget_usd_month": 100}


def test_container_recommendation_asks_for_an_image_and_shows_its_fixed_cost(api):
    code, r = call(api, "POST /recommendations", body={"input": CTR_INPUT})
    assert code == 200 and r["architecture"]["candidate_id"] == "CTR+NONE" and r["provisionable"]
    assert r["artifact_kind"] == "IMAGE" and r["blueprint"] == "container"
    assert {c["id"] for c in r["production_ready_checks"]} >= {"SB-PUB-02", "SB-PUB-03", "PR-C-02", "PLAN-FIXED-COST"}
    assert r["cost"]["fixed_usd"] > 0 and any(i["resource"] == "alb" and i["shared"] for i in r["cost"]["line_items_upper"])


def test_container_environment_is_created_like_serverless(api):
    code, b = call(api, "POST /environments", body={"name": "ws", "input": CTR_INPUT, "image": GOOD_IMAGE})
    assert code == 201, b
    env = b["environment"]
    assert b["upload"] is None and env["artifact"]["kind"] == "IMAGE" and env["artifact"]["passed"]
    code, r = call(api, "POST /environments/{env_id}/approve", env_id=env["env_id"])
    assert code == 202 and r["environment"]["status"] == "PROVISIONING"
    assert call(api, "POST /environments/{env_id}/redeploy", env_id=env["env_id"], body={})[0] == 409  # not in the MVP


# ------------------------------------------------------------------ overview (D-194)

def test_list_carries_health_and_a_summary_scoped_to_the_viewer(api):
    ready = _ready_env(api)
    api.store().update(ready, verdict={"verdict": "READY", "failed": [], "security_failed": False},
                       last_recheck={"at": "2026-11-05T06:22:11Z", "trigger": "MANUAL", "failed": []})
    drifted = create(api, name="d")["environment"]["env_id"]
    api.store().update(drifted, status="DRIFTED", cost_upper_usd=3,
                       drift={"detected_at": "2026-11-05T07:00:00Z", "security": True, "endpoint_disabled": True})
    create(api, user="u2", name="other")
    code, r = call(api, "GET /environments")
    assert code == 200
    rows = {e["env_id"]: e for e in r["environments"]}
    assert rows[ready]["checked_at"] == "2026-11-05T06:22:11Z" and rows[ready]["security_failed"] is False
    assert rows[drifted]["security_failed"] and rows[drifted]["endpoint_disabled"] and rows[drifted]["drift_since"]
    s = r["summary"]
    assert s["scope"] == "mine" and s["total"] == 2 and s["attention"] == 1 and s["security"] == 1
    assert s["committed_usd"] >= 3, "a drifted environment still costs money (D-193)"
    code, r = call(api, "GET /environments", user="admin", admin=True, qs={"all": "1"})
    assert r["summary"]["scope"] == "all" and r["summary"]["total"] == 3 and r["summary"]["cap_usd"] == 30
