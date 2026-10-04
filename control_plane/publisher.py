"""Publisher Lambda (D-150): redeploys a new static revision into an existing environment.

No terraform: infrastructure is unchanged, only the site content is replaced. Invoked by the
redeploy state machine; validation runs afterwards in the worker (STATE + HTTP).
"""

from __future__ import annotations

import os

import boto3

from platform_core.publish import publish_bundle
from platform_core.validation.state import enable_distribution

from .store import Store

UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")


def handler(event, context):
    store = Store()
    env = store.get(event["env_id"])
    art, out = env["artifact"], env["outputs"]
    cloudfront = boto3.client("cloudfront")
    result = publish_bundle(s3=boto3.client("s3"), cloudfront=cloudfront, upload_bucket=UPLOAD_BUCKET,
                            artifact_key=art["key"], root=art.get("root", ""), site_bucket=out["bucket_name"],
                            distribution_id=out["distribution_id"], reference=f"{env['env_id']}-rev-{art['revision']}")
    store.update(env["env_id"], event=(f"rev-{art['revision']} 파일 {result['uploaded']}개를 올리고 이전 파일 {result['deleted']}개를 "
                                       f"정리했습니다. CDN 캐시를 비웁니다."))
    if (env.get("verdict") or {}).get("endpoint_disabled") and enable_distribution(cloudfront, out["distribution_id"]):
        store.update(env["env_id"], event="보안 실패로 막아 두었던 접속을 다시 열고, 새 버전을 처음부터 다시 검증합니다.")
    return {"env_id": env["env_id"], **result}
