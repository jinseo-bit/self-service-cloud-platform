"""Score the repo analyzer against the baseline public apps (docs/baseline/apps.json, D-183).

Two numbers per app:
- field accuracy: analyzer value == ground truth, per field the code can answer
- outcome: run the analyzer's draft through the real engine and compare with the SA's expected outcome,
  (a) as drafted and (b) after the developer corrects the fields the analyzer put on `confirm`.
Repos are shallow-cloned at the pinned commit into build/baseline-repos/ (network, read-only).

Usage: python scripts/eval_analyzer.py [--write]   (--write updates docs/baseline/eval-results.md)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.repo_analyzer import VERSION, analyze  # noqa: E402
from platform_core import blueprints  # noqa: E402
from platform_core.engine import RawInput, recommend  # noqa: E402
from platform_core.pricing import PriceTable  # noqa: E402

APPS = ROOT / "docs" / "baseline" / "apps.json"
CACHE = ROOT / "build" / "baseline-repos"
OUT = ROOT / "docs" / "baseline" / "eval-results.md"
HELDOUT = ROOT / "docs" / "baseline" / "heldout.json"  # D-184: never used to change a rule
HELDOUT_OUT = ROOT / "docs" / "baseline" / "heldout-results.md"
HELDOUT2 = ROOT / "docs" / "baseline" / "heldout2.json"  # D-184: the fair measure of analyzer v2
HELDOUT2_OUT = ROOT / "docs" / "baseline" / "heldout2-results.md"
FIELDS = ["app_form", "spa_fallback", "realtime_connection", "large_payload", "in_memory_state", "non_http",
          "request_duration", "data", "multi_service", "port_env"]
# Fixed answers for what code cannot tell, so outcomes compare the analyzer and nothing else.
DEVELOPER_DEFAULTS = {"traffic_scale": "T1", "traffic_pattern": "IDLE_MOSTLY", "downtime_tolerance": "OK",
                      "latency_sensitivity": "TOLERANT", "exposure": "PUBLIC", "tier": "DEV", "budget_usd_month": 30}
COMPUTE_TO_BLUEPRINT = {"NONE": "static", "SLS": "serverless", "SLS_PC": "serverless", "CTR": "container"}
PRICES = PriceTable.load()
HELDOUT_CAVEATS = """
## 읽을 때 주의

- **held-out 세트다.** 앱 선정과 정답 작성(2026-10-26)을 분석기를 돌리기 전에 했고, 이 결과를 보고 규칙을 고치지 않는다(D-184).
- **정답(heldout.json)은 초안이다.** `reviewed_by`가 비어 있는 동안은 이진서가 검토하기 전 값이다.
- 앱 5개라 하나가 20%포인트다. 경향을 보는 숫자로 읽는다.
"""
CAVEATS = '\n## 읽을 때 주의\n\n- **개발 세트 수치다.** 규칙 두 개(업로드 판정, 확인 요청 범위)를 이 13개 앱을 보며 고쳤다. 공정한 정확도는 규칙을 만들 때 보지 않은 앱(held-out)으로 다시 재야 한다.\n- **정답(apps.json)은 초안이다.** `reviewed_by`가 비어 있는 동안은 이진서가 검토하기 전 값이다.\n- **확인 후 100%는 상한이다.** 확인 요청된 항목을 개발자가 모두 바르게 고친다고 가정한 값이다. 확인 요청을 받지 않은 오답(online-boutique의 data)은 고쳐지지 않는다.\n- 남은 오답: etherpad의 큰 파일(가져오기용 업로드 라이브러리를 대용량 업로드로 판정), uptime-kuma의 DB(드라이버 5개 중 기본값이 SQLite인 것은 코드 구조만으로는 알 수 없음, 확인 요청함), online-boutique의 DB(멀티 서비스라 범위 밖).\n'  # noqa: E501


def checkout(app: dict) -> Path:
    d = CACHE / app["repo"].replace("/", "_")
    if not (d / ".git").exists():
        d.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{app['repo']}"
        for cmd in (["git", "init", "-q"], ["git", "fetch", "-q", "--depth", "1", url, app["commit"]],
                    ["git", "-c", "core.longpaths=true", "-c", "advice.detachedHead=false", "checkout", "-q", "-f", "FETCH_HEAD"]):
            subprocess.run(cmd, cwd=d, check=False, capture_output=True)
    return d


def outcome(draft: dict, unsupported: list[str]) -> tuple[str, str]:
    if "MULTI_SERVICE" in unsupported or "NON_HTTP" in unsupported:
        return "none", "UNSUPPORTED"
    raw = {**DEVELOPER_DEFAULTS, **{k: v for k, v in draft.items() if v is not None and k != "spa_fallback"}}
    rec = recommend(RawInput.from_dict(raw), PRICES)
    if rec["status"] in ("UNSUPPORTED", "NEEDS_INPUT"):
        return "none", "UNSUPPORTED"
    arch = rec["architecture"]
    bp = COMPUTE_TO_BLUEPRINT.get(arch.get("compute"), "none")
    if "PORT_FROM_FLAG_ONLY" in unsupported:
        return bp, "INCOMPATIBLE"
    return bp, "PROVISIONABLE" if blueprints.for_architecture(arch) else "RECOMMEND_ONLY"


def corrected(draft: dict, confirm: list[str], truth: dict) -> dict:
    """The developer fixes every answer the analyzer asked them to confirm (and answers request_duration)."""
    d = json.loads(json.dumps(draft))
    caps = d.setdefault("capabilities", {})
    for key in set(confirm) | {"request_duration"}:
        if key == "app_form":
            d["app_form"] = truth["app_form"]
        elif key == "data":
            d["data"] = {"needed": truth["data"] != "NONE", "detected": truth["data"]}
        elif d["app_form"] != "STATIC_SITE" and key in ("realtime_connection", "large_payload", "in_memory_state"):
            caps[key] = truth[key]
        elif key == "request_duration" and d["app_form"] != "STATIC_SITE" and truth["request_duration"]:
            caps["request_duration"] = truth["request_duration"]
    if d["app_form"] == "STATIC_SITE":
        d["capabilities"] = {}
    return d


def same(field: str, got, want) -> bool:
    if field == "request_duration":
        return got == want  # None == None: both say "the developer must tell"
    return got == want


def evaluate(apps_file: Path = APPS) -> dict:
    apps = json.loads(apps_file.read_text(encoding="utf-8"))["apps"]
    rows, totals = [], {"fields": 0, "fields_ok": 0, "draft_ok": 0, "confirmed_ok": 0, "confirm_asked": 0, "wrong_but_flagged": 0}
    for app in apps:
        res = analyze(checkout(app))
        truth = app["expected"]
        diffs = []
        for fld in FIELDS:
            got = res.value(fld) if fld in res.findings else (None if fld in ("request_duration", "port_env") else False)
            if fld in ("realtime_connection", "large_payload", "in_memory_state") and res.value("app_form") == "STATIC_SITE":
                got = False
            ok = same(fld, got, truth[fld])
            totals["fields"] += 1
            totals["fields_ok"] += ok
            if not ok:
                flagged = fld in res.confirm or fld in res.ask
                totals["wrong_but_flagged"] += flagged
                diffs.append({"field": fld, "got": got, "want": truth[fld], "flagged": flagged,
                              "evidence": res.findings[fld].evidence if fld in res.findings else []})
        want = (app["expected_outcome"]["blueprint"], app["expected_outcome"]["status"])
        got_draft = outcome(res.draft(), res.unsupported)
        got_conf = outcome(corrected(res.draft(), res.confirm, truth), res.unsupported)
        def match(g, want=want):
            return g[1] == want[1] and (want[1] == "UNSUPPORTED" or g[0] == want[0])
        totals["draft_ok"] += match(got_draft)
        totals["confirmed_ok"] += match(got_conf)
        totals["confirm_asked"] += len(res.confirm)
        rows.append({"id": app["id"], "category": app["category"], "want": want, "draft": got_draft, "confirmed": got_conf,
                     "draft_ok": match(got_draft), "confirmed_ok": match(got_conf), "confirm": res.confirm,
                     "unsupported": res.unsupported, "diffs": diffs})
    return {"apps": len(apps), "rows": rows, **totals}


def markdown(r: dict, held: bool = False, src_file: Path | None = None, flag: str = "") -> str:
    n = r["apps"]
    src = (src_file or (HELDOUT if held else APPS)).name
    lines = [
        f"# Repo analyzer {VERSION}: {'held-out' if held else 'baseline'} evaluation ({src})",
        "",
        f"Generated by `python scripts/eval_analyzer.py {flag + ' ' if flag else ''}--write`. "
        f"Ground truth: [{src}]({src}) (draft, under review).",
        "Fields the code cannot answer (traffic, downtime, audience, budget) are fixed to DEV defaults,"
        " so the outcome reflects the analyzer only.",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Field accuracy | {r['fields_ok']}/{r['fields']} ({r['fields_ok'] / r['fields']:.0%}) |",
        f"| Wrong fields that were flagged to the developer | {r['wrong_but_flagged']}/{r['fields'] - r['fields_ok']} |",
        f"| Outcome matches SA, as drafted | {r['draft_ok']}/{n} ({r['draft_ok'] / n:.0%}) |",
        f"| Outcome matches SA, after the developer confirms flagged fields | {r['confirmed_ok']}/{n} ({r['confirmed_ok'] / n:.0%}) |",
        f"| Confirmations asked (total) | {r['confirm_asked']} |",
        "",
        "| App | Category | SA expected | As drafted | After confirm | Confirm asked | Wrong fields |",
        "|---|---|---|---|---|---|---|",
    ]
    fmt = lambda t: f"{t[0]} / {t[1]}"  # noqa: E731
    for row in r["rows"]:
        wrong = ", ".join(f"{d['field']} ({d['got']}→{d['want']}{', flagged' if d['flagged'] else ''})" for d in row["diffs"]) or "–"
        lines.append(f"| {row['id']} | {row['category']} | {fmt(row['want'])} | {'✅' if row['draft_ok'] else '❌'} {fmt(row['draft'])} "
                     f"| {'✅' if row['confirmed_ok'] else '❌'} {fmt(row['confirmed'])} | {', '.join(row['confirm']) or '–'} | {wrong} |")
    return "\n".join(lines) + "\n" + (HELDOUT_CAVEATS if held else CAVEATS)


if __name__ == "__main__":
    sets = {"--heldout": (HELDOUT, HELDOUT_OUT), "--heldout2": (HELDOUT2, HELDOUT2_OUT)}
    flag = next((a for a in sys.argv if a in sets), "")
    held = bool(flag)
    src_file, out = sets.get(flag, (APPS, OUT))
    md = markdown(evaluate(src_file), held, src_file, flag)
    if flag == "--heldout2":
        md = md.replace("(2026-10-26)", "(2026-10-27, v2 규칙을 확정한 뒤)").replace("heldout.json", "heldout2.json")
    print(md)
    if "--write" in sys.argv:
        out.write_text(md, encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
