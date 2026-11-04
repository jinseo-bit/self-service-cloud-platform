"""Container boundaries (D-23, D-33, D-44, D-157, D-170). Container is chosen when the app needs something Lambda
cannot give (persistent connection, long request, memory/local state, big payload, an always-warm steady workload,
an IP allowlist) — and only then, because it always costs money. Serverless keeps every low or bursty, short,
cold-start-tolerant workload."""

import pytest

from platform_core.blueprints import for_architecture
from platform_core.engine import RawInput, recommend
from platform_core.pricing import PriceTable

P = PriceTable.load()
BASE = {"app_form": "SERVER_APP", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "data": {"needed": False},
        "downtime_tolerance": "OK", "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV",
        "budget_usd_month": 200}


def run(**kw):
    return recommend(RawInput.from_dict({**BASE, **kw}), P)


def arch(r):
    return r["architecture"]


# ---------------------------------------------------------------- Serverless side stays Serverless

@pytest.mark.parametrize("kw", [
    {"traffic_pattern": "IDLE_MOSTLY"},                                     # low traffic, scale to zero
    {"traffic_pattern": "SPIKY", "traffic_scale": "T2"},                    # bursts absorbed by Lambda
    {"capabilities": {"request_duration": "SECONDS"}},                      # short requests
    {"latency_sensitivity": "TOLERANT", "traffic_pattern": "BUSINESS_HOURS"},  # cold start tolerated
    {"capabilities": {"realtime_connection": False}},                       # no persistent connection
])
def test_serverless_workloads_never_pay_for_an_always_on_container(kw):
    r = run(**kw)
    assert arch(r)["candidate_id"] == "SLS+NONE", (kw, arch(r))
    assert not any(i["resource"] == "fargate-min-tasks" for i in r["cost"]["line_items_upper"])


# ---------------------------------------------------------------- Container side

@pytest.mark.parametrize("caps,origin_timeout", [
    ({"realtime_connection": True}, 30),            # persistent connection
    ({"request_duration": "ABOUT_30S"}, 60),        # long-running request (D-157: CloudFront origin 60 s)
    ({"request_duration": "S30_TO_60S"}, 90),
    ({"request_duration": "S60_TO_120S"}, 120),     # up to 2 minutes synchronously
    ({"in_memory_state": True}, 30),                # memory / local disk state across requests
    ({"large_payload": True}, 30),
])
def test_lambda_limits_select_a_provisionable_container(caps, origin_timeout):
    r = run(capabilities=caps)
    a = arch(r)
    assert a["candidate_id"] == "CTR+NONE" and r["provisionable"] is True
    assert for_architecture(a).key == "container"
    assert a["scaling"]["edge_origin_response_timeout_s"] == origin_timeout
    assert a["scaling"]["alb_idle_timeout_s"] == origin_timeout + 10  # D-157


def test_request_duration_rule_is_not_reverted():
    """D-157: only over 2 minutes is unsupported; up to 2 minutes is a synchronous Container."""
    assert run(capabilities={"request_duration": "OVER_120S"})["status"] == "UNSUPPORTED"
    assert arch(run(capabilities={"request_duration": "S60_TO_120S"}))["compute"] == "CTR"
    assert arch(run(capabilities={"request_duration": "SECONDS"}))["compute"] == "SLS"


def test_continuously_running_steady_workload_without_cold_starts_uses_container_by_cost():
    """D-23: an always-warm steady workload is Provisioned Concurrency vs Fargate by cost; at steady T3 Fargate wins."""
    r = run(traffic_pattern="STEADY", traffic_scale="T3", latency_sensitivity="SENSITIVE")
    assert arch(r)["candidate_id"] == "CTR+NONE" and r["provisionable"] is True
    small = run(latency_sensitivity="SENSITIVE")
    assert arch(small)["candidate_id"] == "SLS_PC+NONE"  # at low traffic PC is cheaper (recommendation-only for now)


def test_availability_and_tier_shape_the_container():
    a0 = arch(run(capabilities={"realtime_connection": True}))["scaling"]
    assert (a0["ctr_min_tasks"], a0["ctr_capacity"]) == (1, "FARGATE_SPOT")  # POL_SPOT_DEV_A0
    a1 = arch(run(capabilities={"realtime_connection": True}, downtime_tolerance="NOT_IN_BUSINESS_HOURS"))
    assert a1["scaling"]["ctr_min_tasks"] == 2 and a1["multi_az"] == "MULTI" and a1["scaling"]["ctr_capacity"] == "FARGATE"
    prod = arch(run(capabilities={"realtime_connection": True}, tier="PROD"))["scaling"]
    assert prod["ctr_capacity"] == "FARGATE"


def test_container_fixed_costs_are_shown_and_justified():
    r = run(capabilities={"realtime_connection": True})
    items = {i["resource"]: i for i in r["cost"]["line_items_upper"]}
    assert items["fargate-min-tasks"]["kind"] == "FIXED" and items["alb"]["shared"] is True
    reasons = {x["code"] for x in r["reasons"]}
    assert {"FIX_ALWAYS_ON_COMPUTE", "FIX_ALB_FOR_CONTAINER", "INFO_SHARED_MARGINAL_COST"} <= reasons
    assert "NAT" not in str(items)


def test_container_with_data_stays_recommendation_only():
    r = run(capabilities={"realtime_connection": True}, data={"needed": True, "declared": "DYNAMODB"})
    assert arch(r)["compute"] == "CTR" and r["provisionable"] is False
