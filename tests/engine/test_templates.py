"""Quick Start templates (D-163): metadata → Requirement Model → the same Recommendation Engine."""

import pytest

from platform_core import templates
from platform_core.engine import InputError, RawInput, recommend
from platform_core.pricing import PriceTable

PRICES = PriceTable.load()
REQUIRED_KEYS = {"template_id", "title", "short_description", "category", "enabled", "default_requirements",
                 "editable_fields", "required_artifact", "supported_tiers", "blueprint_expectation", "components"}


@pytest.mark.parametrize("t", templates.TEMPLATES, ids=lambda t: t["template_id"])
def test_every_template_is_metadata_with_the_full_schema(t):
    assert REQUIRED_KEYS <= set(t), REQUIRED_KEYS - set(t)
    assert set(t["editable_fields"]) <= set(t["default_requirements"])
    assert t["default_requirements"]["tier"] in t["supported_tiers"]


@pytest.mark.parametrize("t", [t for t in templates.TEMPLATES if t["enabled"]], ids=lambda t: t["template_id"])
def test_enabled_template_defaults_lead_the_engine_to_its_blueprint(t):
    rec = recommend(RawInput.from_dict(templates.build_input(t["template_id"])), PRICES)
    assert rec["architecture"]["candidate_id"] == t["blueprint_expectation"]
    assert rec["provisionable"], "only provisionable blueprints may be enabled as Quick Start"


def test_static_website_template_is_the_same_engine_run_as_the_custom_answers():
    inp = templates.build_input("STATIC_WEBSITE", {"tier": "PROD", "budget_usd_month": 7})
    custom = {"app_form": "STATIC_SITE", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "data": {"needed": False},
              "downtime_tolerance": "OK", "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "PROD",
              "budget_usd_month": 7}
    a = recommend(RawInput.from_dict(inp), PRICES)
    b = recommend(RawInput.from_dict(custom), PRICES)
    assert a["input_hash"] == b["input_hash"] and a == b, "a template must not have its own recommendation path"
    assert a["status"] == "RECOMMENDED" and a["architecture"]["candidate_id"] == "NONE+NONE"


def test_only_editable_fields_can_be_set_from_the_quick_form():
    with pytest.raises(InputError, match="not editable"):
        templates.build_input("STATIC_WEBSITE", {"app_form": "SERVER_APP"})
    with pytest.raises(InputError, match="not editable"):
        templates.build_input("STATIC_WEBSITE", {"exposure": "IP_ALLOWLIST"})


def test_unknown_or_disabled_template_and_unsupported_tier_are_rejected(monkeypatch):
    with pytest.raises(InputError):
        templates.build_input("NOPE")
    future = {**templates.TEMPLATES[0], "template_id": "WEB_API", "enabled": False, "blueprint_expectation": "SLS+NONE"}
    monkeypatch.setitem(templates._BY_ID, "WEB_API", future)
    with pytest.raises(InputError, match="not available"):
        templates.build_input("WEB_API")
    monkeypatch.setitem(templates._BY_ID, "DEV_ONLY", {**templates.TEMPLATES[0], "supported_tiers": ["DEV"]})
    with pytest.raises(InputError, match="tier"):
        templates.build_input("DEV_ONLY", {"tier": "PROD"})


def test_only_enabled_templates_are_published_and_defaults_are_not_shared():
    pub = templates.public_templates()
    assert [t["template_id"] for t in pub] == [t["template_id"] for t in templates.TEMPLATES if t["enabled"]]
    pub[0]["default_requirements"]["tier"] = "MUTATED"
    assert templates.TEMPLATES[0]["default_requirements"]["tier"] == "DEV"
