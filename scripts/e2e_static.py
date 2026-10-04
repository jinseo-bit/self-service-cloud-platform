"""End-to-end test of the Static vertical slice against the REAL deployed platform.

    python scripts/e2e_static.py [--negative] [--keep]

1. creates/uses a Cognito test user and logs in through the public client (like the UI)
2. POST /environments (portfolio preset) → upload examples/static-site → approve
3. waits for READY, asserts the site serves our content over HTTPS (records SLO time)
4. --negative: removes the TLS-deny statement from the env bucket policy (a real
   misconfiguration), re-runs the worker validator, expects FAILED + endpoint disabled
5. DELETE → waits for DELETED → asserts the bucket and distribution are really gone

Writes build/e2e-report.json. Requires credentials for the deployer profile.
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp949
    _s.reconfigure(encoding="utf-8", errors="replace")
REGION = "ap-northeast-2"
SITE = ROOT / "examples" / "static-site"


def tf_outputs() -> dict:
    out = subprocess.run(["terraform", "-chdir=infra/platform", "output", "-json"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


def ensure_user(pool_id: str, client_id: str, email: str, password: str) -> str:
    idp = boto3.client("cognito-idp", region_name=REGION)
    try:
        idp.admin_create_user(UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
    except idp.exceptions.UsernameExistsException:
        pass
    idp.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)
    r = idp.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=client_id,
                          AuthParameters={"USERNAME": email, "PASSWORD": password})
    return r["AuthenticationResult"]["IdToken"]


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def site_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in SITE.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(SITE).as_posix())
    return buf.getvalue()


def presigned_post(upload: dict, data: bytes) -> None:
    boundary = "----sscp" + secrets.token_hex(8)
    parts = []
    for k, v in upload["fields"].items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="site.zip"\r\n'
                 f"Content-Type: application/zip\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(upload["url"], data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        assert r.status in (200, 204), r.status


def wait_status(api: Api, env_id: str, targets: set[str], timeout_s: int) -> dict:
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout_s:
        _, b = api.call("GET", f"/environments/{env_id}")
        env = b["environment"]
        if env["status"] != last:
            print(f"  [{int(time.monotonic() - start):4d}s] {env['status']}")
            last = env["status"]
        if env["status"] in targets:
            return env
        time.sleep(10)
    raise TimeoutError(f"{env_id} did not reach {targets} in {timeout_s}s (last={last})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative", action="store_true", help="inject a policy violation and expect FAILED")
    ap.add_argument("--keep", action="store_true", help="do not delete the environment at the end")
    ap.add_argument("--user", default="e2e-tester@example.com")
    args = ap.parse_args()

    out = tf_outputs()
    pool = out["user_pool_id"]
    client_id = boto3.client("cognito-idp", region_name=REGION).list_user_pool_clients(UserPoolId=pool)["UserPoolClients"][0]["ClientId"]
    password = "E2e-" + secrets.token_urlsafe(12) + "9a"
    api = Api(out["api_url"], ensure_user(pool, client_id, args.user, password))
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    _, cat = api.call("GET", "/presets")
    inp = dict(cat["presets"][0]["input"])
    code, created = api.call("POST", "/environments", {"name": "e2e-static", "input": inp})
    assert code == 201, created
    env_id = created["environment"]["env_id"]
    print(f"created {env_id}: {created['environment']['recommendation']['architecture']['candidate_id']}, "
          f"max ${created['environment']['cost_upper_usd']}/month")
    presigned_post(created["upload"], site_zip())

    t0 = time.monotonic()
    code, approved = api.call("POST", f"/environments/{env_id}/approve")
    assert code == 202, approved
    env = wait_status(api, env_id, {"READY", "DEGRADED", "FAILED"}, timeout_s=40 * 60)
    report["provision_seconds"] = int(time.monotonic() - t0)
    report["verdict"] = env.get("verdict")
    print(f"verdict={env['status']} in {report['provision_seconds']}s; failed={env.get('verdict', {}).get('failed')}")
    assert env["status"] == "READY", json.dumps(env.get("checks"), ensure_ascii=False, indent=1)

    body = urllib.request.urlopen(env["outputs"]["url"], timeout=15).read().decode()
    assert "sscp-e2e-marker" in body, "site content not served"
    report["url"] = env["outputs"]["url"]

    if args.negative:
        print("negative: removing the TLS-deny statement from the bucket policy")
        s3 = boto3.client("s3", region_name=REGION)
        bucket = env["outputs"]["bucket_name"]
        pol = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
        pol["Statement"] = [s for s in pol["Statement"] if s.get("Effect") != "Deny"]
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(pol))
        r = boto3.client("lambda", region_name=REGION).invoke(
            FunctionName="sscp-worker", Payload=json.dumps({"action": "validate", "env_id": env_id, "http_attempts": 3, "http_delay": 5}))
        result = json.loads(r["Payload"].read())
        _, b = api.call("GET", f"/environments/{env_id}")
        neg = b["environment"]
        failed = neg["verdict"]["failed"]
        print(f"  verdict={result.get('verdict')} failed={failed} endpoint_disabled={neg['verdict'].get('endpoint_disabled')}")
        assert neg["status"] == "FAILED" and "SB-TLS-04" in failed and neg["verdict"].get("endpoint_disabled")
        cfg = boto3.client("cloudfront").get_distribution_config(Id=env["outputs"]["distribution_id"])["DistributionConfig"]
        assert cfg["Enabled"] is False
        report["negative"] = {"failed": failed, "endpoint_disabled": True}

    if not args.keep:
        t1 = time.monotonic()
        code, d = api.call("DELETE", f"/environments/{env_id}")
        assert code == 202, d
        wait_status(api, env_id, {"DELETED", "DELETE_FAILED"}, timeout_s=40 * 60)
        report["delete_seconds"] = int(time.monotonic() - t1)
        s3 = boto3.client("s3", region_name=REGION)
        try:
            s3.head_bucket(Bucket=env["outputs"]["bucket_name"])
            raise AssertionError("bucket still exists")
        except ClientError as e:
            assert e.response["Error"]["Code"] in ("404", "NoSuchBucket"), e
        try:
            boto3.client("cloudfront").get_distribution(Id=env["outputs"]["distribution_id"])
            raise AssertionError("distribution still exists")
        except ClientError as e:
            assert e.response["Error"]["Code"] == "NoSuchDistribution", e
        print(f"deleted in {report['delete_seconds']}s; bucket and distribution are gone")

    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "e2e-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
