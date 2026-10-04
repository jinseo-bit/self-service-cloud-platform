"""docs/recommendation-engine.md §19 — 24 scenarios + determinism properties.

Expectations are pinned to RULES_VERSION and the pricing snapshot in the repo.
Blind labels from coach/reviewers (D-115) are compared separately; these tests guard
the engine's own contract.
"""

import json
import random

import pytest

from platform_core.engine import RawInput, recommend
from platform_core.engine.recommend import FIXED_JUSTIFICATION
from platform_core.pricing import PriceTable

P = PriceTable.load()

BASE = dict(app_form="SERVER_APP", traffic_scale="T1", traffic_pattern="IDLE_MOSTLY",
            downtime_tolerance="OK", latency_sensitivity="TOLERANT", exposure="PUBLIC",
            tier="DEV", budget_usd_month=50)
PG = {"needed": True, "detected": "POSTGRES"}
DDB = {"needed": True, "detected": "DYNAMODB"}


def run(**kw):
    d = {**BASE, **kw}
    return recommend(RawInput.from_dict(d), P)


def codes(items):
    return {i["code"] for i in items}


def cid(r):
    return r["architecture"]["candidate_id"]


# ------------------------------------------------------------------ representative (12)

REPRESENTATIVE = {
    "R01": (dict(app_form="STATIC_SITE", budget_usd_month=5), "NONE+NONE"),
    "R02": (dict(app_form="STATIC_SITE", traffic_scale="T2", traffic_pattern="BUSINESS_HOURS",
                 downtime_tolerance="NOT_IN_BUSINESS_HOURS", exposure="IP_ALLOWLIST",
                 allowlist_cidrs=["203.0.113.0/24"], tier="PROD", budget_usd_month=30), "NONE+NONE"),
    "R03": (dict(budget_usd_month=10), "SLS+NONE"),
    "R04": (dict(traffic_scale="T2", traffic_pattern="BUSINESS_HOURS", data=DDB, budget_usd_month=10), "SLS+DDB"),
    "R05": (dict(app_form="STATIC_PLUS_API", traffic_scale="T2", traffic_pattern="STEADY", data=DDB,
                 downtime_tolerance="NOT_IN_BUSINESS_HOURS", tier="PROD", budget_usd_month=30), "SLS+DDB"),
    "R06": (dict(traffic_scale="T2", traffic_pattern="SPIKY", tier="PROD", budget_usd_month=20), "SLS+NONE"),
    "R07": (dict(latency_sensitivity="SENSITIVE", tier="PROD", budget_usd_month=20), "SLS_PC+NONE"),
    "R08": (dict(capabilities={"realtime_connection": True}, traffic_scale="T2", traffic_pattern="STEADY",
                 data=PG, downtime_tolerance="NOT_IN_BUSINESS_HOURS", exposure="APP_AUTH",
                 tier="PROD", budget_usd_month=120), "CTR+RDS_MAZ"),
    "R09": (dict(capabilities={"large_payload": True}, traffic_pattern="BUSINESS_HOURS", budget_usd_month=40), "CTR+NONE"),
    "R10": (dict(capabilities={"request_duration": "S30_TO_60S"}, traffic_pattern="BUSINESS_HOURS",
                 data=PG, exposure="APP_AUTH", budget_usd_month=60), "CTR+AUR_PAUSE"),
    # ^ business-hours activity (35%) makes auto-pause Aurora $1 cheaper than the smallest RDS (D-52).
    # Engine picks SLS_PC over CTR: no capability excludes Serverless, PC removes cold start, cheaper.
    "R11": (dict(app_form="STATIC_PLUS_API", traffic_scale="T3", traffic_pattern="STEADY", data=PG,
                 downtime_tolerance="NEVER", latency_sensitivity="SENSITIVE", tier="PROD",
                 budget_usd_month=300), "SLS_PC+RDS_MAZ"),
    "R12": (dict(traffic_pattern="BUSINESS_HOURS", data={"needed": True, "declared": "POSTGRES"},
                 exposure="IP_ALLOWLIST", allowlist_cidrs=["198.51.100.10/32"], tier="PROD",
                 budget_usd_month=60), "CTR+RDS_SAZ"),
}


@pytest.mark.parametrize("sid", sorted(REPRESENTATIVE))
def test_representative(sid):
    kw, expected = REPRESENTATIVE[sid]
    r = run(**kw)
    assert r["status"] in ("RECOMMENDED", "RECOMMENDED_WITH_WARNINGS"), r.get("budget")
    assert cid(r) == expected


def test_r02_static_ip_allowlist_uses_waf():
    r = run(**REPRESENTATIVE["R02"][0])
    assert "FIX_WAF_FOR_IP_ALLOWLIST" in codes(r["reasons"])
    assert "SB-PUB-08" in r["security_baseline"]


def test_r08_realtime_excludes_serverless_with_reason():
    r = run(**REPRESENTATIVE["R08"][0])
    by_id = {c["candidate_id"]: c for c in r["candidates"]}
    assert by_id["SLS+RDS_MAZ"]["stage"] == "FUNCTIONAL"
    assert codes(by_id["SLS+RDS_MAZ"]["reasons"]) == {"EXC_SLS_REALTIME"}
    assert codes(by_id["CTR+RDS_SAZ"]["reasons"]) == {"EXC_DB_SINGLE_AZ"}


def test_r09_dev_ok_container_uses_spot():
    r = run(**REPRESENTATIVE["R09"][0])
    assert r["architecture"]["scaling"]["ctr_capacity"] == "FARGATE_SPOT"


def test_r12_ip_allowlist_excludes_serverless_at_security_stage():
    r = run(**REPRESENTATIVE["R12"][0])
    sls = [c for c in r["candidates"] if c["candidate_id"].startswith("SLS+")]
    assert {c["stage"] for c in sls} <= {"SECURITY", "FUNCTIONAL"}
    assert any(codes(c["reasons"]) == {"EXC_SLS_IP_ALLOWLIST"} for c in sls)


# ------------------------------------------------------------------ boundary (6)

def test_b01_sls_vs_ctr_crossover_records_delta():
    r = run(traffic_scale="T3", traffic_pattern="STEADY", budget_usd_month=200)
    sel = next(x for x in r["reasons"] if x["code"] in ("SEL_LOWEST_COST", "SEL_TIEBREAK_SIMPLER"))
    assert "runner_up" in sel["params"] or "other" in sel["params"]
    ranked = [c for c in r["candidates"] if c["status"] in ("SELECTED", "RANKED")]
    assert [c["rank"] for c in ranked] == list(range(1, len(ranked) + 1))


def test_b02_request_duration_boundary_switches_to_container():
    a = run(capabilities={"request_duration": "SECONDS"})
    b = run(capabilities={"request_duration": "ABOUT_30S"})  # too close to the 30 s HTTP API hard limit
    assert cid(a) == "SLS+NONE"
    assert cid(b).startswith("CTR+")
    by_id = {c["candidate_id"]: c for c in b["candidates"]}
    assert codes(by_id["SLS+NONE"]["reasons"]) == {"EXC_SLS_TIMEOUT"}


def test_b03_availability_boundary_switches_db_to_multi_az():
    common = dict(capabilities={"realtime_connection": True}, data=PG, traffic_pattern="STEADY", budget_usd_month=200)
    assert cid(run(downtime_tolerance="OK", **common)).endswith("RDS_SAZ")
    assert cid(run(downtime_tolerance="NOT_IN_BUSINESS_HOURS", **common)).endswith("RDS_MAZ")


def test_b04_budget_boundary():
    probe = run(app_form="STATIC_SITE", traffic_scale="T3", traffic_pattern="STEADY", budget_usd_month=10_000)
    upper = probe["cost"]["upper_usd"]
    kw = dict(app_form="STATIC_SITE", traffic_scale="T3", traffic_pattern="STEADY")
    assert run(budget_usd_month=upper, **kw)["status"] != "OVER_BUDGET"
    assert run(budget_usd_month=upper + 1, **kw)["status"] != "OVER_BUDGET"
    assert run(budget_usd_month=upper - 1, **kw)["status"] == "OVER_BUDGET"


def test_b05_serverless_rds_cost_includes_shared_endpoint():
    r = run(traffic_scale="T2", traffic_pattern="BUSINESS_HOURS", data=PG, budget_usd_month=100)
    if r["architecture"]["compute"].startswith("SLS"):
        items = {i["resource"] for i in r["cost"]["line_items_upper"]}
        assert "secretsmanager-endpoint" in items
        assert "SHARED_SM_ENDPOINT" in r["architecture"]["shared_dependencies"]
    else:
        ranked = {c["candidate_id"] for c in r["candidates"] if c["status"] == "RANKED"}
        assert any(c.startswith("SLS") for c in ranked)


def test_b06_idle_relational_prefers_aurora_pause():
    r = run(data=PG, traffic_pattern="IDLE_MOSTLY")
    assert r["architecture"]["db"] == "AUR_PAUSE"
    busy = run(data=PG, traffic_pattern="STEADY")
    assert busy["architecture"]["db"] == "RDS_SAZ"


# ------------------------------------------------------------------ conflict (3)

def test_x01_static_with_data_needs_input():
    r = run(app_form="STATIC_SITE", data={"needed": True})
    assert r["status"] == "NEEDS_INPUT"
    assert codes(r["reasons"]) == {"CONF_STATIC_WITH_DATA"}


def _fields(o):
    return [c["field"] for c in o["changes"]]


def test_x02_budget_too_small_offers_relax_options():
    r = run(data=PG, downtime_tolerance="NEVER", budget_usd_month=15)
    assert r["status"] == "OVER_BUDGET"
    opts = r["budget"]["relax_options"]
    assert any("downtime_tolerance" in _fields(o) for o in opts)
    assert _fields(opts[-1]) == ["budget_usd_month"]
    for o in opts[:-1]:
        assert set(_fields(o)) <= {"downtime_tolerance", "latency_sensitivity"}
        assert o["fits_budget"] == (o["cost_upper_usd"] <= 15)


def test_x03_db_hint_mismatch():
    r = run(data={"needed": True, "detected": "DYNAMODB", "declared": "POSTGRES"})
    assert r["status"] == "NEEDS_INPUT"
    assert codes(r["reasons"]) == {"CONF_DB_HINT_MISMATCH"}


# ------------------------------------------------------------------ unsupported (3)

def test_u01_t4_backend_unsupported_with_nearest():
    r = run(traffic_scale="T4")
    assert r["status"] == "UNSUPPORTED"
    assert codes(r["reasons"]) == {"UNSUP_TRAFFIC_SCALE"}
    assert r["nearest"]["changes"] == {"traffic_scale": "T3"}
    assert r["nearest"]["requires_confirmation"] is True


def test_u01b_t4_static_is_supported():
    assert run(app_form="STATIC_SITE", traffic_scale="T4", budget_usd_month=5000)["status"].startswith("RECOMMENDED")


def test_u02_over_2min_unsupported_with_async_alternative_and_cause():
    r = run(capabilities={"request_duration": "OVER_120S"})
    assert r["status"] == "UNSUPPORTED"
    assert codes(r["reasons"]) == {"UNSUP_REQUEST_OVER_120S"}
    assert r["causes"] == [{"code": "UNSUP_REQUEST_OVER_120S", "field": "request_duration", "value": "OVER_120S"}]
    assert {a["code"] for a in r["alternatives"]} == {"ALT_ASYNC_JOB", "ALT_SPLIT_UNDER_2MIN"}
    assert r["nearest"]["changes"] == {"capabilities.request_duration": "S60_TO_120S"}
    assert r["nearest"]["candidate_id"].startswith("CTR+") and r["nearest"]["requires_confirmation"]


@pytest.mark.parametrize("duration,origin,alb", [("SECONDS", 30, 40), ("ABOUT_30S", 60, 70), ("S30_TO_60S", 90, 100),
                                                  ("S60_TO_120S", 120, 130)])
def test_container_front_door_timeouts_follow_aws_limits(duration, origin, alb):
    """D-157: CloudFront origin response timeout 1-120 s (no quota increase), ALB idle 1-4000 s."""
    r = run(capabilities={"realtime_connection": True, "request_duration": duration}, budget_usd_month=200)
    sc = r["architecture"]["scaling"]
    assert cid(r).startswith("CTR+")
    assert sc["edge_origin_response_timeout_s"] == origin <= 120 and sc["alb_idle_timeout_s"] == alb <= 4000
    assert ("POL_LONG_REQUEST_TIMEOUTS" in codes(r["policies"])) == (origin > 30)


def test_one_to_two_minute_requests_are_supported_by_container():
    r = run(capabilities={"request_duration": "S60_TO_120S"}, budget_usd_month=200)
    assert r["status"].startswith("RECOMMENDED") and cid(r).startswith("CTR+")


def test_relax_prefers_single_change_that_fits():
    r = run(data=PG, traffic_pattern="STEADY", downtime_tolerance="NOT_IN_BUSINESS_HOURS", budget_usd_month=35,
            capabilities={"realtime_connection": True})
    opts = r["budget"]["relax_options"]
    assert opts[0]["fits_budget"] and len(opts[0]["changes"]) == 1


def test_relax_uses_pairs_only_when_no_single_change_fits():
    r = run(data=PG, downtime_tolerance="NEVER", latency_sensitivity="SENSITIVE", budget_usd_month=15)
    opts = r["budget"]["relax_options"]
    assert opts[0]["fits_budget"] and len(opts[0]["changes"]) == 2


def test_relax_shows_cost_reducing_changes_even_when_nothing_fits():
    r = run(app_form="STATIC_PLUS_API", traffic_scale="T2", traffic_pattern="STEADY", data=PG,
            downtime_tolerance="NOT_IN_BUSINESS_HOURS", tier="PROD", budget_usd_month=30,
            capabilities={"realtime_connection": True, "request_duration": "S60_TO_120S"})
    partial = [o for o in r["budget"]["relax_options"] if not o["fits_budget"]]
    assert partial and partial[0]["saved_usd"] > 0 and partial[0]["cost_upper_usd"] < r["cost"]["upper_usd"]
    assert partial[0]["reason"]["code"] == "BUD_RELAX_PARTIAL"


def test_relax_is_a_proposal_only():
    raw = RawInput.from_dict({**BASE, "data": PG, "downtime_tolerance": "NEVER", "budget_usd_month": 15})
    before = raw.to_canonical()
    recommend(raw, P)
    assert raw.to_canonical() == before


def test_u03_other_db_engine():
    r = run(data={"needed": True, "detected": "OTHER"})
    assert codes(r["reasons"]) == {"UNSUP_DB_ENGINE"}


# ------------------------------------------------------------------ properties

ALL_INPUTS = [{**BASE, **kw} for kw, _ in REPRESENTATIVE.values()]


@pytest.mark.parametrize("d", ALL_INPUTS)
def test_deterministic_repeated(d):
    first = json.dumps(recommend(RawInput.from_dict(d), P), sort_keys=True, ensure_ascii=False)
    for _ in range(20):
        assert json.dumps(recommend(RawInput.from_dict(d), P), sort_keys=True, ensure_ascii=False) == first


def test_deterministic_under_input_ordering():
    d = {**BASE, "exposure": "IP_ALLOWLIST", "allowlist_cidrs": ["10.0.0.1/32", "10.0.0.2/32", "10.0.0.3/32"],
         "config": {"env": {"B": "1", "A": "2"}, "secrets": ["Y", "X"]}}
    ref = json.dumps(recommend(RawInput.from_dict(d), P), sort_keys=True)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = dict(rng.sample(list(d.items()), len(d)))
        shuffled["allowlist_cidrs"] = rng.sample(d["allowlist_cidrs"], 3)
        shuffled["config"] = {"secrets": ["X", "Y"], "env": {"A": "2", "B": "1"}}
        assert json.dumps(recommend(RawInput.from_dict(shuffled), P), sort_keys=True) == ref


@pytest.mark.parametrize("d", ALL_INPUTS)
def test_every_fixed_cost_is_justified(d):
    r = recommend(RawInput.from_dict(d), P)
    reason_codes = codes(r["reasons"])
    for item in r["cost"]["line_items_upper"]:
        if item["kind"] == "FIXED" and item["cents"] > 0:
            assert FIXED_JUSTIFICATION[item["resource"]] in reason_codes


@pytest.mark.parametrize("d", ALL_INPUTS)
def test_every_excluded_candidate_has_stage_and_reason(d):
    r = recommend(RawInput.from_dict(d), P)
    for c in r["candidates"]:
        if c["status"] in ("EXCLUDED", "RANKED"):
            assert c["stage"] in ("FUNCTIONAL", "SECURITY", "AVAILABILITY", "PERFORMANCE", "COST")
            assert c["reasons"]


def test_relax_options_never_touch_security_or_facts():
    r = run(data=PG, downtime_tolerance="NEVER", latency_sensitivity="SENSITIVE",
            exposure="IP_ALLOWLIST", allowlist_cidrs=["10.0.0.0/24"], budget_usd_month=5)
    for o in r["budget"]["relax_options"]:
        assert set(_fields(o)) <= {"downtime_tolerance", "latency_sensitivity", "budget_usd_month"}


def test_no_nat_gateway_or_eks_anywhere():
    for d in ALL_INPUTS:
        r = recommend(RawInput.from_dict(d), P)
        blob = json.dumps(r)
        assert "nat" not in {i["resource"] for i in r["cost"]["line_items_upper"]}
        assert r["architecture"]["compute"] in ("NONE", "SLS", "SLS_PC", "CTR")
        assert "EKS" not in r["architecture"]["candidate_id"]
        assert "NAT" not in blob.upper().replace("ALTERNAT", "")


def test_only_static_is_provisionable_in_vertical_slice():
    assert run(app_form="STATIC_SITE")["provisionable"] is True
    r = run()
    assert r["provisionable"] is False
    assert "UNSUP_BLUEPRINT_NOT_YET" in codes(r["warnings"])


def test_pricing_metadata_is_reported():
    r = run(app_form="STATIC_SITE")
    assert r["pricing"] == {"snapshot_id": P.snapshot_id, "region": "ap-northeast-2", "updated_at": "2026-10-04",
                            "source": "manual", "approximate": True}
