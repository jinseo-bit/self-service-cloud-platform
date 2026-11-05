"""B-1 (D-189): the worker's new Security check (SB-LOG-01/versioning) must never go live before the log bucket has
versioning and the worker may read it. Otherwise every Static recheck sees a new security failure and blocks the
endpoint (D-65). Terraform enforces the order with depends_on on the worker Lambda; this test keeps it there."""

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[1] / "infra" / "platform"


def _block(text: str, header: str) -> str:
    return text.split(header, 1)[1].split("\n}\n", 1)[0]


def test_worker_lambda_waits_for_versioning_and_its_read_permission():
    worker = _block((TF / "api.tf").read_text(encoding="utf-8"), 'resource "aws_lambda_function" "worker" {')
    deps = re.search(r"depends_on\s*=\s*\[([^\]]*)\]", worker).group(1)
    assert "aws_s3_bucket_versioning.logs" in deps
    assert "aws_iam_role_policy.worker_log_versioning" in deps


def test_versioning_read_permission_is_scoped_to_the_log_bucket():
    pol = _block((TF / "api.tf").read_text(encoding="utf-8"), 'resource "aws_iam_role_policy" "worker_log_versioning" {')
    assert '"s3:GetBucketVersioning"' in pol and "aws_s3_bucket.logs.arn" in pol and "*" not in pol.replace("2012", "")


def test_log_bucket_versioning_is_enabled_with_noncurrent_expiry():
    base = (TF / "baseline.tf").read_text(encoding="utf-8")
    assert re.search(r'resource "aws_s3_bucket_versioning" "logs" \{[^}]*status = "Enabled"', base, re.S)
    assert "noncurrent_version_expiration" in _block(base, 'resource "aws_s3_bucket_lifecycle_configuration" "logs" {')
