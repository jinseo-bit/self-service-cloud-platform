"""Every route the API Lambda handles must exist in API Gateway (infra/platform/api.tf). The Day-2 E2E of 2026-10-28
failed at the repair click with "Failed to fetch": /recheck and /repair were in control_plane/api.py ROUTES, so the
local server served them, but API Gateway had no such routes and the browser got a 404 without CORS headers."""

import re
from pathlib import Path

from control_plane.api import AGENT_ROUTES, ROUTES

API_TF = Path(__file__).resolve().parents[1] / "infra" / "platform" / "api.tf"


def _tf_routes() -> set[str]:
    block = re.search(r"routes\s*=\s*\[(.*?)\]", API_TF.read_text(encoding="utf-8"), re.S).group(1)
    return set(re.findall(r'"((?:GET|POST|DELETE|PUT|PATCH) [^"]+)"', block))


def test_api_gateway_routes_match_lambda_routes():
    assert _tf_routes() == set(ROUTES)


def test_agent_routes_are_handled():
    assert AGENT_ROUTES <= set(ROUTES)
