"""Admission checks that depend on platform state (docs/recommendation-engine.md §1 "계약의 범위").

They never change the architecture; they only decide whether approval is allowed now.
"""

from __future__ import annotations

from . import constants as C
from .reasons import R

# Statuses whose environment holds (or is about to hold) AWS resources, so it counts toward the quota and the cap.
# D-193: REDEPLOYING (D-150) and DRIFTED/REPAIRING (D-186) were missing, so a drifted environment cost $0 here.
ACTIVE = {"PENDING_ADMIN_APPROVAL", "PROVISIONING", "VALIDATING", "READY", "DEGRADED", "FAILED", "DELETING", "DELETE_FAILED",
          "REDEPLOYING", "DRIFTED", "REPAIRING"}


def admit(recommendation: dict, *, owner_envs: list[dict], all_envs: list[dict], env_id: str,
          budget_override: bool = False) -> list[dict]:
    """Returns blocking reasons (empty = admitted). Envs are store records with
    'env_id', 'status', 'tier', 'cost_upper_usd'.

    D-158: the user's own budget is a SOFT guard (explicit override allowed). Everything else here —
    supported/provisionable architecture, per-user quota, platform-wide monthly cap — is HARD."""
    blocks = []
    status = recommendation.get("status")
    if status in ("UNSUPPORTED", "NEEDS_INPUT"):
        blocks.append(R("CONF_NO_CANDIDATE"))
    elif not recommendation.get("provisionable"):
        blocks.append(R("UNSUP_BLUEPRINT_NOT_YET", blueprint=recommendation.get("architecture", {}).get("candidate_id", "?")))
    if status == "OVER_BUDGET" and not budget_override:
        blocks.append(R("BUD_OVER_USER_LIMIT", cost_usd=recommendation.get("cost", {}).get("upper_usd", "?"),
                        budget_usd=recommendation.get("cost", {}).get("budget_usd", "?")))
    tier = recommendation["architecture"]["tier"] if "architecture" in recommendation else "DEV"
    mine = [e for e in owner_envs if e["status"] in ACTIVE and e["tier"] == tier and e["env_id"] != env_id]
    if len(mine) >= C.QUOTA_PER_USER[tier]:
        blocks.append(R("BUD_QUOTA_EXCEEDED", tier=tier, limit=C.QUOTA_PER_USER[tier]))
    committed = sum(int(e.get("cost_upper_usd") or 0) for e in all_envs if e["status"] in ACTIVE and e["env_id"] != env_id)
    new = int(recommendation.get("cost", {}).get("upper_usd") or 0)
    if committed + new > C.PLATFORM_MONTHLY_CAP_USD:
        blocks.append(R("BUD_PLATFORM_CAP", cap_usd=C.PLATFORM_MONTHLY_CAP_USD))
    return [b.to_dict() for b in blocks]
