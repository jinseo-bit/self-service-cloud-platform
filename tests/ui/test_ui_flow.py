"""Real-browser UI flow (Chromium via Playwright) against scripts/local_dev.py.

No AWS is called: the local server replaces the AWS boundary (see its docstring).
Screenshots go to build/screenshots/. Run:  python -m pytest tests/ui -q
"""

import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "build" / "screenshots"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    (ROOT / "build").mkdir(exist_ok=True)
    log = open(ROOT / "build" / "local_dev.log", "w", encoding="utf-8")  # never a PIPE: an unread pipe blocks the server
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "local_dev.py"), "--port", str(port), "--delay", "0.6"],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    url = f"http://127.0.0.1:{port}"  # the origin the server prints; localhost is a different browser origin
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/config.js", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError((ROOT / "build" / "local_dev.log").read_text(encoding="utf-8"))
    yield url
    proc.kill()
    log.close()


@pytest.fixture(scope="module")
def site_zip(tmp_path_factory):
    p = tmp_path_factory.mktemp("site") / "site.zip"
    with zipfile.ZipFile(p, "w") as z:
        for f in (ROOT / "examples" / "static-site").iterdir():
            z.write(f, f.name)
    return p


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # chromium not installed
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # HTTP 4xx responses we provoke on purpose are logged by Chromium as "Failed to load resource"; not JS errors.
    pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(f"console: {m.text}"))
    pg.base = server
    yield pg
    ctx.close()
    assert not errors, errors


def wait_result(pg, texts, timeout=30_000):
    """Wait on the result block heading only — the event history also contains status words."""
    js = "(ts) => { const h = document.querySelector('[role=status] .result h2'); return !!h && ts.some((t) => h.innerText.includes(t)); }"
    pg.wait_for_function(js, arg=list(texts), timeout=timeout)


def shot(pg, name, full=True):
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)


def login(pg, email="dev@example.com", admin=False):
    pg.goto(pg.base + "/#/login")
    pg.get_by_label("이메일").fill(email)
    if admin:
        pg.get_by_label("관리자(코치)로 로그인").check()
    pg.get_by_role("button", name="로그인").click()
    try:
        pg.wait_for_selector("text=새 환경 만들기", timeout=15_000)
    except Exception:
        shot(pg, f"debug-login-{email}", full=False)
        raise AssertionError("login did not reach the app shell: " + pg.inner_text("body")[:500]) from None


def answer_wizard(pg, *, preset="포트폴리오 · 블로그", name="내 포트폴리오", overrides=None, snap_prefix=None):
    """Walks every question; overrides: {question title: option label} (multi: list)."""
    overrides = overrides or {}
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name=preset).click()
    for i in range(14):
        title = pg.locator("#q-title").inner_text()
        if title in overrides:
            choice = overrides[title]
            for label in (choice if isinstance(choice, list) else [choice]):
                pg.get_by_role("radio", name=label).or_(pg.get_by_role("checkbox", name=label)).first.click()
        if snap_prefix and i < 3:
            shot(pg, f"{snap_prefix}-q{i + 1}", full=False)
        if title == "마지막으로 확인할게요":
            if name is not None:
                pg.get_by_label("환경 이름").fill(name)
            pg.get_by_role("button", name="추천 받기").click()
            return
        if title == "누가 접속하나요?" and isinstance(overrides.get("_cidrs"), str):
            pg.get_by_label("허용할 IP 주소 범위").fill(overrides["_cidrs"])
        pg.get_by_role("button", name="다음").click()
    raise AssertionError("wizard did not finish")


# ------------------------------------------------------------------ main happy path

def test_static_happy_path_with_delete(page, site_zip):
    pg = page
    pg.goto(pg.base + "/")
    shot(pg, "01-login", full=False)
    login(pg)
    shot(pg, "02-home-empty", full=False)
    assert pg.get_by_text("첫 환경을 만들어 볼까요?").is_visible()

    pg.get_by_role("link", name="＋ 새 환경 만들기").click()
    shot(pg, "03-presets", full=False)
    answer_wizard(pg, snap_prefix="04-wizard")

    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    for text in ("이렇게 고른 이유", "예상 비용", "항상 적용되는 보안 기준", "만든 뒤 자동으로 확인하는 것", "사이트 파일 올리기"):
        assert pg.get_by_role("heading", name=text).is_visible(), text
    # regular users read sentences, never internal reason codes / check ids
    assert pg.get_by_text("화면 파일은 서버 없이 CDN(CloudFront)으로 바로 제공합니다.").is_visible()
    assert pg.get_by_text("SEL_STATIC_FRONTEND").count() == 0
    assert pg.locator(".code-tag").count() == 0
    assert pg.get_by_text("검토자용 상세").count() == 0
    assert pg.get_by_text("예상치예요.").is_visible() and pg.get_by_text("수동 입력").first.is_visible()
    approve = pg.get_by_role("button", name="승인하고 만들기")
    assert approve.is_disabled(), "approve must be disabled until a zip is chosen"
    shot(pg, "05-review")

    pg.set_input_files("#file", str(site_zip))
    assert approve.is_enabled()
    approve.click()
    dialog = pg.get_by_role("dialog")
    dialog.wait_for()
    assert "이 구성으로 만들까요?" in dialog.inner_text()
    shot(pg, "06-approve-confirm", full=False)
    dialog.get_by_role("button", name="승인하고 만들기").click()

    pg.wait_for_selector("text=AWS 리소스 생성", timeout=10_000)
    pg.wait_for_function("document.body.innerText.includes('생성 중') || document.body.innerText.includes('검증 중')", timeout=10_000)
    shot(pg, "07-provisioning", full=False)

    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    shot(pg, "08-ready-detail")
    assert pg.get_by_role("heading", name="검증 결과").is_visible()
    site = pg.get_by_role("link", name="사이트 열기 ↗").get_attribute("href")
    assert "sscp-e2e-marker" in urllib.request.urlopen(site).read().decode()

    pg.get_by_role("button", name="삭제").click()
    d = pg.get_by_role("dialog")
    d.wait_for()
    shot(pg, "09-delete-confirm", full=False)
    d.get_by_role("button", name="삭제").click()
    pg.wait_for_selector("text=삭제된 환경이에요", timeout=20_000)
    shot(pg, "10-deleted", full=False)


# ------------------------------------------------------------------ states

def test_injected_security_failure_shows_failed(page, site_zip):
    pg = page
    login(pg, "fail@example.com")
    answer_wizard(pg, name="fail-demo")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=환경을 만들지 못했어요", timeout=30_000)
    assert pg.get_by_text("보안 검증에 실패해 접속을 차단했습니다.").is_visible()
    assert not pg.get_by_role("button", name="다시 시도").is_visible(), "security failures are not retryable"
    shot(pg, "11-failed-security")


def test_injected_degraded(page, site_zip):
    pg = page
    login(pg, "deg@example.com")
    answer_wizard(pg, name="degraded-demo")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=접속에 문제가 있어요", timeout=30_000)
    shot(pg, "12-degraded")


def test_secret_in_bundle_is_rejected_before_provisioning(page, tmp_path):
    pg = page
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("index.html", "<h1>x</h1>")
        z.writestr("app.js", "const k = 'AKIAABCDEFGHIJKLMNOP';")
    login(pg, "secret@example.com")
    answer_wizard(pg, name="secret-demo")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(bad))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector(".toast", timeout=10_000)
    assert "검사를 통과하지 못했습니다" in pg.locator(".toast").inner_text()
    pg.wait_for_selector("text=아직 승인되지 않았어요")
    shot(pg, "13-artifact-rejected", full=False)


def test_validation_errors_and_cancel(page, site_zip):
    pg = page
    login(pg, "val@example.com")
    answer_wizard(pg, name="", overrides={})
    err = pg.locator("#name-err")
    err.wait_for()
    assert "환경 이름을 입력해 주세요" in err.inner_text()
    assert pg.get_by_label("환경 이름").get_attribute("aria-invalid") == "true"
    pg.get_by_label("한 달 최대 예산 (USD)").fill("-3")
    pg.get_by_label("환경 이름").fill("ok")
    pg.get_by_role("button", name="추천 받기").click()
    assert "정수로" in pg.locator("#budget-err").inner_text()
    shot(pg, "14-validation-errors", full=False)
    # cancel in the approval dialog creates nothing
    pg.get_by_label("한 달 최대 예산 (USD)").fill("5")
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="취소").click()
    assert pg.get_by_role("dialog").count() == 0
    pg.goto(pg.base + "/#/")
    pg.wait_for_selector("text=첫 환경을 만들어 볼까요?")


def test_invalid_cidr_is_blocked(page):
    pg = page
    login(pg, "ip@example.com")
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name="포트폴리오 · 블로그").click()
    while pg.locator("#q-title").inner_text() != "누가 접속하나요?":
        pg.get_by_role("button", name="다음").click()
    pg.get_by_role("radio", name="특정 IP에서만").click()
    pg.get_by_label("허용할 IP 주소 범위").fill("10.0.0.300/8, abc")
    pg.get_by_role("button", name="다음").click()
    assert "형식이 맞지 않아요" in pg.locator("#cidrs-err").inner_text()
    shot(pg, "15-cidr-error", full=False)


def test_unsupported_and_not_yet_provisionable(page):
    pg = page
    login(pg, "u@example.com")
    answer_wizard(pg, name="api", overrides={"어떤 앱을 올리시나요?": "서버 앱",
                                             "가장 오래 걸리는 요청은 얼마나 걸리나요?": "2분 넘게 걸리는 작업도 있어요"})
    pg.wait_for_selector("text=아직 지원하지 않는 요구사항이에요")
    assert pg.get_by_text("2분 넘게 걸리는 요청은").is_visible()
    shot(pg, "16-unsupported", full=False)
    answer_wizard(pg, name="api", overrides={"어떤 앱을 올리시나요?": "서버 앱"})
    pg.wait_for_selector("text=이 구성을 추천해요")
    assert pg.get_by_text("아직 플랫폼에서 만들 수 없어요").is_visible()
    assert pg.get_by_role("heading", name="고르지 않은 구성").is_visible()
    shot(pg, "17-serverless-not-yet")


def test_budget_blocked_shows_relax_options(page):
    pg = page
    login(pg, "b@example.com")
    answer_wizard(pg, name="big", overrides={"하루에 몇 명 정도 쓰나요?": "1만 명 넘게"})
    pg.wait_for_selector("text=예산을 넘어요")
    pg.get_by_role("button", name="예산에 맞게 요구사항 조정").click()
    assert pg.get_by_text("예산을 월").first.is_visible()
    shot(pg, "18-budget-blocked")


def test_prod_needs_admin_then_admin_approves(page, site_zip):
    pg = page
    login(pg, "p@example.com")
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name="포트폴리오 · 블로그").click()
    while pg.locator("#q-title").inner_text() != "마지막으로 확인할게요":
        pg.get_by_role("button", name="다음").click()
    pg.get_by_label("환경 이름").fill("prod-site")
    pg.get_by_role("radio", name="실제 운영용").click()
    pg.get_by_label("한 달 최대 예산 (USD)").fill("20")
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="관리자에게 승인 요청").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인 요청").click()
    pg.wait_for_selector("text=관리자 승인을 기다리고 있어요")
    env_url = pg.url
    shot(pg, "19-pending-admin", full=False)
    pg.get_by_role("link", name="로그아웃").click()
    login(pg, "coach@example.com", admin=True)
    pg.goto(env_url)
    pg.get_by_role("button", name="관리자 승인").click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    shot(pg, "20-admin-approved-ready")


@pytest.mark.parametrize("width", [390, 1024])
def test_responsive_layout_has_no_horizontal_scroll(browser, server, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900}, locale="ko-KR")
    pg = ctx.new_page()
    pg.base = server
    login(pg, f"r{width}@example.com")
    answer_wizard(pg, name="mobile")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    overflow = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    shot(pg, f"21-review-{width}")
    ctx.close()
    assert overflow <= 0, f"horizontal overflow {overflow}px at {width}px"


def test_keyboard_only_wizard(page):
    pg = page
    login(pg, "kbd@example.com")
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name="포트폴리오 · 블로그").focus()
    pg.keyboard.press("Enter")
    pg.wait_for_selector("#q-title")
    pg.get_by_role("radio", name="서버 앱").focus()
    pg.keyboard.press("Space")
    assert pg.get_by_role("radio", name="서버 앱").get_attribute("aria-checked") == "true"


AXE = Path(__file__).with_name("vendor") / "axe.min.js"


def _axe(pg):
    pg.add_script_tag(path=str(AXE))
    res = pg.evaluate("axe.run(document, {resultTypes: ['violations']})")
    return [(v["id"], v["impact"], v["help"], len(v["nodes"])) for v in res["violations"] if v["impact"] in ("serious", "critical")]


@pytest.mark.skipif(not AXE.exists(), reason="axe-core not vendored")
def test_accessibility_no_serious_violations(page, site_zip):
    pg = page
    pg.goto(pg.base + "/#/login")
    pg.wait_for_selector("#email")
    found = {"login": _axe(pg)}
    login(pg, "a11y@example.com")
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name="포트폴리오 · 블로그").click()
    pg.wait_for_selector("#q-title")
    found["question"] = _axe(pg)
    answer_wizard(pg, name="a11y")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    found["review"] = _axe(pg)
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    found["dialog"] = _axe(pg)
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    found["detail"] = _axe(pg)
    assert not any(found.values()), found



@pytest.fixture(scope="module")
def site_zip_v2(tmp_path_factory):
    p = tmp_path_factory.mktemp("site2") / "site-v2.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("index.html", "<!doctype html><title>v2</title><h1>second release</h1><p>sscp-v2-marker</p>")
    return p


def _create_ready(pg, site_zip, name):
    answer_wizard(pg, name=name)
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)


def test_reviewer_sees_reason_codes(page):
    pg = page
    login(pg, "coach2@example.com", admin=True)
    answer_wizard(pg, name="review")
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    assert pg.locator(".code-tag", has_text="SEL_STATIC_FRONTEND").count() == 1
    pg.get_by_text("검토자용 상세").click()
    assert pg.get_by_text("rules_version").is_visible()
    assert pg.get_by_role("cell", name="NONE+NONE").first.is_visible()
    shot(pg, "22-reviewer-panel")


def test_redeploy_and_rollback(page, site_zip, site_zip_v2):
    pg = page
    login(pg, "redeploy@example.com")
    _create_ready(pg, site_zip, "redeploy-demo")
    assert "sscp-e2e-marker" in urllib.request.urlopen(pg.get_by_role("link", name="사이트 열기 ↗").get_attribute("href")).read().decode()
    assert pg.get_by_role("heading", name="새 버전 배포").is_visible()
    checks_before = pg.locator("summary", has_text="검사 기록 전체 보기").inner_text()
    btn = pg.get_by_role("button", name="새 버전 배포하기")
    assert btn.is_disabled()
    pg.set_input_files("#rfile", str(site_zip_v2))
    btn.click()
    d = pg.get_by_role("dialog")
    assert "rev-2" in d.inner_text() and "인프라·비용 변화 없음" in d.inner_text()
    shot(pg, "23-redeploy-confirm", full=False)
    d.get_by_role("button", name="배포하기").click()
    wait_result(pg, ("배포하고 있어요", "검증하고 있어요"))
    shot(pg, "24-redeploying", full=False)
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    url = pg.get_by_role("link", name="사이트 열기 ↗").get_attribute("href")
    assert "sscp-v2-marker" in urllib.request.urlopen(url).read().decode(), "new revision must be served"
    assert pg.get_by_text("rev-2").first.is_visible()
    shot(pg, "25-redeployed")
    pg.get_by_role("button", name="rev-1으로 되돌리기").click()
    pg.get_by_role("dialog").get_by_role("button", name="되돌리기").click()
    try:
        pg.wait_for_function("document.querySelector('.chip.ok') && [...document.querySelectorAll('tr')].some("
                             "(tr) => tr.innerText.includes('rev-1') && tr.innerText.includes('현재'))", timeout=30_000)
    except Exception:
        shot(pg, "debug-rollback")
        raise
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert "sscp-e2e-marker" in urllib.request.urlopen(url).read().decode(), "rollback must serve rev-1 again"
    assert pg.locator("summary", has_text="검사 기록 전체 보기").inner_text() == checks_before, "checks must not pile up per revision"
    shot(pg, "26-rolled-back")


def test_redeploy_rejects_bundle_with_secret(page, site_zip, tmp_path):
    pg = page
    bad = tmp_path / "bad2.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("index.html", "x")
        z.writestr("app.js", "const k = 'AKIAABCDEFGHIJKLMNOP';")
    login(pg, "redeploy-bad@example.com")
    _create_ready(pg, site_zip, "redeploy-bad")
    pg.set_input_files("#rfile", str(bad))
    pg.get_by_role("button", name="새 버전 배포하기").click()
    pg.get_by_role("dialog").get_by_role("button", name="배포하기").click()
    err = pg.locator("#rerr")
    err.wait_for()
    pg.wait_for_function("document.getElementById('rerr').innerText.length > 0", timeout=10_000)
    assert "검사를 통과하지 못했습니다" in err.inner_text()
    assert pg.get_by_text("환경이 준비됐어요").is_visible(), "the served revision stays as it was"



# ------------------------------------------------------------------ identity & stale Service Worker diagnostics

def test_server_identifies_the_platform(server):
    import json as _json
    r = urllib.request.urlopen(server + "/")
    assert r.headers["X-SSCP-App"] == "self-service-cloud-platform"
    assert "<title>Self-Service Cloud Platform</title>" in r.read().decode()
    health = _json.loads(urllib.request.urlopen(server + "/__sscp/health").read())
    assert health["app"] == "self-service-cloud-platform" and health["aws_called"] is False
    assert Path(health["static_root"]) == (ROOT / "web").resolve()


def test_service_worker_script_requests_are_refused(server):
    req = urllib.request.Request(server + "/sw.js", headers={"Service-Worker": "script"})
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    log = (ROOT / "build" / "local_dev.log").read_text(encoding="utf-8")
    assert "Service Worker" in log


# ------------------------------------------------------------------ the full journey in one session

def test_full_user_journey(page, site_zip, site_zip_v2):
    """로그인 → 내 환경 → 프리셋 → 8개 질문 → 추천 → 비용/보안 → 업로드 → 승인 → 생성 시뮬레이션
    → Environment Detail → 재배포 → rollback → 삭제"""
    pg = page
    J = "journey"
    pg.goto(pg.base + "/")
    assert pg.title() == "Self-Service Cloud Platform"
    shot(pg, f"{J}-01-login", full=False)
    login(pg, "journey@example.com")
    pg.get_by_role("heading", name="내 환경").wait_for()
    shot(pg, f"{J}-02-my-environments", full=False)

    pg.get_by_role("link", name="＋ 새 환경 만들기").click()
    pg.get_by_role("link", name="맞춤형으로 만들기").click()
    shot(pg, f"{J}-03-presets", full=False)
    pg.get_by_role("button", name="포트폴리오 · 블로그").click()
    seen = []
    while True:
        title = pg.locator("#q-title").inner_text()
        seen.append(title)
        shot(pg, f"{J}-04-q{len(seen)}", full=False)
        if title == "마지막으로 확인할게요":
            pg.get_by_label("환경 이름").fill("journey-site")
            pg.get_by_role("button", name="추천 받기").click()
            break
        pg.get_by_role("button", name="다음").click()
    assert len(seen) == 8, seen  # static: the capability question is skipped

    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")
    for heading in ("이렇게 고른 이유", "예상 비용", "항상 적용되는 보안 기준", "만든 뒤 자동으로 확인하는 것"):
        assert pg.get_by_role("heading", name=heading).is_visible(), heading
    shot(pg, f"{J}-05-recommendation")

    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    shot(pg, f"{J}-06-approve", full=False)
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_function("['생성 중', '검증 중'].some((t) => document.body.innerText.includes(t))", timeout=10_000)
    shot(pg, f"{J}-07-provisioning", full=False)
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    shot(pg, f"{J}-08-detail")
    site = pg.get_by_role("link", name="사이트 열기 ↗").get_attribute("href")
    assert site.startswith(pg.base), "site link must stay on the same origin as the UI"
    assert "sscp-e2e-marker" in urllib.request.urlopen(site).read().decode()

    pg.set_input_files("#rfile", str(site_zip_v2))
    pg.get_by_role("button", name="새 버전 배포하기").click()
    pg.get_by_role("dialog").get_by_role("button", name="배포하기").click()
    wait_result(pg, ("배포하고 있어요", "검증하고 있어요"))
    shot(pg, f"{J}-09-redeploying", full=False)
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert "sscp-v2-marker" in urllib.request.urlopen(site).read().decode()
    pg.wait_for_timeout(2500)  # longer than one poll interval: a stale response must not bring back "배포 중"
    result = pg.locator("[role=status] .result h2").inner_text()
    assert result == "환경이 준비됐어요", f"stale render brought back: {result}"
    rev2_current = "[...document.querySelectorAll('tr')].some((tr) => tr.innerText.includes('rev-2') && tr.innerText.includes('현재'))"
    assert pg.evaluate(rev2_current)
    shot(pg, f"{J}-10-redeployed")

    pg.get_by_role("button", name="rev-1으로 되돌리기").click()
    pg.get_by_role("dialog").get_by_role("button", name="되돌리기").click()
    rev1_current = "[...document.querySelectorAll('tr')].some((tr) => tr.innerText.includes('rev-1') && tr.innerText.includes('현재'))"
    pg.wait_for_function(rev1_current, timeout=30_000)
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert "sscp-e2e-marker" in urllib.request.urlopen(site).read().decode()
    shot(pg, f"{J}-11-rolled-back")

    pg.get_by_role("button", name="삭제").click()
    pg.get_by_role("dialog").get_by_role("button", name="삭제").click()
    pg.wait_for_selector("text=삭제된 환경이에요", timeout=20_000)
    shot(pg, f"{J}-12-deleted", full=False)



# ------------------------------------------------------------------ D-157..D-162 regressions

def _to_question(pg, title, preset="포트폴리오 · 블로그", app_form=None):
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name=preset).click()
    if app_form:
        pg.get_by_role("radio", name=app_form).click()
    while pg.locator("#q-title").inner_text() != title:
        pg.get_by_role("button", name="다음").click()


def test_request_duration_is_single_choice(page):
    pg = page
    login(pg, "dur@example.com")
    _to_question(pg, "가장 오래 걸리는 요청은 얼마나 걸리나요?", app_form="서버 앱")
    group = pg.get_by_role("radiogroup")
    assert group.get_by_role("radio").count() == 5 and pg.get_by_role("checkbox").count() == 0
    pg.get_by_role("radio", name="30초~1분 걸리는 작업도 있어요").click()
    pg.get_by_role("radio", name="2분 넘게 걸리는 작업도 있어요").click()
    checked = [r.inner_text() for r in pg.get_by_role("radio").all() if r.get_attribute("aria-checked") == "true"]
    assert len(checked) == 1 and "2분 넘게" in checked[0]
    # realtime stays a separate question
    pg.get_by_role("button", name="이전").click()
    assert pg.locator("#q-title").inner_text() == "서버 앱에 이런 기능이 있나요?"
    assert pg.get_by_role("checkbox", name="실시간 연결").count() == 1


def test_unsupported_shows_cause_and_jumps_to_that_question(page):
    pg = page
    login(pg, "jump@example.com")
    answer_wizard(pg, name="long", overrides={"어떤 앱을 올리시나요?": "서버 앱",
                                              "가장 오래 걸리는 요청은 얼마나 걸리나요?": "2분 넘게 걸리는 작업도 있어요"})
    pg.wait_for_selector("text=지원하지 않는 이유")
    assert pg.get_by_text("“2분 넘게 걸리는 작업도 있어요”").is_visible()
    assert pg.get_by_text("요청 접수 → 뒤에서 처리 → 결과 확인").is_visible()
    shot(pg, "27-unsupported-cause")
    pg.get_by_role("button", name="이 답변 바꾸기").click()
    pg.wait_for_selector("#q-title")
    assert pg.locator("#q-title").inner_text() == "가장 오래 걸리는 요청은 얼마나 걸리나요?", "must open the cause, not the last question"
    pg.get_by_role("radio", name="1~2분 걸리는 작업도 있어요").click()
    while pg.locator("#q-title").inner_text() != "마지막으로 확인할게요":
        pg.get_by_role("button", name="다음").click()
    pg.get_by_label("한 달 최대 예산 (USD)").fill("200")
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=이 구성을 추천해요")
    assert pg.get_by_text("CDN 대기 시간을 120초").is_visible()


def test_examples_toggle_and_hint_never_change_the_answer(page):
    pg = page
    login(pg, "ex@example.com")
    _to_question(pg, "첫 요청이 1~2초 느려도 괜찮나요?")
    pg.get_by_role("radio", name="항상 빨라야 해요").click()
    details = pg.locator("details.examples")
    assert details.get_attribute("open") is None, "examples are closed by default"
    assert not pg.get_by_text("로그인 API").is_visible()
    pg.get_by_text("예시 보기").click()
    assert pg.get_by_text("로그인 API").first.is_visible()
    hint = pg.locator("[data-hint]")
    assert hint.get_attribute("data-hint") == "TOLERANT"  # static site → weak hint
    assert pg.get_by_role("radio", name="항상 빨라야 해요").get_attribute("aria-checked") == "true", "hint must not auto-select"
    shot(pg, "28-examples-open", full=False)
    pg.get_by_text("예시 보기").click()
    assert details.get_attribute("open") is None
    # no hint when there is no basis
    pg.get_by_role("button", name="다음").click()
    pg.get_by_text("예시 보기").click()
    assert pg.locator("[data-hint]").count() == 0


def test_home_from_logo_and_unsaved_guard(page):
    pg = page
    login(pg, "home@example.com")
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("link", name="홈으로").click()  # nothing typed yet → no prompt
    pg.wait_for_selector("text=첫 환경을 만들어 볼까요?")
    _to_question(pg, "하루에 몇 명 정도 쓰나요?")
    pg.get_by_role("radio", name="1만 명 이하").click()
    pg.get_by_role("link", name="홈으로").click()
    d = pg.get_by_role("dialog")
    assert "작성 중인 내용이 사라질 수 있어요." in d.inner_text()
    shot(pg, "29-unsaved-guard", full=False)
    d.get_by_role("button", name="계속 작성").click()
    assert pg.locator("#q-title").inner_text() == "하루에 몇 명 정도 쓰나요?"
    pg.get_by_role("link", name="홈으로").click()
    pg.get_by_role("dialog").get_by_role("button", name="나가기").click()
    pg.wait_for_selector("text=첫 환경을 만들어 볼까요?")


def test_home_after_environment_created(page, site_zip):
    pg = page
    login(pg, "home2@example.com")
    _create_ready(pg, site_zip, "home-after")
    pg.get_by_role("link", name="홈으로").click()
    assert pg.get_by_role("dialog").count() == 0
    pg.get_by_role("heading", name="내 환경").wait_for()
    assert pg.get_by_role("link", name="home-after").first.is_visible()


def _over_budget_review(pg, budget="1", scale="1,000명 이하", pattern="하루 종일 꾸준히"):
    pg.goto(pg.base + "/#/new/custom")
    pg.get_by_role("button", name="포트폴리오 · 블로그").click()
    while (t := pg.locator("#q-title").inner_text()) != "마지막으로 확인할게요":
        if t == "하루에 몇 명 정도 쓰나요?":
            pg.get_by_role("radio", name=scale).click()
        if t == "주로 언제 쓰나요?":
            pg.get_by_role("radio", name=pattern).click()
        pg.get_by_role("button", name="다음").click()
    pg.get_by_label("환경 이름").fill("over-budget")
    pg.get_by_label("한 달 최대 예산 (USD)").fill(budget)
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=예산을 넘어요")


def test_budget_adjust_shows_before_after_and_applies_only_on_click(page):
    pg = page
    login(pg, "adj@example.com")
    _to_question(pg, "잠깐 멈추면 어떻게 되나요?", app_form="서버 앱")
    pg.get_by_role("radio", name="절대 멈추면 안 돼요").click()
    pg.get_by_role("button", name="다음").click()
    pg.get_by_role("radio", name="항상 빨라야 해요").click()
    while pg.locator("#q-title").inner_text() != "마지막으로 확인할게요":
        pg.get_by_role("button", name="다음").click()
    pg.get_by_label("환경 이름").fill("adjust")
    pg.get_by_label("한 달 최대 예산 (USD)").fill("3")
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=예산을 넘어요")
    before = pg.locator(".cost-big").inner_text()
    pg.get_by_role("button", name="예산에 맞게 요구사항 조정").click()
    panel = pg.locator("#adjust-panel")
    assert panel.is_visible() and panel.locator(".option").count() >= 1
    assert "아직 아무것도 바뀌지 않았어요" in panel.inner_text()
    assert pg.locator(".cost-big").inner_text() == before, "showing options must not change anything"
    first = panel.locator(".option").first
    assert "현재" in first.inner_text() and "제안" in first.inner_text()
    shot(pg, "30-adjust-options")
    first.get_by_role("button", name="이 변경 적용").click()
    pg.wait_for_selector("[data-adjusted]")
    assert pg.get_by_role("heading", name="요구사항을 바꿨어요").is_visible()
    assert pg.locator(".cost-big").inner_text() != before, "cost is recalculated after applying"
    shot(pg, "31-adjust-applied")


def test_budget_override_confirmation_and_record(page, site_zip):
    pg = page
    login(pg, "ovr@example.com")
    _over_budget_review(pg)
    assert pg.locator("#file").count() == 0, "no upload until the budget is resolved"
    pg.get_by_role("button", name="예산 초과로 진행").click()
    d = pg.get_by_role("dialog")
    text = d.inner_text()
    assert "입력한 예산" in text and "예상 초과" in text and "그래도 이 구성으로 진행하시겠습니까?" in text
    shot(pg, "32-override-confirm", full=False)
    d.get_by_role("button", name="취소").click()
    assert pg.locator("#file").count() == 0, "cancel keeps the guard"
    pg.get_by_role("button", name="예산 초과로 진행").click()
    pg.get_by_role("dialog").get_by_role("button", name="초과 승인하고 계속").click()
    pg.wait_for_selector("[data-override]")
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    dialog = pg.get_by_role("dialog")
    assert "예산 초과 승인됨" in dialog.inner_text()
    dialog.get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert pg.locator("[data-override-detail]").is_visible(), "override is recorded on the environment"
    shot(pg, "33-override-recorded")


def test_platform_hard_cap_is_not_overridable(page, site_zip):
    pg = page
    login(pg, "cap@example.com")
    _over_budget_review(pg, budget="1", scale="1만 명 이하")  # max cost above the $30 platform cap
    pg.get_by_role("button", name="예산 초과로 진행").click()
    pg.get_by_role("dialog").get_by_role("button", name="초과 승인하고 계속").click()
    pg.set_input_files("#file", str(site_zip))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector("text=승인이 막힌 이유", timeout=10_000)
    assert pg.locator(".card", has_text="승인이 막힌 이유").get_by_text("플랫폼 전체 월 상한").is_visible()
    assert pg.get_by_text("아직 승인되지 않았어요").is_visible()
    shot(pg, "34-hard-cap-blocked")


def test_override_does_not_bypass_security_checks(page, tmp_path):
    pg = page
    bad = tmp_path / "secret.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("index.html", "x")
        z.writestr("app.js", "const k = 'AKIAABCDEFGHIJKLMNOP';")
    login(pg, "ovr-sec@example.com")
    _over_budget_review(pg)
    baseline_before = pg.locator("#budget-card ~ .card").first.inner_text()
    pg.get_by_role("button", name="예산 초과로 진행").click()
    pg.get_by_role("dialog").get_by_role("button", name="초과 승인하고 계속").click()
    pg.wait_for_selector("[data-override]")
    assert pg.locator("#budget-card ~ .card").first.inner_text() == baseline_before, "override must not change the baseline"
    pg.set_input_files("#file", str(bad))
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_selector(".toast", timeout=10_000)
    assert "검사를 통과하지 못했습니다" in pg.locator(".toast").inner_text()
    pg.wait_for_selector("text=아직 승인되지 않았어요")



# ------------------------------------------------------------------ Quick Start (D-163) and upload help (D-164)

def _quick_review(pg, name="quick-site", budget=None, tier=None):
    pg.goto(pg.base + "/#/new")
    pg.get_by_role("link", name="빠르게 시작하기").click()
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").click()
    pg.get_by_label("환경 이름").fill(name)
    if budget is not None:
        pg.get_by_label("한 달 최대 예산 (USD, 선택)").fill(budget)
    if tier:
        pg.get_by_role("radio", name=tier).click()
    pg.get_by_role("button", name="검토하기").click()
    pg.wait_for_selector("[data-quick-review]")


def test_start_offers_custom_and_quick_start(page):
    pg = page
    login(pg, "start@example.com")
    pg.get_by_role("link", name="＋ 새 환경 만들기").click()
    pg.get_by_role("heading", name="어떻게 만들까요?").wait_for()
    assert pg.get_by_role("link", name="맞춤형으로 만들기").is_visible()
    assert pg.get_by_text("비용, 보안, 가용성을 고려해서").is_visible()
    assert pg.get_by_role("link", name="빠르게 시작하기").is_visible()
    shot(pg, "35-start-modes", full=False)
    pg.get_by_role("link", name="빠르게 시작하기").click()
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").wait_for()
    assert pg.locator("[data-template]").count() == 1, "only provisionable templates are shown"
    for later in ("웹 API", "백엔드", "컨테이너", "PostgreSQL"):
        assert pg.get_by_text(later).count() == 0, later
    shot(pg, "36-templates", full=False)
    pg.get_by_role("link", name="이전").click()
    pg.get_by_role("link", name="맞춤형으로 만들기").click()
    pg.get_by_role("button", name="포트폴리오 · 블로그").wait_for()


def test_quick_start_static_skips_questions_and_creates(page, site_zip):
    pg = page
    login(pg, "quick@example.com")
    pg.goto(pg.base + "/#/new/quick")
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").click()
    assert pg.get_by_role("progressbar").count() == 0 and pg.get_by_text("어떤 앱을 올리시나요?").count() == 0, "no 8-question wizard"
    assert pg.get_by_role("radio", name="개발 · 테스트용").get_attribute("aria-checked") == "true"
    shot(pg, "37-quick-form", full=False)
    pg.get_by_role("button", name="검토하기").click()
    assert pg.get_by_text("환경 이름을 입력해 주세요.").is_visible()
    pg.get_by_label("환경 이름").fill("quick-site")
    pg.get_by_role("button", name="검토하기").click()
    pg.wait_for_selector("[data-quick-review]")
    assert pg.get_by_role("heading", name="나만의 정적 웹사이트", exact=True).is_visible()
    card = pg.locator("[data-quick-review]")
    assert "S3" in card.inner_text() and "웹 파일 저장" in card.inner_text()
    assert "CloudFront" in card.inner_text() and "빠르고 안전하게 웹사이트 제공" in card.inner_text()
    for heading in ("사용할 구성", "예상 비용", "항상 적용되는 보안 기준", "만든 뒤 자동으로 확인하는 것", "DEV / PROD 정책"):
        assert pg.get_by_role("heading", name=heading).is_visible(), heading
    assert "7일 뒤 자동 삭제" in pg.locator("[data-ttl]").inner_text()
    make = pg.get_by_role("button", name="이 구성으로 만들기")
    assert make.is_disabled(), "artifact required"
    shot(pg, "38-quick-review")
    pg.set_input_files("#file", str(site_zip))
    make.click()
    pg.get_by_role("dialog").get_by_role("button", name="이 구성으로 만들기").click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert "sscp-e2e-marker" in urllib.request.urlopen(pg.get_by_role("link", name="사이트 열기 ↗").get_attribute("href")).read().decode()
    pg.get_by_role("link", name="홈으로").click()
    assert pg.get_by_role("dialog").count() == 0
    pg.get_by_role("heading", name="내 환경").wait_for()
    assert pg.get_by_role("link", name="quick-site").first.is_visible()


def test_quick_start_switches_to_custom_with_template_defaults(page):
    pg = page
    login(pg, "quick2@example.com")
    _quick_review(pg, name="tpl-to-custom", budget="8")
    pg.get_by_role("button", name="세부 요구사항 바꾸기").click()
    pg.wait_for_selector("#q-title")
    assert pg.locator("[data-from-template]").is_visible()
    expected = {"어떤 앱을 올리시나요?": "화면만 있는 사이트", "하루에 몇 명 정도 쓰나요?": "100명 이하",
                "주로 언제 쓰나요?": "거의 안 써요", "데이터를 저장해야 하나요?": "저장할 데이터 없음",
                "잠깐 멈추면 어떻게 되나요?": "잠깐 멈춰도 괜찮아요", "첫 요청이 1~2초 느려도 괜찮나요?": "괜찮아요",
                "누가 접속하나요?": "누구나"}
    seen = set()
    while (t := pg.locator("#q-title").inner_text()) != "마지막으로 확인할게요":
        if t in expected:
            assert pg.get_by_role("radio", name=expected[t]).get_attribute("aria-checked") == "true", t
            seen.add(t)
        pg.get_by_role("button", name="다음").click()
    assert seen == set(expected)
    assert pg.get_by_label("환경 이름").input_value() == "tpl-to-custom"
    assert pg.get_by_label("한 달 최대 예산 (USD)").input_value() == "8"
    pg.get_by_role("button", name="추천 받기").click()
    pg.wait_for_selector("text=정적 사이트 구성을 추천해요")  # back on the custom review, same engine
    assert pg.locator("[data-quick-review]").count() == 0


def test_quick_start_reviewer_sees_template_origin(page):
    pg = page
    login(pg, "coach-q@example.com", admin=True)
    _quick_review(pg, name="origin")
    pg.get_by_text("검토자용 상세").click()
    assert '"kind":"TEMPLATE"' in pg.locator("details.reviewer").inner_text()
    assert "STATIC_WEBSITE" in pg.locator("details.reviewer").inner_text()


def test_quick_start_unsaved_guard(page):
    pg = page
    login(pg, "quick3@example.com")
    pg.goto(pg.base + "/#/new/quick")
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").click()
    pg.get_by_label("환경 이름").fill("half")
    pg.get_by_role("link", name="홈으로").click()
    d = pg.get_by_role("dialog")
    assert "작성 중인 내용이 사라질 수 있어요." in d.inner_text()
    d.get_by_role("button", name="나가기").click()
    pg.wait_for_selector("text=첫 환경을 만들어 볼까요?")


def test_failed_upload_explains_cause_and_allows_reupload(page, site_zip, tmp_path):
    pg = page
    app = tmp_path / "router.zip"
    with zipfile.ZipFile(app, "w") as z:
        z.writestr("router.exe", "MZ")
        z.writestr("libcef.dll", "x")
        z.writestr("locales/ko.pak", "x")
    login(pg, "upl@example.com")
    _quick_review(pg, name="wrong-zip")
    pg.set_input_files("#file", str(app))
    pg.get_by_role("button", name="이 구성으로 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="이 구성으로 만들기").click()
    pg.wait_for_selector("[data-artifact-help]", timeout=10_000)
    assert "index.html이 있는지" in pg.locator(".toast").inner_text(), "the toast names the failed check"
    help_ = pg.locator("[data-artifact-help]")
    assert "웹사이트 빌드 결과가 아닌 것 같아요" in help_.inner_text()
    assert "dist/" in help_.inner_text() and ".exe 1개" in pg.locator("[data-found]").inner_text()
    shot(pg, "39-upload-help")
    # the detail page's own approve button also says why
    pg.get_by_role("button", name="승인하고 만들기").click()
    pg.wait_for_function("document.querySelector('.toast')?.innerText.includes('index.html이 있는지')", timeout=10_000)
    # The detail page re-renders after the toast; a file chosen before that render is dropped, so pick again until enabled.
    reupload = pg.get_by_role("button", name="이 파일로 다시 검사하고 승인하기")
    for _ in range(5):
        pg.wait_for_load_state("networkidle")
        pg.set_input_files("#dfile", str(site_zip))
        pg.wait_for_timeout(300)
        if reupload.is_enabled():
            break
    reupload.click()
    pg.wait_for_selector("text=환경이 준비됐어요", timeout=30_000)
    assert pg.locator("[data-artifact-help]").count() == 0


@pytest.mark.skipif(not AXE.exists(), reason="axe-core not vendored")
def test_accessibility_quick_start_and_upload_help(page, tmp_path):
    pg = page
    bad = tmp_path / "noindex.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("app.exe", "MZ")
    login(pg, "a11y-q@example.com")
    pg.goto(pg.base + "/#/new")
    pg.get_by_role("link", name="빠르게 시작하기").wait_for()
    found = {"start": _axe(pg)}
    pg.get_by_role("link", name="빠르게 시작하기").click()
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").wait_for()
    found["templates"] = _axe(pg)
    pg.get_by_role("button", name="나만의 정적 웹사이트 만들기").click()
    pg.get_by_label("환경 이름").wait_for()
    found["quick-form"] = _axe(pg)
    pg.get_by_label("환경 이름").fill("a11y-quick")
    pg.get_by_role("button", name="검토하기").click()
    pg.wait_for_selector("[data-quick-review]")
    found["quick-review"] = _axe(pg)
    pg.set_input_files("#file", str(bad))
    pg.get_by_role("button", name="이 구성으로 만들기").click()
    pg.get_by_role("dialog").get_by_role("button", name="이 구성으로 만들기").click()
    pg.wait_for_selector("[data-artifact-help]", timeout=10_000)
    pg.wait_for_load_state("networkidle")
    found["upload-help"] = _axe(pg)
    assert not any(found.values()), found
