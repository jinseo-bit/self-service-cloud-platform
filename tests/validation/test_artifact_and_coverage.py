import zipfile

from platform_core import baseline
from platform_core.validation.artifact import validate_static_bundle
from platform_core.validation.result import FAIL, PASS


def make_zip(tmp_path, files):
    p = tmp_path / "site.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return str(p)


def status(results, cid):
    return {r.status for r in results if r.check_id == cid}


def test_index_fingerprint(tmp_path):
    import hashlib

    from platform_core.validation.artifact import index_sha256
    p = make_zip(tmp_path, {"dist/index.html": "<h1>x</h1>"})
    assert index_sha256(p, "dist/") == hashlib.sha256(b"<h1>x</h1>").hexdigest()
    assert index_sha256(p, "") is None


def test_clean_bundle_passes(tmp_path):
    results, root = validate_static_bundle(make_zip(tmp_path, {"index.html": "<h1>hi</h1>", "app.js": "console.log(1)"}))
    assert all(r.status == PASS for r in results) and root == ""


def test_nested_folder_bundle(tmp_path):
    results, root = validate_static_bundle(make_zip(tmp_path, {"dist/index.html": "x", "dist/a.js": "y"}))
    assert root == "dist/" and status(results, "ART-INDEX") == {PASS}


def test_negative_secret_in_bundle(tmp_path):
    results, _ = validate_static_bundle(make_zip(tmp_path, {
        "index.html": "x", "config.js": "const k='AKIAABCDEFGHIJKLMNOP';"}))
    assert status(results, "SB-SEC-04") == {FAIL}


def test_negative_missing_index_and_path_traversal(tmp_path):
    results, _ = validate_static_bundle(make_zip(tmp_path, {"../evil.html": "x"}))
    assert status(results, "ART-INDEX") == {FAIL}
    assert status(results, "ART-PATH") == {FAIL}


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_text("nope")
    results, _ = validate_static_bundle(str(p))
    assert status(results, "ART-ZIP") == {FAIL}


# Every baseline item that applies to a Static environment and is checkable by an
# automated layer must have a validator emitting that baseline id.
STATIC_VALIDATORS = {
    "SB-IAM-02": "plan", "SB-ENC-01": "plan,state", "SB-TLS-01": "plan,state,http", "SB-TLS-04": "plan,state",
    "SB-TLS-05": "plan,state,http", "SB-SEC-04": "artifact", "SB-PUB-01": "plan,state,http",
    "SB-PUB-09": "plan", "SB-PUB-10": "plan", "SB-LOG-02": "plan,state", "SB-TAG-01": "plan,state",
    "SB-PUB-08": "plan,state,http",
}


def test_static_baseline_coverage_is_complete():
    applicable = set(baseline.applicable({"S", "IP"}))
    assert applicable == set(STATIC_VALIDATORS), applicable ^ set(STATIC_VALIDATORS)


def test_every_listed_static_production_ready_check_is_emitted(tmp_path):
    """docs/production-ready.md ↔ validators: each check shown to the user is really computed."""
    from platform_core.production_ready import STATIC_CHECKS
    from platform_core.validation.plan import validate_plan
    from tests.validation.test_state_validator import run as run_state

    emitted = {r.check_id for r in run_state()} | {"PR-ALL-06"}  # see test_prod_monitoring_check
    emitted |= {r.check_id for r in validate_static_bundle(make_zip(tmp_path, {"index.html": "x"}))[0]}
    emitted |= {r.check_id for r in validate_plan({"resource_changes": []})}
    emitted |= {"PR-ALL-02", "PR-ALL-03", "PR-ALL-04", "PR-S-01", "PR-S-02", "PR-S-04"} & {
        r.check_id for r in __import__("platform_core.validation.http", fromlist=["x"]).validate_static_http(
            base_url="http://127.0.0.1:9", direct_origin_url="http://127.0.0.1:9/x", spa_fallback=True,
            ip_allowlist=[], attempts=1, delay=0)}
    emitted |= {r.check_id for r in __import__("platform_core.validation.http", fromlist=["x"]).validate_static_http(
        base_url="http://127.0.0.1:9", direct_origin_url=None, spa_fallback=False, ip_allowlist=["10.0.0.0/8"],
        attempts=1, delay=0)}
    from platform_core.validation.http import check_served_revision
    emitted.add(check_served_revision(base_url="http://127.0.0.1:9", expected_sha256="x", revision=1, attempts=1, delay=0).check_id)
    missing = {c[0] for c in STATIC_CHECKS} - emitted
    assert not missing, missing


def test_every_emitted_check_and_baseline_item_has_plain_wording(tmp_path, static_plan, violating_plan):
    """The UI never shows a bare technical id: every id a Static validator emits has a non-expert title."""
    from platform_core.production_ready import CHECK_TITLES
    from platform_core.validation.http import validate_static_http
    from platform_core.validation.plan import validate_plan
    from tests.validation.test_state_validator import run as run_state

    ids = {r.check_id for r in run_state()} | {"PR-ALL-06"}
    ids |= {r.check_id for r in validate_static_bundle(make_zip(tmp_path, {"index.html": "x"}))[0]}
    notzip = tmp_path / "not.zip"
    notzip.write_text("x")
    ids |= {r.check_id for r in validate_static_bundle(str(notzip))[0]}
    ids |= {r.check_id for r in validate_plan(static_plan)} | {r.check_id for r in validate_plan(violating_plan)}
    ids |= {r.check_id for r in validate_static_http(base_url="http://127.0.0.1:9", direct_origin_url="http://127.0.0.1:9/x",
                                                       spa_fallback=True, ip_allowlist=[], attempts=1, delay=0)}
    ids |= {r.check_id for r in validate_static_http(base_url="http://127.0.0.1:9", direct_origin_url=None, spa_fallback=False,
                                                       ip_allowlist=["10.0.0.0/8"], attempts=1, delay=0)}
    assert not ids - set(CHECK_TITLES), ids - set(CHECK_TITLES)
    assert all(i.plain for i in baseline.ITEMS)
