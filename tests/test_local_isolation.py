"""Local simulation must never reach production code paths (RC gate, D-145).

The real boundary is server-side: every deployed API route requires a Cognito-signed JWT, so an
unsigned local dev token is rejected even if a browser were somehow running the UI in local mode.
"""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD_DIRS = ("control_plane", "platform_core", "runner", "infra")
LOCAL_MARKERS = ("local-unsigned", "/local/auth", "/local/upload", "local-sites", "mock_aws", "moto", "local_dev",
                 "authMode", "localMode", "LocalSimulator")


def _bundles():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_bundles.py")], check=True, capture_output=True)
    return [ROOT / "build" / "lambda.zip", ROOT / "build" / "runner.zip"]


def test_bundles_contain_only_production_code():
    for b in _bundles():
        with zipfile.ZipFile(b) as z:
            names = z.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            assert tops <= set(PROD_DIRS), (b.name, tops)
            for n in names:
                data = z.read(n).decode("utf-8", errors="ignore")
                hits = [m for m in LOCAL_MARKERS if m in data]
                assert not hits, f"{b.name}:{n} contains local-simulation marker(s) {hits}"


def test_production_python_has_no_simulation_fallback():
    for d in ("control_plane", "platform_core", "runner"):
        for f in (ROOT / d).rglob("*.py"):
            src = f.read_text(encoding="utf-8")
            for m in ("moto", "mock_aws", "local_dev", "localMode", "authMode", "SSCP_LOCAL"):
                assert m not in src, f"{f.relative_to(ROOT)} references {m}"


def test_every_deployed_api_route_requires_a_signed_jwt():
    tf = (ROOT / "infra" / "platform" / "api.tf").read_text(encoding="utf-8")
    block = re.search(r'resource "aws_apigatewayv2_route" "routes" \{(.*?)\n\}', tf, re.S).group(1)
    assert 'authorization_type = "JWT"' in block and "authorizer_id" in block
    assert len(re.findall(r'resource "aws_apigatewayv2_route"', tf)) == 1, "no other (unauthenticated) route resources"
    assert re.search(r'issuer\s*=\s*"https://cognito-idp\.', tf), "JWT issuer is the Cognito user pool"


def test_deployed_ui_config_never_enables_local_mode():
    web_tf = (ROOT / "infra" / "platform" / "web.tf").read_text(encoding="utf-8")
    cfg = re.search(r"window\.SSCP_CONFIG = \$\{jsonencode\(\{(.*?)\}\)\}", web_tf, re.S).group(1)
    assert set(re.findall(r"(\w+)\s*=", cfg)) == {"apiUrl", "region", "clientId"}
    assert not (ROOT / "web" / "config.js").exists(), "a config.js in web/ would be uploaded by the fileset"


def test_ui_local_branches_are_all_gated_by_config():
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for line in js.splitlines():
        if "/local/" in line:
            context = js[max(0, js.index(line) - 400): js.index(line)]
            assert 'CFG.authMode === "local"' in context, f"ungated local call: {line.strip()}"
    assert "SSCP_CONFIG || {}" in js  # missing config → empty config, which renders an error page, not local mode
    assert 'if (!CFG.apiUrl)' in js


def test_dev_only_dependencies_stay_out_of_runtime_requirements():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for dev in ("moto", "playwright", "pytest", "ruff"):
        assert dev not in runtime
