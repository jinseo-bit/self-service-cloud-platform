"""ARTIFACT check for static bundles (SB-SEC-04, D-16): structure, size, embedded secrets."""

from __future__ import annotations

import collections
import hashlib
import re
import zipfile
from pathlib import PurePosixPath

from .result import FAIL, PASS, CheckResult

MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_FILES = 5_000
TEXT_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".map", ".xml", ".svg", ".env"}
SECRET_PATTERNS = {
    "aws_access_key_id": re.compile(rb"(?<![A-Z0-9])(AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "private_key": re.compile(rb"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{36}"),
    "slack_token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
}


def _r(check_id, ok, detail, dim="Security", *sb):
    return CheckResult(check_id, "ARTIFACT", dim, PASS if ok else FAIL, detail, tuple(sb))


def bundle_root(names: list[str]) -> str:
    """Accept bundles zipped either flat or inside a single top-level folder."""
    if "index.html" in names:
        return ""
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1:
        top = tops.pop()
        if f"{top}/index.html" in names:
            return top + "/"
    return ""


def validate_static_bundle(path: str) -> tuple[list[CheckResult], str]:
    """Returns (results, root_prefix_inside_zip)."""
    out: list[CheckResult] = []
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return [_r("ART-ZIP", False, "not a zip file", "Reachability")], ""
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        unsafe = [n for n in names if n.startswith("/") or ".." in PurePosixPath(n).parts]
        out.append(_r("ART-PATH", not unsafe, f"unsafe paths {unsafe[:5]}" if unsafe else "paths safe"))
        total = sum(i.file_size for i in infos)
        out.append(_r("ART-SIZE", total <= MAX_BUNDLE_BYTES and len(infos) <= MAX_FILES,
                      f"{len(infos)} files, {total} bytes (limit {MAX_BUNDLE_BYTES})", "Cost"))
        root = bundle_root(names)
        has_index = f"{root}index.html" in names
        html = sum(1 for n in names if n.lower().endswith((".html", ".htm")))
        out.append(_r("ART-INDEX", has_index, "index.html at bundle root" if has_index
                      else f"no index.html at bundle root ({html} html files in bundle)", "Reachability"))
        hits = []
        for info in infos:
            if PurePosixPath(info.filename).suffix.lower() not in TEXT_SUFFIXES and not info.filename.endswith(".env"):
                continue
            data = zf.read(info)[:2_000_000]
            for label, rx in SECRET_PATTERNS.items():
                if rx.search(data):
                    hits.append(f"{info.filename}:{label}")
        out.append(_r("SB-SEC-04", not hits, f"possible secrets: {hits[:5]}" if hits else "no secret patterns", "Security", "SB-SEC-04"))
    return out, root


def index_sha256(path: str, root: str) -> str | None:
    """Fingerprint of the revision's index.html, used by PR-S-03 to prove the new version is served."""
    with zipfile.ZipFile(path) as zf:
        try:
            return hashlib.sha256(zf.read(f"{root}index.html")).hexdigest()
        except KeyError:
            return None


NOT_WEB_SUFFIXES = {".exe", ".dll", ".msi", ".pak", ".so", ".dylib", ".class", ".jar", ".py", ".java", ".go"}


def bundle_summary(path: str) -> dict | None:
    """What the user actually uploaded, so a failed ART-INDEX can say "this is not a built website" (D-164)."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return None
    with zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
    exts = collections.Counter(PurePosixPath(n).suffix.lower() or "(확장자 없음)" for n in names)
    tops = sorted({n.split("/", 1)[0] + ("/" if "/" in n else "") for n in names})
    nested = sorted(n for n in names if PurePosixPath(n).name.lower() == "index.html")
    return {"files": len(names), "extensions": [[e, c] for e, c in sorted(exts.items(), key=lambda x: (-x[1], x[0]))[:6]],
            "html_files": sum(1 for n in names if n.lower().endswith((".html", ".htm"))),
            "top_level": tops[:8], "nested_index": nested[:3],
            "not_web_files": sum(c for e, c in exts.items() if e in NOT_WEB_SUFFIXES)}
