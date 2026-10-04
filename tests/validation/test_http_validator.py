"""HTTP validator against real local HTTP servers (compliant and violating behaviour)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from platform_core.validation.http import validate_static_http
from platform_core.validation.result import FAIL, NOT_APPLICABLE, PASS, verdict

SEC_HEADERS = {"Strict-Transport-Security": "max-age=31536000", "X-Content-Type-Options": "nosniff",
               "X-Frame-Options": "SAMEORIGIN"}


def make_server(handler_fn):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = handler_fn(self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def servers():
    started = []

    def start(fn):
        srv, url = make_server(fn)
        started.append(srv)
        return url

    yield start
    for s in started:
        s.shutdown()


def compliant_site(path):
    if path.startswith("/__sscp_probe/"):
        return 200, SEC_HEADERS, b"<html>index</html>"
    return 200, SEC_HEADERS, b"<html>ok</html>"


def run(servers, site=compliant_site, plain=None, origin_status=403, spa=False, allowlist=()):
    https_like = servers(site)
    redirect_target = https_like.replace("http://", "https://")
    plain = plain or (lambda p: (301, {"Location": redirect_target + p}, b""))
    http_like = servers(plain)
    origin = servers(lambda p: (origin_status, {}, b""))
    return validate_static_http(base_url=https_like, http_url=http_like, direct_origin_url=origin + "/index.html",
                                spa_fallback=spa, ip_allowlist=list(allowlist), attempts=2, delay=0.05)


def by(results, cid):
    return {r.status for r in results if r.check_id == cid}


def test_compliant_site_is_ready(servers):
    results = run(servers, spa=True)
    assert verdict(results)["verdict"] == "READY", [r.to_dict() for r in results]


def test_missing_security_headers_fails(servers):
    results = run(servers, site=lambda p: (200, {}, b"ok"))
    assert by(results, "PR-ALL-04") == {FAIL}
    assert verdict(results)["verdict"] == "FAILED"


def test_http_served_without_redirect_fails(servers):
    results = run(servers, plain=lambda p: (200, {}, b"plain http"))
    assert by(results, "PR-ALL-03") == {FAIL}
    assert verdict(results)["security_failed"]


def test_publicly_readable_origin_fails(servers):
    results = run(servers, origin_status=200)
    assert by(results, "PR-S-01") == {FAIL}
    assert verdict(results)["verdict"] == "FAILED"


def test_app_error_is_degraded_not_failed(servers):
    results = run(servers, site=lambda p: (500, SEC_HEADERS, b"boom"))
    assert by(results, "PR-ALL-02") == {FAIL}
    assert verdict(results)["verdict"] == "DEGRADED"


def test_missing_spa_fallback_is_degraded(servers):
    results = run(servers, site=lambda p: (404, SEC_HEADERS, b"") if p.startswith("/__sscp") else (200, SEC_HEADERS, b"ok"),
                  spa=True)
    assert by(results, "PR-S-02") == {FAIL}
    assert verdict(results)["verdict"] == "DEGRADED"


def test_allowlist_must_reject_outsiders(servers):
    ok = run(servers, site=lambda p: (403, {}, b""), allowlist=["203.0.113.0/24"])
    assert by(ok, "PR-S-04") == {PASS} and by(ok, "PR-ALL-02") == {NOT_APPLICABLE}
    leak = run(servers, allowlist=["203.0.113.0/24"])
    assert by(leak, "PR-S-04") == {FAIL}


def test_unreachable_endpoint_is_a_result_not_a_crash():
    results = validate_static_http(base_url="http://127.0.0.1:9", direct_origin_url="http://127.0.0.1:9/x",
                                   spa_fallback=False, ip_allowlist=[], attempts=1, delay=0)
    assert by(results, "PR-ALL-02") == {FAIL}
    assert by(results, "PR-ALL-03") == {FAIL}
    assert verdict(results)["verdict"] == "FAILED"



def test_served_revision_matches_fingerprint(servers):
    import hashlib

    from platform_core.validation.http import check_served_revision
    body = b"<html>rev-2</html>"
    url = servers(lambda p: (200, SEC_HEADERS, body))
    ok = check_served_revision(base_url=url, expected_sha256=hashlib.sha256(body).hexdigest(), revision=2, attempts=1, delay=0)
    assert ok.status == PASS
    stale = check_served_revision(base_url=url, expected_sha256=hashlib.sha256(b"<html>rev-1</html>").hexdigest(), revision=2,
                                  attempts=2, delay=0.01)
    assert stale.status == FAIL and stale.dimension == "Operability"
    assert verdict([stale])["verdict"] == "FAILED"
