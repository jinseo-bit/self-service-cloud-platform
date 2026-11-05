"""SB-PUB-07/findings (D-192): the ECR scan of the deployed image is read once, at validation. CRITICAL fails."""

import boto3
from botocore.stub import Stubber

from platform_core.validation.result import FAIL, NOT_APPLICABLE, PASS
from platform_core.validation.state import check_image_findings

REPO = "sscp-env-abcd1234"
URI = f"111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/{REPO}@sha256:" + "a" * 64
DIGEST = URI.split("@")[1]
PARAMS = {"repositoryName": REPO, "imageId": {"imageDigest": DIGEST}, "maxResults": 1}


def _ecr():
    return boto3.client("ecr", region_name="ap-northeast-2", aws_access_key_id="x", aws_secret_access_key="x")


def _scan(status, counts=None):
    return {"imageScanStatus": {"status": status}, "imageScanFindings": {"findingSeverityCounts": counts or {}}}


def run(*responses, attempts=3):
    ecr = _ecr()
    with Stubber(ecr) as st:
        for r in responses:
            if isinstance(r, str):
                st.add_client_error("describe_image_scan_findings", r, expected_params=PARAMS)
            else:
                st.add_response("describe_image_scan_findings", r, PARAMS)
        return check_image_findings(ecr, repository_name=REPO, image_uri=URI, attempts=attempts, delay=0, sleep=lambda _: None)


def test_clean_image_passes_and_records_counts():
    r = run(_scan("COMPLETE", {"HIGH": 2, "LOW": 5}))
    assert r.status == PASS and "high=2" in r.detail and "low=5" in r.detail and r.dimension == "Security"


def test_critical_finding_fails():
    r = run(_scan("COMPLETE", {"CRITICAL": 1, "HIGH": 3}))
    assert r.status == FAIL and "critical=1" in r.detail


def test_scan_in_progress_is_waited_for():
    r = run(_scan("IN_PROGRESS"), "ScanNotFoundException", _scan("COMPLETE"))
    assert r.status == PASS and r.detail.endswith("none")


def test_scan_never_finishing_is_not_applicable_not_a_pass():
    r = run(_scan("IN_PROGRESS"), _scan("IN_PROGRESS"), _scan("IN_PROGRESS"))
    assert r.status == NOT_APPLICABLE and "still running" in r.detail


def test_unsupported_image_is_not_applicable():
    assert run(_scan("UNSUPPORTED_IMAGE")).status == NOT_APPLICABLE


def test_access_denied_is_a_failure_not_hidden():
    assert run("AccessDeniedException").status == FAIL


def test_unpinned_image_fails_without_calling_aws():
    r = check_image_findings(_ecr(), repository_name=REPO, image_uri=f"{REPO}:latest", attempts=1, delay=0)
    assert r.status == FAIL
