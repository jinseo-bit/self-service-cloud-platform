"""Repository hygiene that must hold before any AWS deployment."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_secrets_in_repository():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_secrets.py")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout


def test_deployer_policy_is_scoped():
    doc = json.loads((ROOT / "infra" / "iam" / "deployer-policy.json").read_text(encoding="utf-8"))
    for st in doc["Statement"]:
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        assert "*" not in acts, f"{st['Sid']}: Action '*' is not allowed"
        if any(a.startswith("iam:") and a not in ("iam:GetRole", "iam:ListRoles") for a in acts) and st["Effect"] == "Allow":
            res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            assert all(":role/sscp-" in r or ":policy/sscp-" in r for r in res), st["Sid"]
    denied = {a for st in doc["Statement"] if st["Effect"] == "Deny" for a in st["Action"]}
    assert {"iam:CreateUser", "iam:CreateAccessKey", "ec2:CreateNatGateway"} <= denied


def test_examples_hold_no_credentials():
    for f in (".env.example", "infra/platform/terraform.tfvars.example"):
        txt = (ROOT / f).read_text(encoding="utf-8").lower()
        assert "aws_secret_access_key=" not in txt and "aws_access_key_id=" not in txt, f


def test_local_dev_server_is_never_deployed():
    """The local auth/upload stubs live only in scripts/; bundles must not contain them."""
    build = (ROOT / "scripts" / "build_bundles.py").read_text(encoding="utf-8")
    assert '"scripts"' not in build
    web = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'CFG.authMode === "local"' in web  # gated by config; deployed config.js (infra/platform/web.tf) never sets it
    assert "authMode" not in (ROOT / "infra" / "platform" / "web.tf").read_text(encoding="utf-8")
