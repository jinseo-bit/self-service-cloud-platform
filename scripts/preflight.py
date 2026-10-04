"""Pre-deployment checks. READ-ONLY: never creates, changes or deletes AWS resources.

    AWS_PROFILE=sscp python scripts/preflight.py

Local checks always run. AWS checks only call sts:GetCallerIdentity, cloudtrail:DescribeTrails,
s3control:GetPublicAccessBlock and budgets:DescribeBudgets (read-only) — and refuse to run as root.
Use --local-only to skip every AWS call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp949
    _s.reconfigure(encoding="utf-8", errors="replace")
REGION = "ap-northeast-2"
OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def check(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def local_checks() -> None:
    tf = shutil.which("terraform")
    if not tf:
        check(FAIL, "terraform installed")
    else:
        v = subprocess.run(["terraform", "version", "-json"], capture_output=True, text=True).stdout
        ver = json.loads(v)["terraform_version"]
        check(OK if tuple(map(int, ver.split(".")[:2])) >= (1, 10) else FAIL, "terraform >= 1.10", ver)
    for f in ("build/lambda.zip", "build/runner.zip"):
        check(OK if (ROOT / f).exists() else FAIL, f"bundle {f}", "" if (ROOT / f).exists() else "run: python scripts/build_bundles.py")
    tfvars = ROOT / "infra/platform/terraform.tfvars"
    if tfvars.exists():
        txt = tfvars.read_text(encoding="utf-8")
        m = re.search(r'admin_email\s*=\s*"([^"]+)"', txt)
        good = bool(m and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", m.group(1)) and "example.com" not in m.group(1))
        check(OK if good else FAIL, "terraform.tfvars admin_email", m.group(1) if m else "missing")
        if re.search(r"(?i)(access_key|secret)", txt):
            check(FAIL, "terraform.tfvars contains no credentials", "remove keys; use AWS_PROFILE")
    else:
        check(FAIL, "infra/platform/terraform.tfvars", "copy terraform.tfvars.example")
    scan = subprocess.run([sys.executable, str(ROOT / "scripts/scan_secrets.py")], capture_output=True, text=True)
    check(OK if scan.returncode == 0 else FAIL, "no secrets in repository", scan.stdout.strip().splitlines()[-1])
    profile = os.environ.get("AWS_PROFILE")
    check(OK if profile else WARN, "AWS_PROFILE set", profile or "unset — the default profile would be used")
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if os.environ.get(var):
            check(WARN, f"{var} not exported", "prefer a named profile over raw keys in the environment")


def aws_checks() -> None:
    import boto3

    session = boto3.Session(region_name=REGION)
    ident = session.client("sts").get_caller_identity()
    arn = ident["Arn"]
    if arn.endswith(":root"):
        check(FAIL, "caller is not root", f"{arn} — create the sscp-deployer IAM user (README §1). Stopping AWS checks.")
        return
    check(OK, "caller is an IAM principal", arn)
    check(OK if session.region_name == REGION else FAIL, "region", session.region_name)
    trails = session.client("cloudtrail").describe_trails(includeShadowTrails=True)["trailList"]
    names = [t["Name"] for t in trails]
    tfvars_path = ROOT / "infra/platform/terraform.tfvars"
    tfvars = tfvars_path.read_text(encoding="utf-8") if tfvars_path.exists() else ""
    creating = not re.search(r"create_trail\s*=\s*false", tfvars)
    if names and creating:
        check(WARN, "CloudTrail", f"existing trails {names}: set create_trail=false and existing_trail_name to avoid a billed second trail")
    else:
        check(OK, "CloudTrail", f"existing={names or 'none'}, create_trail={creating}")
    try:
        bpa = session.client("s3control").get_public_access_block(AccountId=ident["Account"])["PublicAccessBlockConfiguration"]
        check(OK, "account S3 Block Public Access", json.dumps(bpa))
    except Exception as e:
        check(WARN, "account S3 Block Public Access", f"not configured ({type(e).__name__}); bucket-level BPA is still enforced")
    try:
        budgets = session.client("budgets").describe_budgets(AccountId=ident["Account"]).get("Budgets", [])
        check(OK, "existing budgets", ", ".join(b["BudgetName"] for b in budgets) or "none")
    except Exception as e:
        check(WARN, "existing budgets", type(e).__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-only", action="store_true")
    a = ap.parse_args()
    local_checks()
    if not a.local_only:
        aws_checks()
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results)} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
