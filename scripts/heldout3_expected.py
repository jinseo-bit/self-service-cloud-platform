"""Fill expected_outcome in docs/baseline/heldout3.json by running the real engine on the EXPECTED fields (D-184).

The analyzer is not run here: this turns the SA's ground truth into the outcome the platform would give for it, so
the expected outcome and the engine cannot disagree by a hand-written mistake. Run once when the truth changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_analyzer import outcome  # noqa: E402  (imports the analyzer module but never calls it)

FILE = ROOT / "docs" / "baseline" / "heldout3.json"


def truth_draft(t: dict) -> dict:
    static = t["app_form"] == "STATIC_SITE"
    return {"app_form": t["app_form"], "spa_fallback": t["spa_fallback"],
            "capabilities": {} if static else {k: t[k] for k in ("realtime_connection", "large_payload", "in_memory_state", "non_http")},
            "data": {"needed": t["data"] != "NONE", "detected": t["data"]}}


def truth_unsupported(app: dict) -> list[str]:
    t = app["expected"]
    return ([u for u, on in (("MULTI_SERVICE", t["multi_service"]), ("NON_HTTP", t["non_http"])) if on]
            + (["PORT_FROM_FLAG_ONLY"] if app.get("fixed_port") else []))


if __name__ == "__main__":
    doc = json.loads(FILE.read_text(encoding="utf-8"))
    for app in doc["apps"]:
        bp, status = outcome(truth_draft(app["expected"]), truth_unsupported(app))
        app["expected_outcome"] = {"blueprint": bp, "status": status}
        print(f"{app['id']:24} {bp:10} {status}")
    if "--write" in sys.argv:
        FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
