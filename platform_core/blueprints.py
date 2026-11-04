"""Provisionable blueprints (D-165). One place that says, per engine candidate, which Terraform root,
which artifact kind and which validators belong to it. API, runner, worker, local simulator and UI read this
instead of comparing candidate ids.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Blueprint:
    key: str            # "static" | "serverless" | "container"
    candidate_id: str   # engine candidate (compute+db)
    frontend: str       # "STATIC" | "NONE" — the engine's architecture.frontend for this blueprint
    artifact: str       # "STATIC_ZIP" | "IMAGE"
    terraform_dir: str  # under infra/blueprints/
    redeploy: bool      # D-150 file-only redeploy supported
    shared: str | None = None  # infra/shared/<name> stack this blueprint depends on (created/removed with refcount)


BLUEPRINTS = {
    "NONE+NONE": Blueprint("static", "NONE+NONE", "STATIC", "STATIC_ZIP", "static", True),
    # Serverless MVP: HTTP container image on Lambda + Lambda Web Adapter behind an HTTP API, no data layer.
    "SLS+NONE": Blueprint("serverless", "SLS+NONE", "NONE", "IMAGE", "serverless", False),
    # Container MVP (D-170): HTTP container image on ECS Fargate behind CloudFront → VPC origin → shared internal ALB.
    "CTR+NONE": Blueprint("container", "CTR+NONE", "NONE", "IMAGE", "container", False, shared="container"),
}

# (frontend, compute, db) combinations the platform can create; the engine marks everything else
# recommendation-only (UNSUP_BLUEPRINT_NOT_YET).
PROVISIONABLE = {(b.frontend, *b.candidate_id.split("+")) for b in BLUEPRINTS.values()}


def for_architecture(arch: dict | None) -> Blueprint | None:
    """The blueprint that creates this recommended architecture, or None if it is recommendation-only."""
    if not arch:
        return None
    b = BLUEPRINTS.get(arch.get("candidate_id", ""))
    return b if b and b.frontend == arch.get("frontend", "NONE") else None


def for_env(env: dict) -> Blueprint | None:
    return for_architecture((env.get("recommendation") or {}).get("architecture"))
