"""Runner failure reporting: init/plan failures are recorded for the worker's TERRAFORM_PREPARE stage."""

import subprocess
from types import SimpleNamespace

import pytest

import runner.main as rm


def test_tf_failure_keeps_terraform_error_output(monkeypatch, tmp_path):
    monkeypatch.setattr(rm.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout=None, stderr="Error: Failed to install provider\n... doesn't match any of the checksums\n"))
    with pytest.raises(rm.TerraformFailed) as e:
        rm.tf(tmp_path, "init", "-input=false")
    assert isinstance(e.value, SystemExit) and e.value.command == "init" and "checksums" in e.value.detail


class FakeStore:
    def __init__(self):
        self.updates = []

    def get(self, env_id):
        return {"env_id": env_id}

    def update(self, env_id, **fields):
        self.updates.append(fields)


@pytest.mark.parametrize("command,prepare", [("init", True), ("plan", True), ("show", True), ("apply", False),
                                             ("destroy", False)])
def test_terraform_failures_keep_the_error_and_flag_only_prepare_commands(monkeypatch, command, prepare):
    store = FakeStore()
    monkeypatch.setenv("ENV_ID", "env-abcd1234")
    monkeypatch.setenv("ACTION", "apply")
    monkeypatch.setattr(rm, "Store", lambda: store)
    monkeypatch.setattr(rm, "for_env", lambda env: SimpleNamespace(terraform_dir="serverless"))
    monkeypatch.setattr(rm, "write_vars", lambda workdir, env: None)

    def failing_apply(*a):
        raise rm.TerraformFailed(command, 1, "Error: boom")

    monkeypatch.setattr(rm, "apply", failing_apply)
    with pytest.raises(rm.TerraformFailed):
        rm.main()
    (update,) = [u for u in store.updates if "terraform_error" in u]
    assert update["terraform_prepare_failed"] is prepare
    assert update["terraform_error"] == f"terraform {command}: Error: boom"
