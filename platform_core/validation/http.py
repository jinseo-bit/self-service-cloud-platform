"""HTTP-layer validator: probes the environment from outside like a user would (D-91)."""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .result import FAIL, NOT_APPLICABLE, PASS, CheckResult

REQUIRED_HEADERS = ("strict-transport-security", "x-content-type-options", "x-frame-options")
UA = "sscp-validator/1.0"


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, timeout: float = 10, max_bytes: int = 65536) -> Response:
    """Never raises for network problems: status 0 means no HTTP response (refused, DNS, timeout)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(max_bytes))
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(65536) if e.fp else b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return Response(0, {"x-sscp-error": str(e)[:200]}, b"")


def _r(check_id, dim, ok, detail, *sb, status=None):
    return CheckResult(check_id, "HTTP", dim, status or (PASS if ok else FAIL), detail, tuple(sb))


def _get_until(url: str, want: int, attempts: int, delay: float) -> Response | None:
    last = None
    for i in range(attempts):
        last = fetch(url)
        if last.status == want:
            return last
        if i < attempts - 1:
            time.sleep(delay)
    return last


def validate_static_http(*, base_url: str, http_url: str | None = None, direct_origin_url: str | None,
                         spa_fallback: bool, ip_allowlist: list[str], health_path: str = "/",
                         attempts: int = 20, delay: float = 15) -> list[CheckResult]:
    """base_url: https://dxxxx.cloudfront.net ; http_url defaults to the same host over http://"""
    out: list[CheckResult] = []
    http_url = http_url or base_url.replace("https://", "http://", 1)

    if ip_allowlist:
        # The validator runs outside the allowlist, so it must be rejected (SB-PUB-08).
        resp = _get_until(base_url + health_path, 403, attempts, delay)
        out.append(_r("PR-S-04", "Security", resp is not None and resp.status == 403,
                      f"status from non-allowlisted source={getattr(resp, 'status', None)}", "SB-PUB-08"))
        out.append(_r("PR-ALL-02", "Reachability", True, "cannot reach from outside the allowlist by design",
                      status=NOT_APPLICABLE))
        return out

    resp = _get_until(base_url + health_path, 200, attempts, delay)
    ok = resp is not None and resp.status == 200
    out.append(_r("PR-ALL-02", "Reachability", ok, f"GET {health_path} -> {getattr(resp, 'status', 'no response')}"))
    if resp is not None and resp.status == 0:
        resp = None

    hresp = fetch(http_url + health_path)
    loc = hresp.headers.get("location", "")
    out.append(_r("PR-ALL-03", "Security", hresp.status in (301, 302, 307, 308) and loc.startswith("https://"),
                  f"http -> {hresp.status} {loc}", "SB-TLS-01"))

    if resp is not None:
        missing = [h for h in REQUIRED_HEADERS if h not in resp.headers]
        detail = f"missing headers {missing}" if missing else "security headers present"
        out.append(_r("PR-ALL-04", "Security", not missing, detail, "SB-TLS-05"))
    else:
        out.append(_r("PR-ALL-04", "Security", False, "no HTTPS response to inspect", "SB-TLS-05"))

    if direct_origin_url:
        d = fetch(direct_origin_url)
        out.append(_r("PR-S-01", "Security", d.status == 403, f"direct S3 GET -> {d.status}", "SB-PUB-01"))

    if spa_fallback:
        s = fetch(base_url + "/__sscp_probe/does-not-exist")
        out.append(_r("PR-S-02", "Reachability", s.status == 200, f"SPA fallback -> {s.status}"))
    else:
        out.append(_r("PR-S-02", "Reachability", True, "SPA fallback not requested", status=NOT_APPLICABLE))
    return out


def check_served_revision(*, base_url: str, expected_sha256: str | None, revision, attempts: int = 12,
                          delay: float = 10) -> CheckResult:
    """PR-S-03: the index.html actually served equals the deployed revision (cache invalidation done)."""
    if not expected_sha256:
        return _r("PR-S-03", "Operability", True, "no index fingerprint recorded", status=NOT_APPLICABLE)
    got = None
    for i in range(attempts):
        r = fetch(base_url + "/", max_bytes=5_000_000)
        got = hashlib.sha256(r.body).hexdigest() if r.status == 200 else f"status {r.status}"
        if got == expected_sha256:
            return _r("PR-S-03", "Operability", True, f"rev-{revision} index.html is served")
        if i < attempts - 1:
            time.sleep(delay)
    return _r("PR-S-03", "Operability", False, f"served index.html does not match rev-{revision} ({str(got)[:16]})")
