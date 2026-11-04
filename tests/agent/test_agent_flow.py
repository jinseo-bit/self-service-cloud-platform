"""The agent end to end against scripts/local_dev.py (real API handler, engine, validators; no AWS) -- D-181.

The agent signs in with its own client, analyzes a repo, gets a recommendation and creates a DRAFT. It can upload a
site into its DRAFT. It can NOT approve, delete, retry, redeploy, extend or agree to go over budget: the API refuses
the agent's token. A person approves with the UI's token and the environment becomes READY.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent import platform_client, tools

ROOT = Path(__file__).resolve().parents[2]
REQ = {"traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "downtime_tolerance": "OK",
       "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 30}


@pytest.fixture(scope="module")
def server():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (ROOT / "build").mkdir(exist_ok=True)
    log = open(ROOT / "build" / "local_dev_agent.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "local_dev.py"), "--port", str(port), "--delay", "0.3"],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/__sscp/health", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    yield url
    proc.kill()
    log.close()


@pytest.fixture
def agent(server, tmp_path, monkeypatch):
    monkeypatch.setattr(platform_client, "CONFIG", tmp_path / "agent.json")
    platform_client.login(email="dev@example.com", local=server)
    return platform_client.PlatformClient()


def human(server, method, path, body=None, email="dev@example.com"):
    """The same person, signed in through the web UI's client."""
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(
        server + "/local/auth", data=json.dumps({"email": email}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")).read())["IdToken"]
    req = urllib.request.Request(f"{server}/api{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": tok, "Content-Type": "application/json"}, method=method)
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait(agent, env_id, statuses, timeout=40):
    for _ in range(int(timeout / 0.5)):
        st = tools.get_environment(env_id)
        if st["status"] in statuses:
            return st
        time.sleep(0.5)
    raise AssertionError(f"{env_id} stuck in {st['status']}")


def test_static_repo_from_analysis_to_human_approval(agent, server):
    site = ROOT / "examples" / "static-site"
    a = tools.analyze_repo(str(site))
    assert a["draft"]["app_form"] == "STATIC_SITE" and a["confirm"] == [] and a["unsupported"] == []
    assert set(a["questions"]) >= {"traffic_scale", "exposure", "tier"}, "what code cannot tell is asked, never guessed"

    incomplete = tools.recommend(a["draft"])
    assert incomplete["error"] and "traffic_scale" in incomplete["missing"]

    req = {**a["draft"], **REQ}
    rec = tools.recommend(req)
    assert rec["run_model"] == "정적 호스팅" and rec["provisionable"] and rec["artifact"] == "STATIC_ZIP"

    created = tools.create_pending_environment("agent-site", req)
    env_id = created["env_id"]
    assert created["status"] == "DRAFT" and created["needs_site_upload"] and env_id in created["approve_here"]
    assert tools.upload_site(env_id, str(site))["uploaded_bytes"] > 0

    # the agent cannot approve: the API refuses its token (not just a missing tool)
    with pytest.raises(platform_client.PlatformError) as e:
        agent.call("POST", f"/environments/{env_id}/approve")
    assert e.value.status == 403 and e.value.detail.get("human_action_required")

    st = tools.get_environment(env_id)
    assert st["status"] == "DRAFT" and st["created_via"] == "agent"
    assert any("agent" in m for m in st["last_events"]), "the audit trail says who drafted it"

    code, _ = human(server, "POST", f"/environments/{env_id}/approve")
    assert code == 202
    ready = wait(agent, env_id, {"READY", "DEGRADED", "FAILED"})
    assert ready["status"] == "READY" and ready["url"]
    assert tools.explain_failure(env_id)["explanation"] == "실패한 환경이 아니에요."
    listed = {e["env_id"]: e for e in tools.list_environments()["environments"]}
    assert listed[env_id]["created_via"] == "agent", "found in a manual run: the list dropped this field"
    human(server, "DELETE", f"/environments/{env_id}")


def test_agent_token_cannot_change_or_spend(agent, server):
    req = {"app_form": "SERVER_APP", "capabilities": {}, "data": {"needed": False}, **REQ}
    created = tools.create_pending_environment("agent-api", req, image="ghcr.io/mccutchen/go-httpbin:v2.16.1",
                                               health_path="/status/200")
    env_id = created["env_id"]
    assert created["status"] == "DRAFT" and not created["needs_site_upload"]
    for method, path in [("POST", f"/environments/{env_id}/approve"), ("DELETE", f"/environments/{env_id}"),
                         ("POST", f"/environments/{env_id}/retry"), ("POST", f"/environments/{env_id}/redeploy"),
                         ("POST", f"/environments/{env_id}/extend")]:
        with pytest.raises(platform_client.PlatformError) as e:
            agent.call(method, path)
        assert e.value.status == 403, (method, path, e.value.message)
    # agreeing to go over budget is a person's decision too
    over = {**req, "budget_usd_month": 1, "capabilities": {"realtime_connection": True}}
    with pytest.raises(platform_client.PlatformError) as e:
        agent.call("POST", "/environments", {"name": "x", "input": over, "budget_override": True,
                                             "image": {"image": "ghcr.io/mccutchen/go-httpbin:v2.16.1"}})
    assert e.value.status == 403
    assert human(server, "DELETE", f"/environments/{env_id}")[0] == 200, "the person can still delete the agent's draft"


def test_admin_power_does_not_pass_to_the_agent(server, tmp_path, monkeypatch):
    monkeypatch.setattr(platform_client, "CONFIG", tmp_path / "agent.json")
    status, raw = platform_client._http("POST", server + "/local/auth",
                                        json.dumps({"email": "coach@example.com", "admin": True, "client": "agent"}).encode(),
                                        {"Content-Type": "application/json"})
    platform_client.save_config({"mode": "local", "api_url": server + "/api", "ui_url": server,
                                 "id_token": json.loads(raw)["IdToken"], "id_token_exp": 0})
    me = platform_client.PlatformClient().call("GET", "/me")
    assert me["agent"] is True and me["admin"] is False


def test_expired_refresh_token_asks_to_sign_in_again(tmp_path, monkeypatch):
    """AWS 2026-11-04: a 4-day-old login surfaced Cognito's raw 'Refresh Token has expired'."""
    path = tmp_path / "agent.json"
    platform_client.save_config({"mode": "cognito", "api_url": "https://api.example", "region": "ap-northeast-2",
                                 "client_id": "c", "email": "dev@example.com", "id_token": "x", "id_token_exp": 0,
                                 "refresh_token": "old"}, path)

    def expired(region, action, payload):
        raise platform_client.PlatformError(400, "Refresh Token has expired")
    monkeypatch.setattr(platform_client, "_cognito", expired)
    with pytest.raises(platform_client.NotLoggedIn) as e:
        platform_client.PlatformClient(path).call("GET", "/me")
    assert "python -m agent.login" in e.value.message


def test_secret_env_vars_are_rejected_for_the_agent_too(agent):
    req = {"app_form": "SERVER_APP", "capabilities": {}, "data": {"needed": False}, **REQ}
    out = tools.create_pending_environment("leak", req, image="ghcr.io/mccutchen/go-httpbin:v2.16.1",
                                           env_vars={"DB_PASSWORD": "hunter2"})
    assert "error" in out and "env_id" not in out


def test_mcp_server_exposes_only_the_safe_tools():
    """Over the real MCP stdio protocol: the tool list is what Claude Code sees."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent.mcp_server"], cwd=str(ROOT))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            init = await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            res = await s.call_tool("analyze_repo", {"path": str(ROOT / "examples" / "static-site")})
            return init, names, res

    init, names, res = anyio.run(run)
    assert names == {"analyze_repo", "recommend", "create_pending_environment", "upload_site", "get_environment",
                     "list_environments", "explain_failure", "recheck_environment"}
    assert not any(w in n for n in names for w in ("approve", "delete", "retry", "redeploy", "extend", "repair"))
    assert "approve" in (init.instructions or "") and not res.isError
    assert json.loads(res.content[0].text)["draft"]["app_form"] == "STATIC_SITE"


def test_realtime_app_recommendation_lists_each_run_model_once(agent):
    """Manual run 2026-10-27: Lambda and pre-warmed Lambda both showed up as "요청 단위 실행"."""
    req = {"app_form": "SERVER_APP", "capabilities": {"realtime_connection": True}, "data": {"needed": False}, **REQ}
    rec = tools.recommend(req)
    labels = [c["run_model"] for c in rec["not_chosen"]]
    assert rec["run_model"] == "상시 실행 컨테이너" and len(labels) == len(set(labels)) and "요청 단위 실행" in labels


def test_security_failure_is_explained(server, tmp_path, monkeypatch):
    """Manual run 2026-10-27: a validation (D-65) block has no `failure`, only `verdict`; it must still be explained."""
    monkeypatch.setattr(platform_client, "CONFIG", tmp_path / "agent.json")
    platform_client.login(email="fail@example.com", local=server)  # local_dev injects a security failure for this owner
    req = {"app_form": "STATIC_SITE", "data": {"needed": False}, **REQ}
    env_id = tools.create_pending_environment("fail", req)["env_id"]
    tools.upload_site(env_id, str(ROOT / "examples" / "static-site"))
    assert human(server, "POST", f"/environments/{env_id}/approve", email="fail@example.com")[0] == 202
    for _ in range(80):
        if tools.get_environment(env_id)["status"] == "FAILED":
            break
        time.sleep(0.5)
    why = tools.explain_failure(env_id)
    assert why["stage"] == "SECURITY" and "재시도 대상이 아니에요" in why["what_to_do"][0]
    assert why["failed_checks"] and all(s for s in why["what_to_do"])


# ------------------------------------------------------------------ Day-2: drift -> plan -> human repair (D-186)

def _post(server, path, body=None):
    req = urllib.request.Request(server + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def _until(fn, cond, timeout=30):
    for _ in range(int(timeout / 0.3)):
        v = fn()
        if cond(v):
            return v
        time.sleep(0.3)
    raise AssertionError(f"timed out: {v}")


def test_console_change_is_detected_planned_and_repaired_only_by_a_person(agent, server):
    req = {"app_form": "STATIC_SITE", "data": {"needed": False}, **REQ}
    env_id = tools.create_pending_environment("day2", req)["env_id"]
    tools.upload_site(env_id, str(ROOT / "examples" / "static-site"))
    assert human(server, "POST", f"/environments/{env_id}/approve")[0] == 202
    _until(lambda: tools.get_environment(env_id)["status"], lambda s: s == "READY")

    # someone opens the bucket policy in the console -> CloudTrail write event -> recheck
    _post(server, f"/local/drift/{env_id}")
    env = human(server, "GET", f"/environments/{env_id}")[1]["environment"]
    assert env["status"] == "DRIFTED"
    d = env["drift"]
    assert d["trigger"] == "EVENT" and d["event_name"] == "PutBucketPolicy" and d["actor"].endswith("local-console-user")
    assert d["security"] and d["endpoint_disabled"], "a security drift is blocked before anyone approves anything (D-65)"
    assert d["detect_seconds"] is not None

    # the platform prepares a plan back to the approved configuration; nothing is applied yet
    plan = _until(lambda: human(server, "GET", f"/environments/{env_id}")[1]["environment"].get("repair_plan"),
                  lambda p: bool(p and p.get("stacks")))
    assert plan["passed"] and plan["changes"] == 1 and plan["stacks"][0]["changes"][0]["address"] == "aws_s3_bucket_policy.origin"
    assert tools.get_environment(env_id)["status"] == "DRIFTED"

    # the agent can look and recheck, but never repair
    with pytest.raises(platform_client.PlatformError) as e:
        agent.call("POST", f"/environments/{env_id}/repair")
    assert e.value.status == 403
    assert agent.call("POST", f"/environments/{env_id}/recheck")["environment"]["env_id"] == env_id
    # D-188 (AWS E2E 2026-10-28): the recheck sees the blocked endpoint fail; that is not a second drift
    again = _until(lambda: human(server, "GET", f"/environments/{env_id}")[1]["environment"],
                   lambda x: (x.get("last_recheck") or {}).get("trigger") == "MANUAL")
    assert again["status"] == "DRIFTED" and "PR-ALL-02/enabled" in again["last_recheck"]["failed"]
    assert again["drift"]["event_name"] == "PutBucketPolicy" and len(again["drift_history"]) == 1
    why = tools.explain_failure(env_id)
    assert why["stage"] == "DRIFT" and "PutBucketPolicy" in json.dumps(why, ensure_ascii=False)

    # a person approves the plan -> applied -> validated -> READY, with the Day-2 metrics recorded
    assert human(server, "POST", f"/environments/{env_id}/repair")[0] == 202
    done = _until(lambda: human(server, "GET", f"/environments/{env_id}")[1]["environment"], lambda x: x["status"] == "READY")
    assert done.get("drift") is None and done["last_repair"]["approved_by"] == "dev@example.com"
    assert done["last_repair"]["repair_seconds"] is not None and done["last_repair"]["detect_seconds"] is not None
    assert done["last_repair"]["wait_seconds"] is not None and done["last_repair"]["apply_seconds"] is not None
    human(server, "DELETE", f"/environments/{env_id}")


def test_repair_needs_a_drifted_environment_with_a_plan(agent, server):
    req = {"app_form": "STATIC_SITE", "data": {"needed": False}, **REQ}
    env_id = tools.create_pending_environment("day2-guard", req)["env_id"]
    tools.upload_site(env_id, str(ROOT / "examples" / "static-site"))
    human(server, "POST", f"/environments/{env_id}/approve")
    _until(lambda: tools.get_environment(env_id)["status"], lambda s: s == "READY")
    code, body = human(server, "POST", f"/environments/{env_id}/repair")
    assert code == 409 and "설정 변경이 감지된 환경만" in body["error"]
    human(server, "DELETE", f"/environments/{env_id}")
