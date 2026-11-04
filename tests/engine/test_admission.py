"""D-158: the user's budget is a SOFT guard; everything else in admission is HARD."""

from platform_core.engine import RawInput, recommend
from platform_core.engine import constants as C
from platform_core.engine.admission import admit
from platform_core.pricing import PriceTable

P = PriceTable.load()
STATIC = dict(app_form="STATIC_SITE", traffic_scale="T2", traffic_pattern="STEADY", downtime_tolerance="OK",
              latency_sensitivity="TOLERANT", exposure="PUBLIC", tier="DEV")


def rec(**kw):
    return recommend(RawInput.from_dict({**STATIC, **kw}), P)


def codes(blocks):
    return {b["code"] for b in blocks}


def test_over_budget_needs_explicit_override():
    r = rec(budget_usd_month=1)
    assert r["status"] == "OVER_BUDGET" and r["guardrails"]["budget_guard"] == "SOFT"
    assert codes(admit(r, owner_envs=[], all_envs=[], env_id="e")) == {"BUD_OVER_USER_LIMIT"}
    assert admit(r, owner_envs=[], all_envs=[], env_id="e", budget_override=True) == []


def test_override_cannot_bypass_platform_cap():
    r = rec(budget_usd_month=1)
    others = [{"env_id": "x", "status": "READY", "tier": "DEV", "cost_upper_usd": C.PLATFORM_MONTHLY_CAP_USD}]
    assert "BUD_PLATFORM_CAP" in codes(admit(r, owner_envs=[], all_envs=others, env_id="e", budget_override=True))


def test_override_cannot_bypass_quota():
    r = rec(budget_usd_month=1)
    mine = [{"env_id": f"x{i}", "status": "READY", "tier": "DEV", "cost_upper_usd": 0} for i in range(C.QUOTA_PER_USER["DEV"])]
    assert "BUD_QUOTA_EXCEEDED" in codes(admit(r, owner_envs=mine, all_envs=mine, env_id="e", budget_override=True))


def test_override_cannot_bypass_unsupported_or_unprovisionable():
    unsupported = recommend(RawInput.from_dict({**STATIC, "app_form": "SERVER_APP", "traffic_scale": "T1",
                                                "capabilities": {"request_duration": "OVER_120S"}}), P)
    assert admit(unsupported, owner_envs=[], all_envs=[], env_id="e", budget_override=True)
    server = recommend(RawInput.from_dict({**STATIC, "app_form": "SERVER_APP", "traffic_scale": "T1", "budget_usd_month": 0,
                                            "data": {"needed": True, "declared": "DYNAMODB"}}), P)  # data layer: recommendation-only
    assert "UNSUP_BLUEPRINT_NOT_YET" in codes(admit(server, owner_envs=[], all_envs=[], env_id="e", budget_override=True))


def test_security_baseline_is_identical_with_or_without_budget_pressure():
    """Override is an admission flag; it never reaches the engine, so the baseline list cannot change."""
    assert rec(budget_usd_month=1)["security_baseline"] == rec(budget_usd_month=10_000)["security_baseline"]
