"""Build deployment bundles into build/: lambda.zip (control plane) and runner.zip (CodeBuild source)."""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
SKIP_DIRS = {"__pycache__", ".terraform", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".tfstate", ".backup", ".bin"}


def add_tree(z: zipfile.ZipFile, rel: str) -> int:
    n = 0
    for p in sorted((ROOT / rel).rglob("*")):
        if p.is_dir() or SKIP_DIRS & set(p.parts) or p.suffix in SKIP_SUFFIX or p.name.endswith("override.tf"):
            continue
        info = zipfile.ZipInfo(p.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))  # reproducible
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, p.read_bytes())
        n += 1
    return n


def build(name: str, trees: list[str]) -> None:
    BUILD.mkdir(exist_ok=True)
    out = BUILD / name
    with zipfile.ZipFile(out, "w") as z:
        count = sum(add_tree(z, t) for t in trees)
    print(f"{out.relative_to(ROOT)}: {count} files, {out.stat().st_size} bytes")


if __name__ == "__main__":
    build("lambda.zip", ["control_plane", "platform_core"])
    build("runner.zip", ["runner", "control_plane", "platform_core", "infra/modules", "infra/blueprints", "infra/shared"])
