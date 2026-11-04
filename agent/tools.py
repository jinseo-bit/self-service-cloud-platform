"""Agent tools (D-181), plain functions so they can be tested without MCP; agent/mcp_server.py exposes them.

What they can do: read a repo, ask the platform for a recommendation, create a DRAFT environment, upload a site zip
into a DRAFT, read status, explain a failure. What they cannot do: approve, delete, retry, redeploy, or agree to go
over budget -- the API refuses those for the agent's token, and no tool here even tries.
"""

from __future__ import annotations

from pathlib import Path

from agent.platform_client import PlatformClient, PlatformError, zip_dir
from agent.repo_analyzer import DEVELOPER_ONLY, analyze

STATIC_LABEL = "정적 호스팅"  # D-182; the compute labels below never include NONE (that is the static frontend)
RUN_MODEL = {"SLS": "요청 단위 실행", "SLS_PC": "요청 단위 실행", "CTR": "상시 실행 컨테이너"}
QUESTIONS = {  # what to ask the developer, in the platform UI's own words
    "traffic_scale": "하루에 몇 명 정도 쓰나요? (T1 100명 이하 / T2 1,000명 이하 / T3 1만 명 이하 / T4 그 이상)",
    "traffic_pattern": "주로 언제 쓰나요? (IDLE_MOSTLY 거의 안 씀 / BUSINESS_HOURS 업무시간 / STEADY 하루 종일 / SPIKY 갑자기 몰림)",
    "downtime_tolerance": "잠깐 멈추면 어떻게 되나요? (OK 괜찮음 / NOT_IN_BUSINESS_HOURS 업무시간엔 안 됨 / NEVER 절대 안 됨)",
    "latency_sensitivity": "첫 요청이 1~2초 느려도 괜찮나요? (TOLERANT 괜찮음 / SENSITIVE 항상 빨라야 함)",
    "exposure": "누가 접속하나요? (PUBLIC 누구나 / APP_AUTH 로그인한 사용자만 / IP_ALLOWLIST 특정 IP만)",
    "tier": "용도는? (DEV 개발·테스트, 7일 뒤 자동 삭제 / PROD 실제 운영, 관리자 승인 필요)",
    "budget_usd_month": "한 달 최대 예산(USD)은? (비우면 $10)",
    "request_duration": "가장 오래 걸리는 요청은? (SECONDS 몇 초 / ABOUT_30S 30초 정도 / S30_TO_60S / S60_TO_120S / OVER_120S)",
}
UNSUPPORTED = {
    "MULTI_SERVICE": "서비스가 여러 개인 앱이에요. 플랫폼은 서비스 하나를 한 환경으로 만들어요.",
    "NON_HTTP": "HTTP가 아닌 통신(게임 서버 등)이라 지원 범위 밖이에요.",
    "PORT_FROM_FLAG_ONLY": "포트를 실행 인자로만 바꿀 수 있어요. 플랫폼은 PORT=8080 환경변수만 넘기므로, "
                           "앱이 환경변수에서 포트를 읽도록 바꿔야 해요.",
}
STAGE_HELP = {
    "PLAN": "만들기 전 보안 검사에서 막혔어요. 재시도 대상이 아니에요. 요구사항이나 설정을 바꿔 새 초안을 만들어야 해요.",
    "IMAGE": "이미지를 가져오지 못했어요. 이미지 주소·태그가 맞는지, 공개 저장소인지, linux/amd64 이미지가 있는지 확인해 주세요.",
    "TERRAFORM_PREPARE": "생성 준비 단계의 일시 오류일 수 있어요. 화면에서 '다시 시도'를 눌러 주세요.",
    "PROVISION": "AWS 리소스 생성 중 오류예요. 화면에서 다시 시도하거나 삭제할 수 있어요. 반복되면 관리자에게 알려 주세요.",
    "VALIDATE": "검증 실행 자체가 실패했어요. 화면에서 다시 시도해 주세요.",
    "REDEPLOY": "새 버전 배포에 실패했어요. 화면에서 다시 시도하거나 이전 버전으로 되돌릴 수 있어요.",
    "SECURITY": "만든 뒤 보안 검사에서 기준 위반이 나와 플랫폼이 접속을 차단했어요(D-65). "
                "다시 시도해도 같은 결과라 재시도 대상이 아니에요. "
                "아래 검사가 앱 설정 때문이면 고쳐서 새 초안을 만들고, 플랫폼이 만든 설정 때문이면 관리자에게 알려 주세요.",
}
CHECK_HELP = {  # failed check id prefix -> what the developer can change in their app
    "PR-ALL-02": "앱이 8080 포트에서 응답하는지, 상태 확인 경로가 200을 돌려주는지 확인해 주세요.",
    "PR-L-05": "Lambda에서 앱이 시작되지 못했을 수 있어요. 시작 시간이 길거나 /tmp 밖에 파일을 쓰는지 확인해 주세요.",
    "PR-C-03": "컨테이너가 ALB 상태 확인을 통과하지 못했어요. 상태 확인 경로와 8080 포트를 확인해 주세요.",
    "SB-SEC": "설정값에 비밀번호·키처럼 보이는 값이 있어요. 지금은 비밀값을 받지 않으니 빼 주세요.",
    "ART-INDEX": "zip 안에 index.html이 없어요. 빌드 결과 폴더(dist, build 등)를 올려 주세요.",
    "SB-": "플랫폼 보안 기준 항목이에요. 앱이 바꿀 수 있는 설정이 아니면 관리자에게 이 검사 이름을 전달해 주세요.",
}


def _client() -> PlatformClient:
    return PlatformClient()


def analyze_repo(path: str) -> dict:
    a = analyze(Path(path).expanduser())
    summary = [f"{k}: {f.value} ({f.confidence}) — {'; '.join(f.evidence[:2])}" for k, f in a.findings.items()]
    return {
        "summary": summary,
        "unsupported": [{"code": u, "why": UNSUPPORTED[u]} for u in a.unsupported],
        "confirm": a.confirm,
        "questions": {k: QUESTIONS[k] for k in a.ask if k in QUESTIONS},
        "draft": a.draft(),
        "port_env": a.value("port_env"),
        "next": "confirm에 있는 값을 근거와 함께 개발자에게 확인하고, questions를 물어 draft의 빈칸(None)을 채운 뒤 recommend를 부르세요.",
    }


def _check_missing(requirements: dict) -> list[str]:
    return [k for k in ("app_form", *DEVELOPER_ONLY) if requirements.get(k) is None and k != "budget_usd_month"]


def _not_chosen(rec: dict) -> list[dict]:
    """The best-placed excluded candidate per compute family, as the UI's "고르지 않은 구성" shows it."""
    chosen = (rec.get("architecture") or {}).get("compute")
    seen, out = set(), []
    for c in rec.get("candidates") or []:
        fam = c.get("candidate_id", "").split("+")[0]
        if c.get("status") == "SELECTED" or fam == chosen or fam in seen:
            continue
        seen.add(fam)
        label = RUN_MODEL.get(fam, STATIC_LABEL if fam == "NONE" else fam)
        if label in {o["run_model"] for o in out}:  # SLS and SLS_PC are one run model to the developer
            continue
        out.append({"run_model": label, "why": (c.get("reasons") or [{}])[0].get("message")})
    return out


def recommend(requirements: dict) -> dict:
    missing = _check_missing(requirements)
    if missing:
        return {"error": "아직 답하지 않은 항목이 있어요.", "missing": missing, "questions": {k: QUESTIONS.get(k, k) for k in missing}}
    rec = _client().call("POST", "/recommendations", {"input": requirements})
    arch = rec.get("architecture") or {}
    model = " + ".join(x for x in ((STATIC_LABEL if arch.get("frontend") == "STATIC" else None),
                                   RUN_MODEL.get(arch.get("compute") or "")) if x)
    cost = rec.get("cost") or {}
    return {
        "status": rec.get("status"),
        "run_model": model or None,
        "candidate": arch.get("candidate_id"),
        "provisionable": bool(rec.get("provisionable")),
        "artifact": rec.get("artifact_kind"),  # STATIC_ZIP (upload a build folder) | IMAGE (give an image reference)
        "cost_usd_month": {"typical": cost.get("rep_usd"), "max": cost.get("upper_usd"), "budget": cost.get("budget_usd")},
        "reasons": [r["message"] for r in rec.get("reasons") or []],
        "warnings": [r["message"] for r in rec.get("warnings") or []],
        "not_chosen": _not_chosen(rec),
        "unsupported": [r["message"] for r in rec.get("causes") or []] if rec.get("status") == "UNSUPPORTED" else [],
        "nearest_alternative": rec.get("nearest"),
        "over_budget": rec.get("status") == "OVER_BUDGET",
        "note": "예산을 넘으면 요구사항을 바꾸거나, 사람이 화면에서 초과에 동의해야 해요(agent는 동의할 수 없음)."
        if rec.get("status") == "OVER_BUDGET" else None,
    }


def create_pending_environment(name: str, requirements: dict, image: str | None = None, health_path: str = "/",
                               env_vars: dict | None = None) -> dict:
    """DRAFT only. Nothing is created in AWS until a person presses 승인 in the web UI."""
    missing = _check_missing(requirements)
    if missing:
        return {"error": "아직 답하지 않은 항목이 있어요.", "missing": missing}
    body = {"name": name, "input": requirements}
    if image:
        body["image"] = {"image": image, "health_path": health_path or "/", "env": env_vars or {}}
    try:
        out = _client().call("POST", "/environments", body)
    except PlatformError as e:
        return {"error": e.message, "detail": e.detail}
    env = out["environment"]
    c = _client()
    return {
        "env_id": env["env_id"], "status": env["status"],
        "approve_here": c.env_url(env["env_id"]),
        "needs_site_upload": out.get("upload") is not None,
        "next": ("upload_site로 빌드 결과를 올린 뒤, " if out.get("upload") else "")
        + "개발자에게 approve_here 링크를 열어 구성·비용을 확인하고 직접 '승인하고 만들기'를 누르라고 안내하세요. "
          "agent는 승인할 수 없어요.",
    }


def upload_site(env_id: str, path: str) -> dict:
    """Upload a static build folder (or a .zip) into a DRAFT environment."""
    c = _client()
    try:
        form = c.call("POST", f"/environments/{env_id}/upload-url")["upload"]
        data = zip_dir(Path(path).expanduser())
        c.upload_zip(form, data)
    except PlatformError as e:
        return {"error": e.message, "detail": e.detail}
    return {"uploaded_bytes": len(data), "approve_here": c.env_url(env_id),
            "next": "파일 검사는 승인할 때 실행돼요. 개발자에게 화면에서 승인해 달라고 안내하세요."}


def _checks(env: dict) -> tuple[dict, list[dict]]:
    checks = env.get("checks") or []
    counts: dict[str, int] = {}
    for c in checks:
        counts[c.get("status", "?")] = counts.get(c.get("status", "?"), 0) + 1
    failed = [c for c in checks if c.get("status") == "FAIL"]
    return counts, failed


def get_environment(env_id: str) -> dict:
    c = _client()
    env = c.call("GET", f"/environments/{env_id}")["environment"]
    counts, failed = _checks(env)
    return {
        "env_id": env_id, "name": env.get("name"), "status": env.get("status"), "created_via": env.get("created_via"),
        "url": (env.get("outputs") or {}).get("url"), "expires_at": env.get("expires_at"),
        "checks": counts, "failed_checks": [f"{x.get('check_id')}: {x.get('detail', '')}"[:200] for x in failed],
        "failure": env.get("failure"), "ui": c.env_url(env_id),
        "drift": _drift_summary(env), "last_repair": env.get("last_repair"),
        "last_events": [e.get("message") for e in (env.get("event_log") or [])[-4:]],
    }


def _drift_summary(env: dict) -> dict | None:
    d = env.get("drift")
    if not d:
        return None
    plan = env.get("repair_plan") or {}
    return {"changed_checks": [f["check_id"] for f in d.get("failed") or []], "event": d.get("event_name"),
            "by": d.get("actor"), "at": d.get("event_time") or d.get("detected_at"), "trigger": d.get("trigger"),
            "endpoint_blocked": bool(d.get("endpoint_disabled")), "detect_seconds": d.get("detect_seconds"),
            "repair_plan": {"ready": bool(plan.get("stacks")) and bool(plan.get("passed")), "changes": plan.get("changes"),
                            "resources": [c["address"] for s in plan.get("stacks") or [] for c in s.get("changes") or []],
                            "error": plan.get("error")}}


def recheck_environment(env_id: str) -> dict:
    """Run the environment's checks again now (read-only; a found difference is reported, never changed back)."""
    c = _client()
    try:
        c.call("POST", f"/environments/{env_id}/recheck")
    except PlatformError as e:
        return {"error": e.message}
    return {"env_id": env_id, "next": "몇 초 뒤 get_environment로 결과(drift)를 확인하세요.", "ui": c.env_url(env_id)}


def list_environments() -> dict:
    envs = _client().call("GET", "/environments").get("environments") or []
    return {"environments": [{"env_id": e["env_id"], "name": e.get("name"), "status": e.get("status"),
                              "created_via": e.get("created_via")} for e in envs if e.get("status") != "DELETED"]}


def explain_failure(env_id: str) -> dict:
    """Deterministic: the failed stage and checks mapped to what the developer can change. No guessing."""
    c = _client()
    env = c.call("GET", f"/environments/{env_id}")["environment"]
    titles = c.call("GET", "/presets").get("check_titles") or {}
    _, failed = _checks(env)
    if env.get("status") == "DRIFTED":
        return _explain_drift(c, env, titles)
    failure = dict(env.get("failure") or {})
    verdict = env.get("verdict") or {}
    if not failure.get("stage") and verdict.get("security_failed"):  # validation blocked it; no Step Functions failure
        failure = {"stage": "SECURITY", "message": "보안 검사에 실패해 접속을 차단했어요." if verdict.get("endpoint_disabled")
                   else "보안 검사에 실패했어요."}
    if env.get("status") not in ("FAILED", "DEGRADED", "DELETE_FAILED") and not failed:
        return {"status": env.get("status"), "explanation": "실패한 환경이 아니에요.", "ui": c.env_url(env_id)}
    steps = [STAGE_HELP[failure["stage"]]] if failure.get("stage") in STAGE_HELP else []
    for x in failed:
        cid = x.get("check_id", "")
        hint = next((h for k, h in CHECK_HELP.items() if cid.startswith(k)), None)
        steps.append(f"{cid} {titles.get(cid, '')}: " + (hint or "화면의 검사 결과를 확인해 주세요."))
    return {
        "status": env.get("status"), "stage": failure.get("stage"), "message": failure.get("message"),
        "failed_checks": [{"id": x.get("check_id"), "title": titles.get(x.get("check_id"), ""), "detail": x.get("detail")}
                          for x in failed],
        "what_to_do": steps or ["화면의 진행 기록을 확인해 주세요."],
        "who_acts": "다시 시도·삭제는 사람이 화면에서 해요(agent는 할 수 없음). 앱이나 설정을 고친 경우에는 새 초안을 만드세요.",
        "ui": c.env_url(env_id),
    }


def _explain_drift(c: PlatformClient, env: dict, titles: dict) -> dict:
    d = _drift_summary(env) or {}
    plan = d.get("repair_plan") or {}
    who = f"{d.get('event')} ({d.get('by')}, {d.get('at')})" if d.get("event") else f"주기 검사({d.get('at')})"
    steps = [f"승인된 구성과 달라졌어요: {', '.join(f'{x} {titles.get(x, chr(32)).strip()}' for x in d.get('changed_checks') or [])}. "
             f"원인으로 보이는 변경: {who}."]
    if d.get("endpoint_blocked"):
        steps.append("보안 기준을 벗어난 변경이라 플랫폼이 접속을 차단해 두었어요(D-65). 복구하면 다시 열려요.")
    if plan.get("ready"):
        steps.append(f"플랫폼이 원래 구성으로 되돌리는 수정안을 만들었어요(리소스 {plan.get('changes')}개: "
                     f"{', '.join(plan.get('resources') or [])}). 화면에서 수정안을 확인하고 '원래 구성으로 복구'를 눌러 주세요.")
    elif plan.get("error"):
        steps.append(plan["error"])
    else:
        steps.append("수정안을 만드는 중이에요. 잠시 뒤 다시 확인해 주세요.")
    steps.append("일부러 바꾼 설정이라면 되돌리지 말고, 요구사항을 바꿔 새 환경으로 반영해야 해요"
                 "(콘솔 변경은 다음 재검사에서 다시 감지돼요).")
    return {"status": "DRIFTED", "stage": "DRIFT", "message": "승인된 구성과 실제 설정이 달라졌어요.",
            "drift": d, "what_to_do": steps,
            "who_acts": "복구 승인은 사람이 화면에서 해요(agent는 할 수 없음).", "ui": c.env_url(env["env_id"])}
