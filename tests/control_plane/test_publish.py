"""platform_core.publish against moto S3 + CloudFront: sync semantics and cache invalidation."""

import io
import os
import zipfile

import boto3
import pytest
from moto import mock_aws

from platform_core.publish import publish_bundle


@pytest.fixture
def aws():
    os.environ.update(AWS_DEFAULT_REGION="ap-northeast-2", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")
    with mock_aws():
        s3 = boto3.client("s3")
        for b in ("uploads", "site"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
        cf = boto3.client("cloudfront", region_name="us-east-1")
        dist = cf.create_distribution(DistributionConfig={
            "CallerReference": "x", "Comment": "", "Enabled": True,
            "Origins": {"Quantity": 1, "Items": [{"Id": "o", "DomainName": "site.s3.amazonaws.com",
                                                  "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "o", "ViewerProtocolPolicy": "redirect-to-https",
                                     "MinTTL": 0, "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
                                     "TrustedSigners": {"Enabled": False, "Quantity": 0}}})["Distribution"]["Id"]
        yield s3, cf, dist


def put_zip(s3, key, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    s3.put_object(Bucket="uploads", Key=key, Body=buf.getvalue())


def keys(s3):
    return {o["Key"] for o in s3.list_objects_v2(Bucket="site").get("Contents", [])}


def test_publish_replaces_content_and_removes_stale_files(aws):
    s3, cf, dist = aws
    put_zip(s3, "uploads/e/rev-1.zip", {"index.html": "v1", "old.js": "1", "css/a.css": "x"})
    r1 = publish_bundle(s3=s3, cloudfront=cf, upload_bucket="uploads", artifact_key="uploads/e/rev-1.zip", root="",
                        site_bucket="site", distribution_id=dist, reference="e-rev-1")
    assert r1["uploaded"] == 3 and r1["deleted"] == 0 and r1["invalidation_id"]
    put_zip(s3, "uploads/e/rev-2.zip", {"dist/index.html": "v2", "dist/new.js": "2"})
    r2 = publish_bundle(s3=s3, cloudfront=cf, upload_bucket="uploads", artifact_key="uploads/e/rev-2.zip", root="dist/",
                        site_bucket="site", distribution_id=dist, reference="e-rev-2")
    assert keys(s3) == {"index.html", "new.js"} and r2["deleted"] == 2
    obj = s3.get_object(Bucket="site", Key="index.html")
    assert obj["Body"].read() == b"v2"
    assert obj["ContentType"].startswith("text/html") and obj["CacheControl"] == "no-cache"
    assert s3.get_object(Bucket="site", Key="new.js")["CacheControl"].startswith("public")


def test_publish_skips_unsafe_paths(aws):
    s3, cf, dist = aws
    put_zip(s3, "uploads/e/rev-1.zip", {"index.html": "ok", "../escape.html": "no", "/abs.html": "no"})
    publish_bundle(s3=s3, cloudfront=cf, upload_bucket="uploads", artifact_key="uploads/e/rev-1.zip", root="",
                   site_bucket="site", distribution_id=dist, reference="r")
    assert keys(s3) == {"index.html"}
