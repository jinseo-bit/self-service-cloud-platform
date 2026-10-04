"""Raw input and normalized Requirement Model (docs/recommendation-engine.md §2)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from . import constants as C

APP_FORMS = ("STATIC_SITE", "SERVER_APP", "STATIC_PLUS_API")
# "How long does the slowest request take?" (single choice, D-157). Thresholds come from the front doors:
# API Gateway HTTP API 30 s (hard), CloudFront origin response timeout up to 120 s without a quota increase.
REQUEST_DURATIONS = ("SECONDS", "ABOUT_30S", "S30_TO_60S", "S60_TO_120S", "OVER_120S")
TRAFFIC_SCALES = ("T1", "T2", "T3", "T4")
TRAFFIC_PATTERNS = ("IDLE_MOSTLY", "BUSINESS_HOURS", "STEADY", "SPIKY")
DETECTED_ENGINES = ("NONE", "POSTGRES", "DYNAMODB", "OTHER")
DECLARED_ENGINES = ("UNKNOWN", "POSTGRES", "DYNAMODB")
DOWNTIME = ("OK", "NOT_IN_BUSINESS_HOURS", "NEVER")
LATENCY = ("TOLERANT", "SENSITIVE")
EXPOSURES = ("PUBLIC", "APP_AUTH", "IP_ALLOWLIST")
TIERS = ("DEV", "PROD")
AVAILABILITY_LEVEL = {"OK": "A0", "NOT_IN_BUSINESS_HOURS": "A1", "NEVER": "A2"}


class InputError(ValueError):
    """Raw input is malformed (not a requirement conflict — those are CONF_* results)."""


def _enum(value, allowed, name):
    if value not in allowed:
        raise InputError(f"{name}: '{value}' is not one of {list(allowed)}")
    return value


@dataclass(frozen=True)
class Capabilities:
    realtime_connection: bool = False
    request_duration: str = "SECONDS"
    large_payload: bool = False
    in_memory_state: bool = False
    non_http: bool = False


@dataclass(frozen=True)
class DataTraits:
    relational: bool = False
    flexible_query: bool = False
    key_lookup_only: bool = False


@dataclass(frozen=True)
class Data:
    needed: bool = False
    detected: str = "NONE"  # from code scan (Git input) — NONE means nothing detected
    declared: str = "UNKNOWN"  # what the user says the app uses
    traits: DataTraits = DataTraits()


@dataclass(frozen=True)
class RawInput:
    app_form: str
    traffic_scale: str
    traffic_pattern: str
    downtime_tolerance: str
    latency_sensitivity: str
    exposure: str
    tier: str
    capabilities: Capabilities = Capabilities()
    data: Data = Data()
    allowlist_cidrs: tuple[str, ...] = ()
    budget_usd_month: int | None = None
    env_keys: tuple[str, ...] = ()
    secret_keys: tuple[str, ...] = ()

    @staticmethod
    def from_dict(d: dict) -> RawInput:
        caps = d.get("capabilities") or {}
        data = d.get("data") or {}
        traits = data.get("traits") or {}
        config = d.get("config") or {}
        budget = d.get("budget_usd_month")
        if budget is not None:
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
                raise InputError("budget_usd_month: must be a non-negative integer or null")
        raw = RawInput(
            app_form=_enum(d.get("app_form"), APP_FORMS, "app_form"),
            traffic_scale=_enum(d.get("traffic_scale"), TRAFFIC_SCALES, "traffic_scale"),
            traffic_pattern=_enum(d.get("traffic_pattern"), TRAFFIC_PATTERNS, "traffic_pattern"),
            downtime_tolerance=_enum(d.get("downtime_tolerance"), DOWNTIME, "downtime_tolerance"),
            latency_sensitivity=_enum(d.get("latency_sensitivity"), LATENCY, "latency_sensitivity"),
            exposure=_enum(d.get("exposure"), EXPOSURES, "exposure"),
            tier=_enum(d.get("tier"), TIERS, "tier"),
            capabilities=Capabilities(
                realtime_connection=bool(caps.get("realtime_connection", False)),
                request_duration=_enum(caps.get("request_duration", "SECONDS"),
                                       REQUEST_DURATIONS, "capabilities.request_duration"),
                large_payload=bool(caps.get("large_payload", False)),
                in_memory_state=bool(caps.get("in_memory_state", False)),
                non_http=bool(caps.get("non_http", False)),
            ),
            data=Data(
                needed=bool(data.get("needed", False)),
                detected=_enum(data.get("detected", "NONE"), DETECTED_ENGINES, "data.detected"),
                declared=_enum(data.get("declared", "UNKNOWN"), DECLARED_ENGINES, "data.declared"),
                traits=DataTraits(
                    relational=bool(traits.get("relational", False)),
                    flexible_query=bool(traits.get("flexible_query", False)),
                    key_lookup_only=bool(traits.get("key_lookup_only", False)),
                ),
            ),
            # Order-insensitive fields are sorted so that input order never changes output.
            allowlist_cidrs=tuple(sorted(set(d.get("allowlist_cidrs") or ()))),
            budget_usd_month=budget,
            env_keys=tuple(sorted(set((config.get("env") or {}).keys()))),
            secret_keys=tuple(sorted(set(config.get("secrets") or ()))),
        )
        if raw.exposure == "IP_ALLOWLIST" and not raw.allowlist_cidrs:
            raise InputError("allowlist_cidrs: required when exposure is IP_ALLOWLIST")
        return raw

    def to_canonical(self) -> dict:
        return {
            "app_form": self.app_form,
            "traffic_scale": self.traffic_scale,
            "traffic_pattern": self.traffic_pattern,
            "downtime_tolerance": self.downtime_tolerance,
            "latency_sensitivity": self.latency_sensitivity,
            "exposure": self.exposure,
            "tier": self.tier,
            "capabilities": {
                "realtime_connection": self.capabilities.realtime_connection,
                "request_duration": self.capabilities.request_duration,
                "large_payload": self.capabilities.large_payload,
                "in_memory_state": self.capabilities.in_memory_state,
                "non_http": self.capabilities.non_http,
            },
            "data": {
                "needed": self.data.needed,
                "detected": self.data.detected,
                "declared": self.data.declared,
                "traits": {
                    "relational": self.data.traits.relational,
                    "flexible_query": self.data.traits.flexible_query,
                    "key_lookup_only": self.data.traits.key_lookup_only,
                },
            },
            "allowlist_cidrs": list(self.allowlist_cidrs),
            "budget_usd_month": self.budget_usd_month,
            "config": {"env": list(self.env_keys), "secrets": list(self.secret_keys)},
        }

    def input_hash(self) -> str:
        blob = json.dumps(self.to_canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()

    def replace(self, **changes) -> RawInput:
        d = {f: getattr(self, f) for f in self.__dataclass_fields__}
        d.update(changes)
        return RawInput(**d)


@dataclass(frozen=True)
class Load:
    users: float
    monthly_requests: float
    peak_rps: float
    avg_rps: float


@dataclass(frozen=True)
class Requirement:
    raw: RawInput
    has_frontend: bool
    has_backend: bool
    load_rep: Load
    load_upper: Load
    active_ratio: float
    availability: str  # A0 | A1 | A2
    cold_start_ok: bool
    data_kind: str = "NONE"  # NONE | KEY_VALUE | RELATIONAL (set by db rules)
    data_source: str = "NONE"
    peak_concurrency: int = field(default=1)


def _load(users: float, pattern: str) -> Load:
    daily = users * C.REQUESTS_PER_USER_PER_DAY
    avg_rps = daily / 86_400
    return Load(
        users=users,
        monthly_requests=daily * C.DAYS_PER_MONTH,
        peak_rps=avg_rps * C.PEAK_FACTOR[pattern],
        avg_rps=avg_rps,
    )


def normalize(raw: RawInput) -> Requirement:
    upper = C.BUCKET_UPPER_USERS[raw.traffic_scale]
    load_upper = _load(upper, raw.traffic_pattern)
    load_rep = _load(upper * C.REP_RATIO, raw.traffic_pattern)
    return Requirement(
        raw=raw,
        has_frontend=raw.app_form in ("STATIC_SITE", "STATIC_PLUS_API"),
        has_backend=raw.app_form in ("SERVER_APP", "STATIC_PLUS_API"),
        load_rep=load_rep,
        load_upper=load_upper,
        active_ratio=C.ACTIVE_RATIO[raw.traffic_pattern],
        availability=AVAILABILITY_LEVEL[raw.downtime_tolerance],
        cold_start_ok=raw.latency_sensitivity == "TOLERANT",
        peak_concurrency=max(1, math.ceil(load_upper.peak_rps * C.LAMBDA_AVG_DURATION_S)),
    )
