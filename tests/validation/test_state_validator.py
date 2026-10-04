"""STATE validator unit tests with botocore Stubber (recorded AWS response shapes).

The live counterpart is scripts/e2e_static.py, which mutates a real environment
(e.g. disables BPA) and asserts the same validator returns FAILED.
"""

import json

import boto3
import pytest
from botocore.stub import Stubber

from platform_core.validation.result import FAIL, PASS, verdict
from platform_core.validation.state import validate_static_state

DIST_ARN = "arn:aws:cloudfront::111122223333:distribution/E123"
TAGS = {"project": "sscp", "managed-by": "platform", "env-id": "env-abcd1234", "owner": "t",
        "tier": "DEV", "blueprint": "static", "expires-at": "2026-10-11T00:00:00Z"}
CTX = {"bucket": "sscp-env-abcd1234-111122223333", "distribution_id": "E123", "distribution_arn": DIST_ARN,
       "tier": "DEV", "ip_allowlist": [], "log_bucket": "sscp-logs-111122223333", "trail_name": "sscp-account-trail"}

GOOD_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "AllowCloudFrontOAC", "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"},
     "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*",
     "Condition": {"StringEquals": {"AWS:SourceArn": DIST_ARN}}},
    {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::b", "arn:aws:s3:::b/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
]}


def dist_config(viewer="redirect-to-https", logging=True, enabled=True, headers=True, oac="OAC1"):
    behavior = {"TargetOriginId": "s3-origin", "ViewerProtocolPolicy": viewer}
    if headers:
        behavior["ResponseHeadersPolicyId"] = "67f7725c-6f97-4210-82d7-5512b31e9d03"
    return {"ETag": "E1", "DistributionConfig": {
        "CallerReference": "x", "Comment": "", "Enabled": enabled,
        "Origins": {"Quantity": 1, "Items": [{"Id": "s3-origin", "DomainName": "b.s3.ap-northeast-2.amazonaws.com",
                                               "OriginAccessControlId": oac}]},
        "DefaultCacheBehavior": behavior,
        "Logging": {"Enabled": logging, "IncludeCookies": False, "Bucket": "logs.s3.amazonaws.com" if logging else "",
                    "Prefix": "envs/x/"},
        "WebACLId": "",
    }}


def run(bpa_all=True, policy=GOOD_POLICY, encryption=True, cfg=None, bucket_tags=TAGS, trail_logging=True):
    s3 = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="x", aws_secret_access_key="x")
    cf = boto3.client("cloudfront", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    ct = boto3.client("cloudtrail", region_name="ap-northeast-2", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(s3) as s3s, Stubber(cf) as cfs, Stubber(ct) as cts:
        b = CTX["bucket"]
        s3s.add_response("get_public_access_block", {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": bpa_all, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}},
            {"Bucket": b})
        s3s.add_response("get_bucket_ownership_controls",
                         {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}, {"Bucket": b})
        if encryption:
            s3s.add_response("get_bucket_encryption", {"ServerSideEncryptionConfiguration": {"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}, {"Bucket": b})
        else:
            s3s.add_client_error("get_bucket_encryption", "ServerSideEncryptionConfigurationNotFoundError")
        s3s.add_response("get_bucket_policy", {"Policy": json.dumps(policy)}, {"Bucket": b})
        cfs.add_response("get_distribution_config", cfg or dist_config(), {"Id": "E123"})
        s3s.add_response("get_bucket_tagging", {"TagSet": [{"Key": k, "Value": v} for k, v in bucket_tags.items()]}, {"Bucket": b})
        cfs.add_response("list_tags_for_resource", {"Tags": {"Items": [{"Key": k, "Value": v} for k, v in TAGS.items()]}},
                         {"Resource": DIST_ARN})
        cts.add_response("get_trail_status", {"IsLogging": trail_logging}, {"Name": "sscp-account-trail"})
        s3s.add_response("get_bucket_lifecycle_configuration", {"Rules": [
            {"ID": "expire", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 90}}]},
            {"Bucket": "sscp-logs-111122223333"})
        return validate_static_state(s3=s3, cloudfront=cf, cloudtrail=ct, ctx=CTX)


def status_of(results, check_id):
    return {r.status for r in results if r.check_id == check_id}


def test_compliant_environment_passes():
    results = run()
    assert not [r.to_dict() for r in results if r.status == FAIL]
    assert verdict(results)["verdict"] == "READY"


@pytest.mark.parametrize("kwargs,check_id", [
    (dict(bpa_all=False), "SB-PUB-01"),
    (dict(encryption=False), "SB-ENC-01"),
    (dict(policy={"Statement": [GOOD_POLICY["Statement"][0]]}), "SB-TLS-04"),
    (dict(policy={"Statement": [{"Sid": "Pub", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                 "Resource": "*"}, GOOD_POLICY["Statement"][1]]}), "SB-PUB-01/policy"),
    (dict(cfg=dist_config(viewer="allow-all")), "SB-TLS-01"),
    (dict(cfg=dist_config(logging=False)), "SB-LOG-02"),
    (dict(cfg=dist_config(headers=False)), "SB-TLS-05"),
    (dict(cfg=dist_config(oac="")), "SB-PUB-01/oac"),
    (dict(bucket_tags={k: v for k, v in TAGS.items() if k != "owner"}), "PR-ALL-09"),
    (dict(bucket_tags={k: v for k, v in TAGS.items() if k != "expires-at"}), "PR-ALL-08"),
    (dict(trail_logging=False), "SB-LOG-01"),
])
def test_negative_state_violation_is_caught(kwargs, check_id):
    results = run(**kwargs)
    assert FAIL in status_of(results, check_id)
    assert verdict(results)["verdict"] == "FAILED"


def test_oac_grant_for_other_distribution_fails():
    other = json.loads(json.dumps(GOOD_POLICY))
    other["Statement"][0]["Condition"]["StringEquals"]["AWS:SourceArn"] = DIST_ARN.replace("E123", "EOTHER")
    assert FAIL in status_of(run(policy=other), "SB-PUB-01/policy")


def test_disabled_distribution_is_reachability_failure_only():
    results = run(cfg=dist_config(enabled=False))
    assert status_of(results, "PR-ALL-02/enabled") == {FAIL}
    v = verdict(results)
    assert v["verdict"] == "DEGRADED" and not v["security_failed"]


def test_all_static_state_checks_present():
    ids = {r.check_id for r in run()}
    assert {"SB-PUB-01", "SB-ENC-01", "SB-TLS-04", "SB-PUB-01/policy", "SB-TLS-01", "SB-TLS-05",
            "SB-LOG-02", "SB-LOG-01", "PR-ALL-09", "PR-ALL-08", "PR-ALL-05"} <= ids
    assert all(r.status in (PASS, "NOT_APPLICABLE") for r in run())


def _monitoring(tier, alarms=("a-5xx", "a-4xx"), wired=True, subs=1):
    from platform_core.validation.state import check_prod_monitoring
    cw = boto3.client("cloudwatch", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    sn = boto3.client("sns", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    topic = "arn:aws:sns:us-east-1:111122223333:t"
    ctx = {"tier": tier, "alarm_names": list(alarms), "alert_topic_arn": topic}
    with Stubber(cw) as cws, Stubber(sn) as sns:
        if tier == "PROD" and alarms:
            cws.add_response("describe_alarms", {"MetricAlarms": [
                {"AlarmName": n, "AlarmActions": [topic] if wired else []} for n in alarms]}, {"AlarmNames": list(alarms)})
            sns.add_response("list_subscriptions_by_topic", {"Subscriptions": [
                {"SubscriptionArn": f"{topic}:s{i}", "Protocol": "email"} for i in range(subs)]}, {"TopicArn": topic})
        return check_prod_monitoring(cloudwatch=cw, sns=sn, ctx=ctx)


def test_prod_monitoring_check():
    assert _monitoring("DEV").status == "NOT_APPLICABLE"
    assert _monitoring("PROD").status == PASS
    assert _monitoring("PROD", wired=False).status == FAIL
    assert _monitoring("PROD", subs=0).status == FAIL
    assert _monitoring("PROD", alarms=()).status == FAIL
