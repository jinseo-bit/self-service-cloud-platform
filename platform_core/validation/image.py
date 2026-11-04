"""ARTIFACT check for the Serverless blueprint input (D-03, D-166): a public container image reference,
plain environment variables and a health path. Secret values are not accepted in the MVP (D-167):
env values end up in DynamoDB and Terraform state, so anything that looks like a secret is rejected.
"""

from __future__ import annotations

import re

from .artifact import SECRET_PATTERNS
from .result import FAIL, PASS, CheckResult

# Registries the runner can pull anonymously. Private registries need credentials the platform does not hold.
ALLOWED_REGISTRIES = ("docker.io", "public.ecr.aws", "ghcr.io", "quay.io")
_REF = re.compile(r"^(?:(?P<registry>[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?)/)?(?P<repo>[a-z0-9]+(?:[._/-][a-z0-9]+)*)"
                  r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SECRET_KEY = re.compile(r"(?i)(secret|passw|token|api[_-]?key|private[_-]?key|credential|access[_-]?key|session)")
RESERVED_PREFIXES = ("AWS_", "LAMBDA_", "_HANDLER", "_X_AMZN")
RESERVED_KEYS = {"PORT", "PATH", "HOME", "TZ", "LANG"}  # set by the platform / runtime
MAX_ENV_VARS = 20
MAX_ENV_BYTES = 3072  # Lambda allows 4 KB in total; the platform adds its own variables
_HEALTH = re.compile(r"^/[A-Za-z0-9/_.\-]{0,127}$")


def _r(check_id, ok, detail, dim="Security", *sb):
    return CheckResult(check_id, "ARTIFACT", dim, PASS if ok else FAIL, detail, tuple(sb))


def normalize_image(ref: str) -> str:
    """docker.io short names → fully qualified (traefik/whoami:v1 → docker.io/traefik/whoami:v1)."""
    m = _REF.match(ref.strip())
    if not m:
        return ref.strip()
    registry = m.group("registry")
    if registry is None:
        repo = m.group("repo")
        return "docker.io/" + (repo if "/" in repo else f"library/{repo}") + ref.strip()[len(repo):]
    return ref.strip()


def validate_image_spec(spec: dict) -> tuple[list[CheckResult], dict]:
    """Returns (checks, normalized spec {image, env, health_path})."""
    image = normalize_image(str(spec.get("image") or ""))
    env = spec.get("env") or {}
    health = str(spec.get("health_path") or "/")
    out: list[CheckResult] = []

    m = _REF.match(image)
    registry = (m.group("registry") or "") if m else ""
    ok_ref = bool(m) and registry in ALLOWED_REGISTRIES and bool(m.group("tag") or m.group("digest"))
    why = ("tag or digest required" if m and not (m.group("tag") or m.group("digest"))
           else f"registry must be one of {list(ALLOWED_REGISTRIES)}" if m else "not an image reference")
    out.append(_r("IMG-REF", ok_ref, image if ok_ref else f"{image or '(empty)'}: {why}", "Reachability"))

    if not isinstance(env, dict):
        env = {}
        out.append(_r("IMG-ENV", False, "environment variables must be key/value pairs", "Operability"))
    else:
        bad_keys = [k for k in env if not _KEY.match(str(k)) or str(k).upper() in RESERVED_KEYS
                    or str(k).upper().startswith(RESERVED_PREFIXES)]
        size = sum(len(str(k)) + len(str(v)) for k, v in env.items())
        ok_env = not bad_keys and len(env) <= MAX_ENV_VARS and size <= MAX_ENV_BYTES
        out.append(_r("IMG-ENV", ok_env, f"{len(env)} variables, {size} bytes" if ok_env else
                      f"invalid or reserved keys {bad_keys}" if bad_keys else f"too many/large ({len(env)} vars, {size} bytes)",
                      "Operability"))
    secret_keys = [k for k in env if SECRET_KEY.search(str(k))]
    secret_vals = [k for k, v in env.items() for rx in SECRET_PATTERNS.values() if rx.search(str(v).encode())]
    hits = sorted(set(secret_keys + secret_vals))
    out.append(_r("SB-SEC-02", not hits, f"secret-like variables {hits}: secrets are not supported yet" if hits
                  else "no secret-like variables", "Security", "SB-SEC-02"))
    out.append(_r("IMG-HEALTH", bool(_HEALTH.match(health)), health, "Reachability"))
    return out, {"image": image, "env": {str(k): str(v) for k, v in env.items()}, "health_path": health}
