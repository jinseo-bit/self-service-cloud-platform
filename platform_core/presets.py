"""Use-case presets (D-11). A preset only pre-fills the 8 inputs; users can change all of them.

Only Static presets for now (decided 2026-10-04); more are added with each blueprint.
"""

PRESETS = [
    {
        "id": "portfolio",
        "title": "포트폴리오 · 블로그",
        "description": "개인 소개, 작업물, 글을 보여주는 사이트",
        "input": {
            "app_form": "STATIC_SITE", "traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY",
            "data": {"needed": False}, "downtime_tolerance": "OK", "latency_sensitivity": "TOLERANT",
            "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 5,
        },
    },
    {
        "id": "company-landing",
        "title": "회사 · 서비스 소개 페이지",
        "description": "업무시간에 방문이 많은 소개·랜딩 페이지",
        "input": {
            "app_form": "STATIC_SITE", "traffic_scale": "T2", "traffic_pattern": "BUSINESS_HOURS",
            "data": {"needed": False}, "downtime_tolerance": "NOT_IN_BUSINESS_HOURS",
            "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 10,
        },
    },
]
