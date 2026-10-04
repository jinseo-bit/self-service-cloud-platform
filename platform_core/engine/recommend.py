"""Recommendation pipeline (docs/recommendation-engine.md §1).

recommend(raw, prices) is a pure function: Normalize → Gate → Enumerate → Filter →
Rank → Budget → Attach policy. Platform-state checks (platform cap, quota) live in
admission.py and never change the architecture.
"""

from __future__ import annotations

import dataclasses
import math

from .. import baseline
from ..pricing import PriceTable
from . import constants as C
from .cost import Candidate, CostEstimate, ctr_max_tasks, ctr_min_tasks, dollars, estimate, rds_class, uses_spot
from .model import RawInput, Requirement, normalize
from .reasons import R, Reason

STAGES = ("FUNCTIONAL", "SECURITY", "AVAILABILITY", "PERFORMANCE")
RDB = ("RDS_SAZ", "RDS_MAZ", "AUR_PAUSE", "AUR_MAZ")
SLS_VARIANTS = ("SLS", "SLS_PC")

# §9.6: every fixed-cost line item must map to a justification code.
FIXED_JUSTIFICATION = {
    "waf": "FIX_WAF_FOR_IP_ALLOWLIST",
    "lambda-provisioned-concurrency": "FIX_PC_FOR_LATENCY",
    "secretsmanager-endpoint": "FIX_ENDPOINT_FOR_VPC_LAMBDA",
    "fargate-min-tasks": "FIX_ALWAYS_ON_COMPUTE",
    "alb": "FIX_ALB_FOR_CONTAINER",
    "rds-instance": "FIX_RDB_FOR_RELATIONAL_DATA",
    "rds-storage": "FIX_RDB_FOR_RELATIONAL_DATA",
    "aurora-min-acu": "FIX_RDB_FOR_RELATIONAL_DATA",
    "db-secret": "FIX_SECRET_STORAGE",
    "app-secrets": "FIX_SECRET_STORAGE",
    "ecr": "FIX_IMAGE_REGISTRY",
    "prod-monitoring": "FIX_PROD_MONITORING",
}


# ---------------------------------------------------------------- gate (§16, §17)

def _gate(raw: RawInput) -> tuple[list[Reason], list[Reason]]:
    unsupported, conflicts = [], []
    backend = raw.app_form in ("SERVER_APP", "STATIC_PLUS_API")
    if backend:
        if raw.capabilities.non_http:
            unsupported.append(R("UNSUP_NON_HTTP"))
        if raw.capabilities.request_duration == "OVER_120S":
            unsupported.append(R("UNSUP_REQUEST_OVER_120S"))
        if raw.traffic_scale == "T4":
            unsupported.append(R("UNSUP_TRAFFIC_SCALE"))
    if raw.data.needed and raw.data.detected == "OTHER":
        unsupported.append(R("UNSUP_DB_ENGINE"))
    if raw.app_form == "STATIC_SITE":
        if raw.data.needed:
            conflicts.append(R("CONF_STATIC_WITH_DATA"))
        if raw.secret_keys:
            conflicts.append(R("CONF_STATIC_SECRETS"))
    d = raw.data
    if d.needed and d.detected in ("POSTGRES", "DYNAMODB") and d.declared != "UNKNOWN" and d.detected != d.declared:
        conflicts.append(R("CONF_DB_HINT_MISMATCH", detected=d.detected, declared=d.declared))
    return unsupported, conflicts


# ---------------------------------------------------------------- db rules (§5)

def _data_kind(raw: RawInput) -> tuple[str, str, Reason, list[Reason]]:
    d = raw.data
    if not d.needed:
        return "NONE", "NONE", R("SEL_DB_NONE"), []
    engine = d.detected if d.detected in ("POSTGRES", "DYNAMODB") else None
    if engine:
        kind = "RELATIONAL" if engine == "POSTGRES" else "KEY_VALUE"
        return kind, "DETECTED", R("SEL_DB_DETECTED", engine=_engine_name(engine)), []
    if d.declared != "UNKNOWN":
        kind = "RELATIONAL" if d.declared == "POSTGRES" else "KEY_VALUE"
        return kind, "DECLARED", R("SEL_DB_DECLARED", engine=_engine_name(d.declared)), []
    t = d.traits
    if t.relational or t.flexible_query:
        return "RELATIONAL", "INFERRED", R("SEL_DB_INFERRED_RELATIONAL"), []
    if t.key_lookup_only:
        return "KEY_VALUE", "INFERRED", R("SEL_DB_INFERRED_KEY_VALUE"), []
    return "RELATIONAL", "DEFAULTED", R("SEL_DB_DEFAULTED"), [R("WARN_DB_DEFAULTED")]


def _engine_name(e: str) -> str:
    return {"POSTGRES": "PostgreSQL", "DYNAMODB": "DynamoDB"}[e]


# ---------------------------------------------------------------- enumerate + filter (§3, §4, §6, §7)

def _enumerate(req: Requirement) -> list[Candidate]:
    computes = ["NONE", "SLS", "SLS_PC", "CTR"] if req.has_backend else ["NONE"]
    dbs = ["DDB", *RDB] if req.raw.data.needed else ["NONE"]
    return [Candidate(c, d) for c in computes for d in dbs]


def _exclusion(c: Candidate, req: Requirement) -> tuple[str, Reason] | None:
    raw, caps = req.raw, req.raw.capabilities
    # FUNCTIONAL
    if req.has_backend and c.compute == "NONE":
        return "FUNCTIONAL", R("EXC_STATIC_NEEDS_SERVER")
    if c.compute in SLS_VARIANTS:
        if caps.realtime_connection:
            return "FUNCTIONAL", R("EXC_SLS_REALTIME")
        if caps.request_duration != "SECONDS":  # HTTP API hard limit 30 s: keep headroom (D-157)
            return "FUNCTIONAL", R("EXC_SLS_TIMEOUT")
        if caps.large_payload:
            return "FUNCTIONAL", R("EXC_SLS_PAYLOAD")
        if caps.in_memory_state:
            return "FUNCTIONAL", R("EXC_SLS_STATEFUL")
    if c.db != "NONE":
        wanted = "KEY_VALUE" if c.db == "DDB" else "RELATIONAL"
        if wanted != req.data_kind:
            return "FUNCTIONAL", R("EXC_DB_KIND_MISMATCH")
    # SECURITY
    if c.compute in SLS_VARIANTS and raw.exposure == "IP_ALLOWLIST":
        return "SECURITY", R("EXC_SLS_IP_ALLOWLIST")
    # AVAILABILITY
    if c.db in ("RDS_SAZ", "AUR_PAUSE") and req.availability != "A0":
        return "AVAILABILITY", R("EXC_DB_SINGLE_AZ")
    # PERFORMANCE
    if not req.cold_start_ok:
        if c.compute == "SLS":
            return "PERFORMANCE", R("EXC_SLS_COLD_START")
        if c.db == "AUR_PAUSE":
            return "PERFORMANCE", R("EXC_AURORA_PAUSE_LATENCY")
    return None


def _rank_key(c: Candidate, est: CostEstimate):
    return (dollars(est.total_cents), c.complexity, est.fixed_cents, c.id)


# ---------------------------------------------------------------- core

def _select(req: Requirement, prices: PriceTable):
    """Returns (selected, selected_estimates, candidate_records, selection_reasons)."""
    records, survivors = [], []
    for cand in _enumerate(req):
        excl = _exclusion(cand, req)
        if excl:
            stage, reason = excl
            records.append({"candidate_id": cand.id, "status": "EXCLUDED", "stage": stage,
                            "reasons": [reason.to_dict()]})
        else:
            survivors.append((cand, estimate(cand, req, req.load_rep, prices)))
    if not survivors:
        return None, None, records, [R("CONF_NO_CANDIDATE")]

    survivors.sort(key=lambda s: _rank_key(*s))
    best, best_est = survivors[0]
    best_usd = dollars(best_est.total_cents)
    sel_reasons: list[Reason] = []
    if len(survivors) == 1:
        sel_reasons.append(R("SEL_ONLY_CANDIDATE"))
    else:
        runner, runner_est = survivors[1]
        delta = dollars(runner_est.total_cents) - best_usd
        if delta == 0:
            sel_reasons.append(R("SEL_TIEBREAK_SIMPLER", other=runner.id))
        else:
            sel_reasons.append(R("SEL_LOWEST_COST", runner_up=runner.id, delta_usd=delta))

    for rank, (cand, est) in enumerate(survivors, start=1):
        usd = dollars(est.total_cents)
        rec = {"candidate_id": cand.id, "status": "SELECTED" if rank == 1 else "RANKED",
               "rank": rank, "cost_rep_usd": usd, "reasons": []}
        if rank > 1:
            delta = usd - best_usd
            reason = R("EXC_HIGHER_COST", delta_usd=delta) if delta > 0 else R("EXC_TIEBREAK_COMPLEX")
            rec["stage"] = "COST"
            rec["reasons"] = [reason.to_dict()]
        records.append(rec)
    return best, best_est, records, sel_reasons


def _az(c: Candidate, req: Requirement) -> tuple[str, list[Reason]]:
    reasons: list[Reason] = []
    if req.has_frontend:
        reasons.append(R("SEL_AZ_INHERENT", component="CloudFront·S3"))
    if c.compute in SLS_VARIANTS:
        reasons.append(R("SEL_AZ_INHERENT", component="Lambda·API Gateway"))
    if c.db == "DDB":
        reasons.append(R("SEL_AZ_INHERENT", component="DynamoDB"))
    stateful = c.compute == "CTR" or c.db in RDB
    if not stateful:
        return "INHERENT", reasons
    if req.availability == "A0":
        reasons.append(R("SEL_AZ_SINGLE_ALLOWED"))
        return "SINGLE", reasons
    parts = []
    if c.compute == "CTR":
        parts.append("컨테이너")
    if c.db in RDB:
        parts.append("DB")
    reasons.append(R("SEL_AZ_MULTI_REQUIRED", component="·".join(parts)))
    if req.availability == "A2" and c.compute == "CTR":
        reasons.append(R("SEL_STATIC_STABILITY"))
        return "MULTI_STATIC_STABLE", reasons
    return "MULTI", reasons


def _scaling_and_policies(c: Candidate, req: Requirement) -> tuple[dict, list[Reason]]:
    raw = req.raw
    scaling: dict = {}
    pol: list[Reason] = []
    if raw.tier == "DEV":
        pol += [R("POL_TIER_DEV"), R("POL_TTL_DEV", days=C.TTL_DAYS_DEV)]
    else:
        pol.append(R("POL_TIER_PROD_APPROVAL"))

    if c.compute in SLS_VARIANTS:
        cap = min(C.LAMBDA_CONCURRENCY_PLATFORM_CAP, max(C.LAMBDA_CONCURRENCY_MIN, 2 * req.peak_concurrency))
        if c.db in RDB:
            max_conn = C.RDS_MAX_CONNECTIONS.get(rds_class(req), 100)
            guard = min(C.LAMBDA_CONCURRENCY_BUCKET_CAP[raw.traffic_scale], math.floor(max_conn * 0.5))
            cap = min(cap, guard)
            pol.append(R("POL_DB_CONNECTION_GUARD", limit=cap))
        else:
            pol.append(R("POL_LAMBDA_CONCURRENCY_CAP", limit=cap))
        rate = max(C.THROTTLE_MIN_RATE, math.ceil(2 * req.load_upper.peak_rps))
        scaling.update(lambda_reserved_concurrency=cap, apigw_rate=rate, apigw_burst=2 * rate)
        if c.compute == "SLS_PC":
            scaling["lambda_provisioned_concurrency"] = req.peak_concurrency
        pol.append(R("POL_THROTTLE", rate=rate, burst=2 * rate))

    if c.compute == "CTR":
        lo, hi = ctr_min_tasks(req), ctr_max_tasks(req)
        scaling.update(ctr_min_tasks=lo, ctr_max_tasks=hi, ctr_cpu_target_percent=60,
                       ctr_capacity="FARGATE_SPOT" if uses_spot(req) else "FARGATE",
                       ctr_task_vcpu=C.TASK_VCPU, ctr_task_memory_gb=C.TASK_MEMORY_GB)
        pol.append(R("POL_CTR_AUTOSCALING", min=lo, max=hi))
        origin_s = C.CTR_ORIGIN_TIMEOUT_S[raw.capabilities.request_duration]
        scaling.update(edge_origin_response_timeout_s=origin_s, alb_idle_timeout_s=origin_s + C.ALB_IDLE_MARGIN_S)
        if origin_s > 30:
            pol.append(R("POL_LONG_REQUEST_TIMEOUTS", origin_timeout_s=origin_s, alb_idle_timeout_s=origin_s + C.ALB_IDLE_MARGIN_S))
        if uses_spot(req):
            pol.append(R("POL_SPOT_DEV_A0"))

    if c.db == "DDB":
        scaling["dynamodb_mode"] = "ON_DEMAND_WITH_MAX_THROUGHPUT"
        pol.append(R("POL_DDB_MAX_THROUGHPUT"))
    elif c.db in ("RDS_SAZ", "RDS_MAZ"):
        scaling.update(rds_class=rds_class(req), rds_storage_gb=C.RDS_STORAGE_GB,
                       rds_max_storage_gb=2 * C.RDS_STORAGE_GB)
    elif c.db == "AUR_PAUSE":
        scaling.update(aurora_min_acu=C.AURORA_PAUSE_ACU[0], aurora_max_acu=C.AURORA_PAUSE_ACU[1])
    elif c.db == "AUR_MAZ":
        scaling.update(aurora_min_acu=C.AURORA_MAZ_ACU[0], aurora_max_acu=C.AURORA_MAZ_ACU[1])
    return scaling, pol


def _warnings(c: Candidate, req: Requirement, multi_az: str) -> list[Reason]:
    w: list[Reason] = []
    raw = req.raw
    if raw.tier == "DEV" and multi_az in ("MULTI", "MULTI_STATIC_STABLE"):
        w.append(R("WARN_DEV_MULTI_AZ_COST"))
    if c.compute == "CTR" and raw.capabilities.in_memory_state and ctr_max_tasks(req) > 1:
        w.append(R("WARN_STICKY_STATE"))
    if not req.cold_start_ok and raw.traffic_pattern == "SPIKY" and c.compute in ("SLS_PC", "CTR"):
        w.append(R("WARN_SPIKE_PREWARM_COST"))
    if req.availability == "A2":
        w.append(R("WARN_REGION_SCOPE"))
    if c.compute in SLS_VARIANTS and c.db in RDB:
        w.append(R("WARN_NO_INTERNET_EGRESS"))
    return w


def _fixed_justifications(c: Candidate, est: CostEstimate, req: Requirement, multi_az: str) -> list[Reason]:
    out: list[Reason] = []
    seen = set()
    for item in est.items:
        if item.kind != "FIXED" or item.cents == 0:
            continue
        code = FIXED_JUSTIFICATION[item.resource]  # KeyError = unjustified fixed cost (test fails)
        if code in seen:
            continue
        seen.add(code)
        if code == "FIX_PC_FOR_LATENCY":
            out.append(R(code, count=req.peak_concurrency))
        elif code == "FIX_ALWAYS_ON_COMPUTE":
            out.append(R(code, count=ctr_min_tasks(req)))
        else:
            out.append(R(code))
    if multi_az in ("MULTI", "MULTI_STATIC_STABLE"):
        out.append(R("FIX_MULTI_AZ_FOR_AVAILABILITY"))
    return out


def _shared(c: Candidate) -> list[str]:
    deps = ["ACCOUNT_BASELINE"]
    if c.compute == "CTR" or c.db in RDB:
        deps.append("SHARED_VPC")
    if c.compute == "CTR":
        deps += ["SHARED_ECS_CLUSTER", "SHARED_INTERNAL_ALB"]
    if c.compute in SLS_VARIANTS and c.db in RDB:
        deps.append("SHARED_SM_ENDPOINT")
    if c.compute in ("SLS", "SLS_PC", "CTR"):
        deps.append("SHARED_ECR")
    return deps


def _components(c: Candidate, req: Requirement) -> set[str]:
    tags = set()
    if req.has_frontend:
        tags.add("S")
    if c.compute in SLS_VARIANTS:
        tags.add("L")
    if c.compute == "CTR":
        tags.add("C")
    if c.db in RDB:
        tags.add("R")
    if c.db == "DDB":
        tags.add("D")
    if req.raw.exposure == "IP_ALLOWLIST":
        tags.add("IP")
    if "SHARED_VPC" in _shared(c):
        tags.add("SHARED_VPC")
    return tags


def _cost_block(est_rep: CostEstimate, est_upper: CostEstimate, budget_usd: int) -> dict:
    return {
        "rep_usd": dollars(est_rep.total_cents),
        "upper_usd": dollars(est_upper.total_cents),
        "fixed_usd": dollars(est_upper.fixed_cents),
        "variable_upper_usd": dollars(est_upper.total_cents - est_upper.fixed_cents),
        "shared_allocated_usd": dollars(est_upper.shared_cents),
        "budget_usd": budget_usd,
        "line_items_upper": [
            {"resource": i.resource, "kind": i.kind, "cents": i.cents, "shared": i.shared}
            for i in est_upper.items
        ],
    }


def _base(raw: RawInput, prices: PriceTable) -> dict:
    return {
        "rules_version": C.RULES_VERSION,
        "pricing_snapshot_id": prices.snapshot_id,
        "pricing": prices.metadata(),
        "input_hash": raw.input_hash(),
    }


def recommend(raw: RawInput, prices: PriceTable, *, allow_relax: bool = True) -> dict:
    out = _base(raw, prices)
    unsupported, conflicts = _gate(raw)
    if unsupported:
        out.update(status="UNSUPPORTED", reasons=[r.to_dict() for r in unsupported], causes=_causes(raw, unsupported))
        if allow_relax:
            out["nearest"] = _nearest(raw, prices, unsupported)
            out["alternatives"] = _alternatives(unsupported)
        return out
    if conflicts:
        out.update(status="NEEDS_INPUT", reasons=[r.to_dict() for r in conflicts], causes=_causes(raw, conflicts))
        return out

    req = normalize(raw)
    kind, source, db_reason, db_warnings = _data_kind(raw)
    req = dataclasses.replace(req, data_kind=kind, data_source=source)

    best, est_rep, records, sel_reasons = _select(req, prices)
    if best is None:
        out.update(status="NEEDS_INPUT", reasons=[r.to_dict() for r in sel_reasons], candidates=records)
        return out

    est_upper = estimate(best, req, req.load_upper, prices)
    budget_usd = raw.budget_usd_month if raw.budget_usd_month is not None else C.DEFAULT_ENV_BUDGET_USD
    multi_az, az_reasons = _az(best, req)
    scaling, policies = _scaling_and_policies(best, req)
    warnings = db_warnings + _warnings(best, req, multi_az)
    fixed = _fixed_justifications(best, est_upper, req, multi_az)

    reasons: list[Reason] = []
    if req.has_frontend:
        reasons.append(R("SEL_STATIC_FRONTEND"))
    reasons += sel_reasons + [db_reason] + az_reasons + fixed
    if any(i.shared for i in est_upper.items):
        reasons.append(R("INFO_SHARED_MARGINAL_COST", n=C.SHARED_SPLIT_N))
    if best.compute == "CTR" and raw.traffic_scale == "T3":
        reasons.append(R("INFO_EKS_OUT_OF_SCOPE"))

    components = _components(best, req)
    sb = baseline.applicable(components)
    reasons.append(R("SEC_BASELINE_APPLIED", count=len(sb)))

    upper_usd = dollars(est_upper.total_cents)
    budget: dict = {"status": "WITHIN"}
    if upper_usd > budget_usd:
        budget = {"status": "OVER", "reasons": [R("BUD_OVER_USER_LIMIT", cost_usd=upper_usd, budget_usd=budget_usd).to_dict()]}
        if allow_relax:
            budget["relax_options"] = _relax_options(raw, prices, budget_usd, upper_usd)
    elif budget_usd > 0 and upper_usd >= C.NEAR_BUDGET_RATIO * budget_usd:
        warnings.append(R("WARN_NEAR_BUDGET", percent=int(100 * upper_usd / budget_usd)))

    if budget["status"] == "OVER":
        status = "OVER_BUDGET"  # soft guard (D-158): the user may adjust or explicitly override
    elif warnings:
        status = "RECOMMENDED_WITH_WARNINGS"
    else:
        status = "RECOMMENDED"

    out.update(
        status=status,
        architecture={
            "frontend": "STATIC" if req.has_frontend else "NONE",
            "compute": best.compute,
            "db": best.db,
            "candidate_id": best.id,
            "multi_az": multi_az,
            "tier": raw.tier,
            "exposure": raw.exposure,
            "scaling": dict(sorted(scaling.items())),
            "shared_dependencies": _shared(best),
        },
        provisionable=(best.compute, best.db) in C.PROVISIONABLE,
        cost=_cost_block(est_rep, est_upper, budget_usd),
        budget=budget,
        reasons=[r.to_dict() for r in reasons],
        warnings=[w.to_dict() for w in warnings],
        policies=[p.to_dict() for p in policies],
        candidates=sorted(records, key=lambda r: (r.get("rank", 999), r["candidate_id"])),
        security_baseline=sb,
        guardrails={
            "ttl_days": C.TTL_DAYS_DEV if raw.tier == "DEV" else None,
            "log_retention_days": C.LOG_RETENTION_DAYS[raw.tier],
            "budget_guard": "SOFT",  # user budget: override allowed; platform cap/quota: HARD (admission)
            "budget_usd": budget_usd,
        },
    )
    if not out["provisionable"]:
        out["warnings"].append(R("UNSUP_BLUEPRINT_NOT_YET", blueprint=best.id).to_dict())
    return out


# ---------------------------------------------------------------- relaxation & nearest (§9.5, §17)

# Only preferences the user can trade for money are relaxed — never facts (traffic, data, features),
# exposure or security (D-136, D-159).
RELAXABLE = (
    ("downtime_tolerance", ("NEVER", "NOT_IN_BUSINESS_HOURS", "OK")),
    ("latency_sensitivity", ("SENSITIVE", "TOLERANT")),
)
FIELD_LABEL = {"downtime_tolerance": "멈춤 허용도", "latency_sensitivity": "응답 속도"}
VALUE_LABEL = {
    "downtime_tolerance": {"NEVER": "절대 멈추면 안 돼요", "NOT_IN_BUSINESS_HOURS": "업무시간엔 멈추면 안 돼요",
                           "OK": "잠깐 멈춰도 괜찮아요"},
    "latency_sensitivity": {"SENSITIVE": "항상 빨라야 해요", "TOLERANT": "첫 요청이 조금 느려도 괜찮아요"},
}


def _change(raw: RawInput, field: str, value: str) -> dict:
    return {"field": field, "label": FIELD_LABEL[field], "from": getattr(raw, field), "to": value,
            "from_label": VALUE_LABEL[field][getattr(raw, field)], "to_label": VALUE_LABEL[field][value]}


def _relax_options(raw: RawInput, prices: PriceTable, budget_usd: int, upper_usd: int) -> list[dict]:
    """D-159. Options that fit the budget come first, fewest changes first (singles before pairs).
    If nothing fits, show the changes that still lower the cost (marked fits_budget=False).
    The last option is always 'raise the budget'. Nothing is applied until the user confirms."""
    lowered = {f: ladder[ladder.index(getattr(raw, f)) + 1:] for f, ladder in RELAXABLE}
    combos = [[(f, v)] for f, _ in RELAXABLE for v in lowered[f]]
    combos += [[("downtime_tolerance", d), ("latency_sensitivity", lat)]
               for d in lowered["downtime_tolerance"] for lat in lowered["latency_sensitivity"]]
    options = []
    for order, combo in enumerate(combos):
        alt = recommend(raw.replace(**dict(combo)), prices, allow_relax=False)
        if alt["status"] not in ("RECOMMENDED", "RECOMMENDED_WITH_WARNINGS", "OVER_BUDGET"):
            continue
        cost = alt["cost"]["upper_usd"]
        if cost >= upper_usd:
            continue  # does not lower the cost
        changes = [_change(raw, f, v) for f, v in combo]
        text = ", ".join(f"{c['label']}를 '{c['to_label']}'로" for c in changes)  # both labels end in a vowel
        fits = cost <= budget_usd
        reason = (R("BUD_RELAX_OPTION", changes=text, cost_usd=cost) if fits
                  else R("BUD_RELAX_PARTIAL", changes=text, cost_usd=cost, saved_usd=upper_usd - cost))
        options.append(((not fits, len(changes), cost, order), {
            "changes": changes, "fits_budget": fits, "cost_rep_usd": alt["cost"]["rep_usd"], "cost_upper_usd": cost,
            "saved_usd": upper_usd - cost, "candidate_id": alt["architecture"]["candidate_id"],
            "multi_az": alt["architecture"]["multi_az"], "reason": reason.to_dict(),
        }))
    options.sort(key=lambda o: o[0])
    if options and options[0][1]["fits_budget"]:
        fewest = options[0][0][1]
        chosen = [o[1] for o in options if o[1]["fits_budget"] and len(o[1]["changes"]) == fewest]
    else:
        chosen = [o[1] for o in options][:3]
    chosen.append({"changes": [{"field": "budget_usd_month", "label": "예산", "from": budget_usd, "to": upper_usd,
                                "from_label": f"월 ${budget_usd}", "to_label": f"월 ${upper_usd}"}],
                   "fits_budget": True, "cost_upper_usd": upper_usd, "reason": R("BUD_RAISE_BUDGET", cost_usd=upper_usd).to_dict()})
    return chosen


# Which answer caused a gate result — the UI jumps straight to that question (D-160).
CAUSE_FIELD = {
    "UNSUP_TRAFFIC_SCALE": "traffic_scale", "UNSUP_REQUEST_OVER_120S": "request_duration", "UNSUP_NON_HTTP": "capabilities",
    "UNSUP_DB_ENGINE": "data", "CONF_STATIC_WITH_DATA": "data", "CONF_STATIC_SECRETS": "config",
    "CONF_DB_HINT_MISMATCH": "data",
}


def _causes(raw: RawInput, reasons: list[Reason]) -> list[dict]:
    value = {"traffic_scale": raw.traffic_scale, "request_duration": raw.capabilities.request_duration,
             "capabilities": "non_http", "data": raw.data.declared if raw.data.needed else "NONE", "config": "secrets"}
    return [{"code": r.code, "field": CAUSE_FIELD[r.code], "value": value[CAUSE_FIELD[r.code]]}
            for r in reasons if r.code in CAUSE_FIELD]


def _alternatives(unsupported: list[Reason]) -> list[dict]:
    codes = {r.code for r in unsupported}
    if "UNSUP_REQUEST_OVER_120S" in codes:
        return [R("ALT_ASYNC_JOB").to_dict(), R("ALT_SPLIT_UNDER_2MIN").to_dict()]
    return []


def _nearest(raw: RawInput, prices: PriceTable, unsupported: list[Reason]) -> dict | None:
    codes = {r.code for r in unsupported}
    if codes == {"UNSUP_TRAFFIC_SCALE"}:
        alt = recommend(raw.replace(traffic_scale="T3"), prices, allow_relax=False)
        if alt["status"] != "UNSUPPORTED":
            return {"changes": {"traffic_scale": "T3"}, "requires_confirmation": True,
                    "candidate_id": alt.get("architecture", {}).get("candidate_id"),
                    "cost_upper_usd": alt.get("cost", {}).get("upper_usd")}
    if codes == {"UNSUP_REQUEST_OVER_120S"}:
        caps = dataclasses.replace(raw.capabilities, request_duration="S60_TO_120S")
        alt = recommend(raw.replace(capabilities=caps), prices, allow_relax=False)
        if alt["status"] not in ("UNSUPPORTED", "NEEDS_INPUT"):
            return {"changes": {"capabilities.request_duration": "S60_TO_120S"}, "requires_confirmation": True,
                    "candidate_id": alt["architecture"]["candidate_id"], "cost_upper_usd": alt["cost"]["upper_usd"],
                    "note": "작업을 2분 안에 끝나도록 나누는 경우"}
    if codes == {"UNSUP_DB_ENGINE"}:
        return {"changes": {"data.detected": "POSTGRES"}, "requires_confirmation": True,
                "note": "앱이 PostgreSQL을 사용하도록 바뀌면 지원할 수 있습니다."}
    return None
