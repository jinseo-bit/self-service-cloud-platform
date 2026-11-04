"""HTTP-layer validator: probes the environment from outside like a user would (D-91)."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
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


# ---------------------------------------------------------------- Serverless (D-165)

def validate_serverless_http(*, base_url: str, health_path: str = "/", attempts: int = 20, delay: float = 6) -> list[CheckResult]:
    """base_url: https://<api-id>.execute-api.<region>.amazonaws.com — the HTTP API's own HTTPS endpoint."""
    out: list[CheckResult] = []
    url = base_url.rstrip("/") + health_path
    t0 = time.monotonic()
    resp = _get_until(url, 200, attempts, delay)  # first request includes the cold start
    took = time.monotonic() - t0
    ok = resp is not None and resp.status == 200
    status = getattr(resp, "status", "no response")
    out.append(_r("PR-ALL-02", "Reachability", ok, f"GET {health_path} -> {status} ({took:.1f}s incl. cold start)"))

    # The app itself answered: not an API Gateway / Lambda Web Adapter error page.
    gw_error = resp is not None and ("x-amzn-errortype" in resp.headers or resp.body.strip() in
                                     (b'{"message":"Internal Server Error"}', b'{"message":"Service Unavailable"}'))
    out.append(_r("PR-L-05", "Reachability", ok and not gw_error and len(resp.body) > 0,
                  f"app response {len(resp.body) if resp else 0} bytes" if ok and not gw_error else
                  f"gateway/adapter error: {resp.headers.get('x-amzn-errortype') if resp else 'no response'}"))

    # SB-TLS-02: execute-api serves HTTPS only; a plain-HTTP request must not get a page.
    h = fetch(base_url.replace("https://", "http://", 1).rstrip("/") + health_path, timeout=5)
    https_only = h.status == 0 or (h.status in (301, 302, 307, 308) and h.headers.get("location", "").startswith("https://"))
    out.append(_r("PR-ALL-03", "Security", https_only, f"http -> {h.status or 'no HTTP listener'}", "SB-TLS-02"))

    if resp is not None and resp.status:
        missing = [x for x in REQUIRED_HEADERS if x not in resp.headers]
        out.append(_r("PR-ALL-04", "Security", not missing, f"missing headers {missing}" if missing else "security headers present"))
    else:
        out.append(_r("PR-ALL-04", "Security", False, "no HTTPS response to inspect"))
    return out


def _resolves_private_only(host: str) -> tuple[bool, str]:
    """PR-C-01: an internal ALB's DNS name has only private addresses (or none) — nothing public to connect to."""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)}
    except OSError:
        return True, "does not resolve from the internet"
    public = sorted(a for a in addrs if not ipaddress.ip_address(a).is_private)
    return not public, f"public addresses {public}" if public else f"private addresses only ({len(addrs)})"


def _port_closed(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return False
    except OSError:
        return True


def validate_container_http(*, base_url: str, health_path: str = "/", alb_dns_name: str = "", task_public_ips=(),
                            app_port: int = 8080, ip_allowlist=(), attempts: int = 20, delay: float = 15,
                            connect_timeout: float = 3) -> list[CheckResult]:
    """base_url: https://<distribution>.cloudfront.net — CloudFront → VPC origin → internal ALB → Fargate (D-33)."""
    out: list[CheckResult] = []
    url = base_url.rstrip("/") + health_path
    if ip_allowlist:
        # SB-PUB-08 / PR-C-07: this checker is not on the allowlist, so the edge must refuse it.
        resp = _get_until(url, 403, attempts, delay)
        blocked = resp is not None and resp.status == 403
        out.append(_r("PR-C-07", "Security", blocked, f"non-allowlisted request -> {getattr(resp, 'status', 'no response')}",
                      "SB-PUB-08"))
        out.append(_r("PR-ALL-02", "Reachability", True, "allowlisted environment: reachability is checked from allowed IPs only",
                      status=NOT_APPLICABLE))
    else:
        out.append(_r("PR-C-07", "Security", True, "public exposure", "SB-PUB-08", status=NOT_APPLICABLE))
        resp = _get_until(url, 200, attempts, delay)  # a new distribution and new tasks take a moment
        ok = resp is not None and resp.status == 200
        # The app answered: not a CloudFront error page, and not the shared ALB's "no environment" default.
        edge_error = resp is not None and ("error" in resp.headers.get("x-cache", "").lower() or resp.body.strip() == b"no environment")
        detail = f"GET {health_path} -> {getattr(resp, 'status', 'no response')}"
        out.append(_r("PR-ALL-02", "Reachability", ok and not edge_error,
                      detail + (" (edge/ALB default response, not the app)" if ok and edge_error else "")))

    h = fetch(base_url.replace("https://", "http://", 1).rstrip("/") + health_path, timeout=5)
    redirect = h.status in (301, 302, 307, 308) and h.headers.get("location", "").startswith("https://")
    out.append(_r("PR-ALL-03", "Security", redirect, f"http -> {h.status} {h.headers.get('location', '')[:60]}", "SB-TLS-01"))

    if resp is not None and resp.status:
        missing = [x for x in REQUIRED_HEADERS if x not in resp.headers]
        out.append(_r("PR-ALL-04", "Security", not missing, f"missing headers {missing}" if missing else "security headers present",
                      "SB-TLS-05"))
    else:
        out.append(_r("PR-ALL-04", "Security", False, "no HTTPS response to inspect", "SB-TLS-05"))

    ok, detail = _resolves_private_only(alb_dns_name) if alb_dns_name else (False, "ALB DNS name unknown")
    out.append(_r("PR-C-01", "Security", ok, f"ALB {detail}", "SB-PUB-02"))

    open_ips = [ip for ip in task_public_ips if not _port_closed(ip, app_port, connect_timeout)]
    out.append(_r("PR-C-02", "Security", not open_ips,
                  f"app port reachable directly on {open_ips}" if open_ips else
                  f"{len(task_public_ips)} task public IP(s): app port {app_port} not reachable from the internet", "SB-PUB-03"))
    return out
