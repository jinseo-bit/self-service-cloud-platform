"""Offline `terraform plan` fixtures: real Terraform, real provider schema, fake credentials.

No AWS API is called (refresh disabled, no data sources); plans are fed to the real
PLAN validator.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".terraform-plugin-cache"

OFFLINE_OVERRIDE = """
terraform {
  backend "local" {}
}
provider "aws" {
  access_key                  = "offline"
  secret_key                  = "offline"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}
provider "aws" {
  alias                       = "us_east_1"
  access_key                  = "offline"
  secret_key                  = "offline"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}
"""

STATIC_VARS = {
    "env_id": "env-abcd1234", "account_id": "111122223333", "owner": "tester",
    "tier": "DEV", "expires_at": "2026-10-11T00:00:00Z", "log_bucket": "sscp-logs-111122223333",
}


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    CACHE.mkdir(exist_ok=True)
    env["TF_PLUGIN_CACHE_DIR"] = str(CACHE)
    # The fixture config has no lock file; let it reuse the cached provider anyway.
    env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
    env["TF_IN_AUTOMATION"] = "1"
    return env


def _tf(cwd, *args):
    p = subprocess.run(["terraform", *args, "-no-color"], cwd=cwd, env=_env(), capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"terraform {args[0]} failed:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def offline_plan(workdir: Path, variables: dict | None = None, out: str = "plan.bin") -> dict:
    if not (workdir / ".terraform").exists():
        _tf(workdir, "init", "-input=false")
    var_args = []
    for k, v in (variables or {}).items():
        var_args += ["-var", f"{k}={json.dumps(v) if isinstance(v, list) else v}"]
    _tf(workdir, "plan", "-input=false", "-refresh=false", f"-out={out}", *var_args)
    return json.loads(_tf(workdir, "show", "-json", out))


requires_terraform = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")


@pytest.fixture(scope="session")
def static_blueprint_dir(tmp_path_factory):
    dst = tmp_path_factory.mktemp("bp") / "infra"
    shutil.copytree(ROOT / "infra", dst, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
    bp = dst / "blueprints" / "static"
    (bp / "offline_override.tf").write_text(OFFLINE_OVERRIDE)
    return bp


@pytest.fixture(scope="session")
def static_plan(static_blueprint_dir):
    return offline_plan(static_blueprint_dir, STATIC_VARS)


@pytest.fixture(scope="session")
def static_plan_allowlist(static_blueprint_dir, static_plan):
    return offline_plan(static_blueprint_dir, {**STATIC_VARS, "ip_allowlist": ["203.0.113.0/24"]}, out="plan_ip.bin")


@pytest.fixture(scope="session")
def violating_plan(tmp_path_factory):
    dst = tmp_path_factory.mktemp("bad")
    shutil.copytree(ROOT / "tests" / "fixtures" / "tf" / "violating_static", dst, dirs_exist_ok=True)
    return offline_plan(dst)


@pytest.fixture(scope="session")
def static_plan_prod(static_blueprint_dir, static_plan):
    v = {**STATIC_VARS, "tier": "PROD", "expires_at": "", "alert_email": "owner@example.com"}
    return offline_plan(static_blueprint_dir, v, out="plan_prod.bin")
