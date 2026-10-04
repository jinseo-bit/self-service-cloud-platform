"""Publish a validated static bundle into an environment's site bucket (D-150).

Shared by the runner (first deploy, after terraform apply) and the publisher Lambda
(redeploy, no terraform). Sync semantics: upload every file of the revision, then delete
objects that are not part of it, then invalidate the CDN cache.
"""

from __future__ import annotations

import mimetypes
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath


def content_headers(name: str) -> dict:
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    cache = "no-cache" if name.endswith(".html") else "public, max-age=3600"
    return {"ContentType": ctype, "CacheControl": cache}


def _existing_keys(s3, bucket: str) -> set[str]:
    keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys |= {o["Key"] for o in page.get("Contents", [])}
    return keys


def publish_bundle(*, s3, cloudfront, upload_bucket: str, artifact_key: str, root: str,
                   site_bucket: str, distribution_id: str, reference: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        z = Path(d) / "site.zip"
        s3.download_file(upload_bucket, artifact_key, str(z))
        written = set()
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(root):
                    continue
                key = info.filename[len(root):]
                if not key or key.startswith("/") or ".." in PurePosixPath(key).parts:
                    continue
                s3.put_object(Bucket=site_bucket, Key=key, Body=zf.read(info), **content_headers(key))
                written.add(key)
    stale = sorted(_existing_keys(s3, site_bucket) - written)
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=site_bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]], "Quiet": True})
    inv = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": f"{reference}-{time.time()}"})
    return {"uploaded": len(written), "deleted": len(stale), "invalidation_id": inv["Invalidation"]["Id"]}
