"""Quick Start templates (D-163). A template is metadata only: it produces a Requirement Model input that goes
through the same Recommendation Engine, cost, admission and validators as the custom question flow.

Adding a template = adding an entry here. Only templates whose blueprint can be provisioned are enabled;
nothing in the UI or API branches on a specific template_id.
"""

from __future__ import annotations

import copy

from platform_core.engine import InputError

TEMPLATES = [
    {
        "template_id": "STATIC_WEBSITE",
        "title": "나만의 정적 웹사이트 만들기",
        "short_description": "포트폴리오, 소개 페이지, 문서 사이트처럼 서버가 필요 없는 웹사이트를 올려요.",
        "category": "WEBSITE",
        "enabled": True,
        "default_requirements": {
            "app_form": "STATIC_SITE", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY",
            "data": {"needed": False}, "capabilities": {}, "downtime_tolerance": "OK", "latency_sensitivity": "TOLERANT",
            "exposure": "PUBLIC", "allowlist_cidrs": [], "tier": "DEV", "budget_usd_month": 5, "spa_fallback": False,
        },
        # Only these may be set on the quick form; everything else changes through "세부 요구사항 바꾸기".
        "editable_fields": ["tier", "budget_usd_month", "spa_fallback"],
        "required_artifact": {"kind": "STATIC_ZIP", "label": "빌드 결과 zip (맨 위에 index.html)"},
        "supported_tiers": ["DEV", "PROD"],
        "blueprint_expectation": "NONE+NONE",
        "components": [
            {"service": "S3", "role": "웹 파일 저장"},
            {"service": "CloudFront", "role": "빠르고 안전하게 웹사이트 제공"},
        ],
    },
]

_BY_ID = {t["template_id"]: t for t in TEMPLATES}


def public_templates() -> list[dict]:
    return [copy.deepcopy(t) for t in TEMPLATES if t["enabled"]]


def get(template_id: str) -> dict:
    t = _BY_ID.get(template_id)
    if not t or not t["enabled"]:
        raise InputError(f"template '{template_id}' is not available")
    return t


def build_input(template_id: str, values: dict | None = None) -> dict:
    """Template defaults + the user's quick-form values → raw input for the Recommendation Engine."""
    t = get(template_id)
    values = values or {}
    unknown = sorted(set(values) - set(t["editable_fields"]))
    if unknown:
        raise InputError(f"template '{template_id}': not editable here: {unknown}")
    inp = copy.deepcopy(t["default_requirements"])
    inp.update(values)
    if inp["tier"] not in t["supported_tiers"]:
        raise InputError(f"template '{template_id}': tier must be one of {t['supported_tiers']}")
    return inp


def origin(template_id: str, rec: dict) -> dict:
    t = get(template_id)
    chosen = rec.get("architecture", {}).get("candidate_id")
    return {"kind": "TEMPLATE", "template_id": template_id, "expected_candidate": t["blueprint_expectation"],
            "matches_expectation": chosen == t["blueprint_expectation"]}
