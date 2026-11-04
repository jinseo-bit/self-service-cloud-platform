"""Serverless vs Container boundaries (D-157, D-165, D-170). Serverless is chosen only when every Lambda constraint
holds; anything that needs a long request, a persistent connection, local state or a big payload goes to
Container. Both are provisionable without a data layer."""

import pytest

from platform_core.blueprints import BLUEPRINTS, for_architecture
from platform_core.engine import RawInput, recommend
from platform_core.pricing import PriceTable

P = PriceTable.load()
BASE = {"app_form": "SERVER_APP", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "data": {"needed": False},
        "downtime_tolerance": "OK", "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV",
        "budget_usd_month": 100}


def run(**kw):
    return recommend(RawInput.from_dict({**BASE, **kw}), P)


def codes(r):
    """Reason codes recorded on the excluded Serverless candidates."""
    return {x["code"] for c in r["candidates"] if c["candidate_id"].startswith("SLS+") for x in c.get("reasons", [])}


@pytest.mark.parametrize("pattern", ["IDLE_MOSTLY", "SPIKY", "BUSINESS_HOURS"])
def test_low_or_irregular_traffic_scales_to_zero_on_serverless(pattern):
    r = run(traffic_pattern=pattern)
    assert r["architecture"]["candidate_id"] == "SLS+NONE" and r["provisionable"] is True


@pytest.mark.parametrize("caps,reason", [
    ({"request_duration": "ABOUT_30S"}, "EXC_SLS_TIMEOUT"),       # HTTP API 30 s hard limit leaves no margin (D-157)
    ({"request_duration": "S60_TO_120S"}, "EXC_SLS_TIMEOUT"),     # long-running request
    ({"realtime_connection": True}, "EXC_SLS_REALTIME"),          # persistent connection
    ({"in_memory_state": True}, "EXC_SLS_STATEFUL"),              # memory / local disk state across requests
    ({"large_payload": True}, "EXC_SLS_PAYLOAD"),                 # > 6 MB request/response
])
def test_lambda_constraints_send_the_app_to_container(caps, reason):
    r = run(capabilities=caps)
    assert r["architecture"]["compute"] == "CTR", r["architecture"]
    assert reason in codes(r)
    assert r["provisionable"] is True and r["architecture"]["candidate_id"] == "CTR+NONE"  # Container MVP (D-170)


def test_seconds_long_requests_stay_on_serverless():
    assert run(capabilities={"request_duration": "SECONDS"})["architecture"]["candidate_id"] == "SLS+NONE"


def test_over_two_minutes_is_unsupported_not_container():
    assert run(capabilities={"request_duration": "OVER_120S"})["status"] == "UNSUPPORTED"


def test_cold_start_sensitive_app_needs_provisioned_concurrency_which_is_not_provisionable_yet():
    r = run(latency_sensitivity="SENSITIVE")
    assert r["architecture"]["candidate_id"] in ("SLS_PC+NONE", "CTR+NONE") and r["provisionable"] is False


@pytest.mark.parametrize("kw", [{"data": {"needed": True, "declared": "DYNAMODB"}}, {"app_form": "STATIC_PLUS_API"}])
def test_mvp_scope_serverless_with_data_or_screen_is_recommendation_only(kw):
    assert run(**kw)["provisionable"] is False


def test_ip_allowlist_moves_the_app_to_container_with_waf():
    """Lambda behind an HTTP API cannot carry the WAF allowlist (EXC_SLS_IP_ALLOWLIST); Container's CloudFront can."""
    r = run(exposure="IP_ALLOWLIST", allowlist_cidrs=["203.0.113.0/24"])
    assert r["architecture"]["candidate_id"] == "CTR+NONE" and r["provisionable"] is True


def test_provisionable_flag_and_blueprint_registry_agree():
    for kw in ({}, {"app_form": "STATIC_SITE"}):
        r = run(**kw)
        bp = for_architecture(r["architecture"])
        assert bp is not None and bp in BLUEPRINTS.values() and r["provisionable"] is True
    assert for_architecture(run(capabilities={"realtime_connection": True})["architecture"]).key == "container"
    assert for_architecture(run(data={"needed": True, "declared": "DYNAMODB"})["architecture"]) is None


def test_serverless_choice_is_deterministic():
    a, b = run(traffic_pattern="SPIKY"), run(traffic_pattern="SPIKY")
    assert a == b and a["input_hash"] == b["input_hash"]
