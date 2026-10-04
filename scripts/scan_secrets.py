"""Fail if anything that looks like a credential is in the repository working tree.

    python scripts/scan_secrets.py
Uses the same patterns as the SB-SEC-04 artifact check plus AWS secret-key assignments.
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_core.validation.artifact import SECRET_PATTERNS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp949
    _s.reconfigure(encoding="utf-8", errors="replace")
EXTRA = {
    "aws_secret_assignment": re.compile(rb"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}"),
}
# Test fixtures intentionally contain a fake key to prove the scanner works.
ALLOW = {"tests/validation/test_artifact_and_coverage.py", "tests/control_plane/test_api_flow.py", "tests/ui/test_ui_flow.py"}
SKIP_DIRS = {".venv", "venv", ".git", ".terraform", ".terraform-plugin-cache", "build", "__pycache__", "references",
             "vendor", ".pytest_cache"}


def files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
        return [ROOT / f for f in out.stdout.splitlines()]
    except Exception:
        return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def main() -> int:
    hits = []
    for f in files():
        rel = f.relative_to(ROOT).as_posix()
        if not f.is_file() or SKIP_DIRS & set(Path(rel).parts) or rel in ALLOW:
            continue
        data = f.read_bytes()[:5_000_000]
        for name, rx in {**SECRET_PATTERNS, **EXTRA}.items():
            for m in rx.finditer(data):
                hits.append(f"{rel}: {name}: {m.group(0)[:12].decode(errors='replace')}…")
    for h in hits:
        print(h)
    print(f"scanned, {len(hits)} finding(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
