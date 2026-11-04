"""Derived image (D-166) without touching a registry: crane and ECR are faked, the layer is real."""

import base64
import io
import json
import os
import subprocess
import tarfile

import pytest

from runner.image_build import ADAPTER_TARGET, ImageBuildError, adapter_layer, build_derived_image

REPO = "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/sscp-env-abcd1234"
DIGEST = "sha256:" + "d" * 64


class FakeEcr:
    def get_authorization_token(self):
        return {"authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:secret-token").decode()}]}


def fake_crane(fail_on=None, adapter_name="lambda-adapter"):
    calls = []

    def run(cmd, input=None, capture_output=True, text=True):
        calls.append((cmd, input))
        verb = cmd[1]
        if verb == fail_on:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: MANIFEST_UNKNOWN: manifest unknown")
        if verb == "export":
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                data = b"\x7fELF-adapter"
                info = tarfile.TarInfo(adapter_name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            with open(cmd[-1], "wb") as f:
                f.write(buf.getvalue())
        if verb == "digest":
            return subprocess.CompletedProcess(cmd, 0, DIGEST + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run, calls


def test_adapter_layer_puts_the_binary_where_lambda_loads_extensions():
    with tarfile.open(fileobj=io.BytesIO(adapter_layer(b"bin"))) as t:
        m = t.getmember(ADAPTER_TARGET)
        assert m.mode & 0o111 and t.extractfile(m).read() == b"bin"


def test_derived_image_is_pushed_to_the_environment_repo_and_pinned_by_digest():
    run, calls = fake_crane()
    ref = build_derived_image(source="docker.io/traefik/whoami:v1.10.3", repository_url=REPO, tag="rev1-1",
                              adapter_image="public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4", ecr=FakeEcr(), run=run)
    assert ref == f"{REPO}@{DIGEST}"
    verbs = [c[0][1] for c in calls]
    assert verbs == ["auth", "export", "append", "digest"]
    login = calls[0]
    assert login[1] == "secret-token" and "secret-token" not in json.dumps(login[0]), "password goes through stdin only"
    append = calls[2][0]
    assert append[append.index("-b") + 1] == "docker.io/traefik/whoami:v1.10.3", "the user image is the base, never modified"
    assert append[append.index("-t") + 1] == f"{REPO}:rev1-1"
    assert "--platform" in append and "linux/amd64" in append


def test_unpullable_source_becomes_a_clear_build_error():
    run, _ = fake_crane(fail_on="append")
    with pytest.raises(ImageBuildError, match="could not build from docker.io/x/missing:1.*public"):
        build_derived_image(source="docker.io/x/missing:1", repository_url=REPO, tag="t", adapter_image="a", ecr=FakeEcr(), run=run)


def test_adapter_image_without_the_binary_fails_loudly():
    run, _ = fake_crane(adapter_name="something-else")
    with pytest.raises(ImageBuildError, match="adapter binary not found"):
        build_derived_image(source="docker.io/x/y:1", repository_url=REPO, tag="t", adapter_image="a", ecr=FakeEcr(), run=run)


def test_runner_tfvars_for_serverless(monkeypatch):
    monkeypatch.setenv("ACCOUNT_ID", "111122223333")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    monkeypatch.setenv("ENV_BOUNDARY_ARN", "arn:aws:iam::111122223333:policy/sscp-env-boundary")
    from runner.main import tfvars
    env = {"env_id": "env-abcd1234", "owner_sub": "u", "owner_email": "u@example.com", "tier": "DEV", "expires_at": "x",
           "input": {}, "recommendation": {"architecture": {"candidate_id": "SLS+NONE", "frontend": "NONE"}},
           "artifact": {"kind": "IMAGE", "image": "docker.io/a/b:1", "env": {"A": "1"}, "health_path": "/h", "revision": 1}}
    v = tfvars(env)
    assert v["image_uri"] == f"{REPO}:sscp-plan-check" and v["env_vars"] == {"A": "1"} and v["health_path"] == "/h"
    assert v["env_boundary_arn"].endswith(":policy/sscp-env-boundary")
    assert tfvars(env, f"{REPO}@{DIGEST}")["image_uri"] == f"{REPO}@{DIGEST}"
    assert "spa_fallback" not in v and os.environ["ACCOUNT_ID"]
