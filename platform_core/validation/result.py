"""Check results and the READY / DEGRADED / FAILED verdict (docs/production-ready.md §2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

PASS, FAIL, DEFERRED, NOT_APPLICABLE = "PASS", "FAIL", "DEFERRED", "NOT_APPLICABLE"
DIMENSIONS = ("Security", "Reachability", "Resilience", "Observability", "Operability", "Cost")


@dataclass(frozen=True)
class CheckResult:
    check_id: str  # PR-* or SB-* id from the docs
    layer: str  # PLAN | STATE | HTTP | ARTIFACT
    dimension: str
    status: str
    detail: str = ""
    baseline_ids: tuple[str, ...] = field(default=())

    def __post_init__(self):
        assert self.dimension in DIMENSIONS, self.dimension
        assert self.status in (PASS, FAIL, DEFERRED, NOT_APPLICABLE), self.status

    def to_dict(self) -> dict:
        d = asdict(self)
        d["baseline_ids"] = list(self.baseline_ids)
        return d


def verdict(results: list[CheckResult]) -> dict:
    """Security/Resilience/Observability/Operability/Cost failures are platform
    responsibility → FAILED. Reachability failures only → DEGRADED (user code)."""
    failed = [r for r in results if r.status == FAIL]
    platform_failures = [r for r in failed if r.dimension != "Reachability"]
    if platform_failures:
        state = "FAILED"
    elif failed:
        state = "DEGRADED"
    else:
        state = "READY"
    return {
        "verdict": state,
        "security_failed": any(r.dimension == "Security" for r in failed),
        "failed": [r.check_id for r in failed],
        "counts": {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, DEFERRED, NOT_APPLICABLE)},
    }
