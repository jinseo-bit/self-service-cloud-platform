"""Cost model (docs/recommendation-engine.md §9.1).

All amounts are integer cents. Each line item is tagged fixed/variable so the UI can
show them separately (D-72) and so §9.6 can check that every fixed cost is justified.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..pricing import PriceTable
from . import constants as C
from .model import Load, Requirement

H = C.HOURS_PER_MONTH


@dataclass(frozen=True)
class LineItem:
    resource: str
    kind: str  # FIXED | VARIABLE
    cents: int
    shared: bool = False


def _c(usd: float) -> int:
    return int(round(usd * 100))


@dataclass(frozen=True)
class Candidate:
    compute: str  # NONE | SLS | SLS_PC | CTR
    db: str  # NONE | DDB | RDS_SAZ | RDS_MAZ | AUR_PAUSE | AUR_MAZ

    @property
    def id(self) -> str:
        return f"{self.compute}+{self.db}"

    @property
    def complexity(self) -> int:
        return C.COMPLEXITY[self.compute] + C.COMPLEXITY["DB_NONE" if self.db == "NONE" else self.db]


def ctr_min_tasks(req: Requirement) -> int:
    if req.availability == "A0":
        base = 1
    elif req.availability == "A1":
        base = 2
    else:  # A2: static stability — each of 2 AZs carries peak alone
        base = 2 * max(1, math.ceil(req.load_upper.peak_rps / C.TASK_RPS_CAPACITY))
    if not req.cold_start_ok and req.raw.traffic_pattern == "SPIKY":
        base = max(base, math.ceil(req.load_upper.peak_rps / C.TASK_RPS_CAPACITY))
    return base


def ctr_max_tasks(req: Requirement) -> int:
    wanted = 2 * max(1, math.ceil(req.load_upper.peak_rps / C.TASK_RPS_CAPACITY))
    return max(ctr_min_tasks(req), min(C.CTR_MAX_TASKS_PLATFORM_CAP, wanted))


def uses_spot(req: Requirement) -> bool:
    return req.raw.tier == "DEV" and req.availability == "A0"


def rds_class(req: Requirement) -> str:
    return C.RDS_CLASS_BY_SCALE[req.raw.traffic_scale]


def _cloudfront(p: PriceTable, requests: float, avg_kb: float) -> int:
    gb = requests * avg_kb / 1_000_000
    return _c(requests / 10_000 * p["cloudfront.request_https_per_10k"] + gb * p["cloudfront.transfer_out_per_gb"])


def line_items(cand: Candidate, req: Requirement, load: Load, p: PriceTable) -> list[LineItem]:
    items: list[LineItem] = []
    reqs = load.monthly_requests
    raw = req.raw

    if req.has_frontend:
        items.append(LineItem("cloudfront-static", "VARIABLE", _cloudfront(p, reqs, C.STATIC_AVG_RESPONSE_KB)))
        items.append(LineItem("s3-site", "VARIABLE", _c(
            C.STATIC_STORAGE_GB * p["s3.storage_gb_month"]
            + reqs * C.STATIC_CACHE_MISS_RATIO / 1000 * p["s3.get_per_1k"])))

    if raw.exposure == "IP_ALLOWLIST":
        items.append(LineItem("waf", "FIXED", _c(p["waf.web_acl_month"] + p["waf.rule_month"])))
        items.append(LineItem("waf-requests", "VARIABLE", _c(reqs / 1e6 * p["waf.request_per_1m"])))

    if cand.compute in ("SLS", "SLS_PC"):
        dur_price = p["lambda.pc_duration_gb_second"] if cand.compute == "SLS_PC" else p["lambda.gb_second"]
        gb_s = reqs * C.LAMBDA_AVG_DURATION_S * C.LAMBDA_MEMORY_GB
        items.append(LineItem("lambda", "VARIABLE", _c(reqs / 1e6 * p["lambda.request_per_1m"] + gb_s * dur_price)))
        items.append(LineItem("apigw", "VARIABLE", _c(reqs / 1e6 * p["apigw.http_request_per_1m"])))
        items.append(LineItem("logs", "VARIABLE", _c(reqs * C.LOG_BYTES_PER_REQUEST / 1e9 * p["cloudwatch.logs_ingest_gb"])))
        items.append(LineItem("ecr", "FIXED", _c(0.5 * p["ecr.storage_gb_month"])))
        if cand.compute == "SLS_PC":
            pc = req.peak_concurrency
            items.append(LineItem("lambda-provisioned-concurrency", "FIXED",
                                  _c(pc * C.LAMBDA_MEMORY_GB * H * 3600 * p["lambda.pc_gb_second"])))
        if cand.db not in ("NONE", "DDB"):
            endpoint = 2 * H * p["vpce.interface_hour_per_az"] / C.SHARED_SPLIT_N
            items.append(LineItem("secretsmanager-endpoint", "FIXED", _c(endpoint), shared=True))

    if cand.compute == "CTR":
        spot = uses_spot(req)
        vcpu = p["fargate_spot.vcpu_hour" if spot else "fargate.vcpu_hour"]
        mem = p["fargate_spot.gb_hour" if spot else "fargate.gb_hour"]
        task_hour = C.TASK_VCPU * vcpu + C.TASK_MEMORY_GB * mem + p["vpc.public_ipv4_hour"]
        min_tasks = ctr_min_tasks(req)
        running = max(min_tasks, math.ceil(load.avg_rps / C.TASK_RPS_CAPACITY))
        items.append(LineItem("fargate-min-tasks", "FIXED", _c(min_tasks * task_hour * H)))
        if running > min_tasks:
            items.append(LineItem("fargate-scaled-tasks", "VARIABLE", _c((running - min_tasks) * task_hour * H)))
        items.append(LineItem("alb", "FIXED", _c(H * p["alb.hour"] / C.SHARED_SPLIT_N), shared=True))
        lcu = max(0.05, load.avg_rps / 25)
        items.append(LineItem("alb-lcu", "VARIABLE", _c(lcu * H * p["alb.lcu_hour"])))
        items.append(LineItem("cloudfront-api", "VARIABLE", _cloudfront(p, reqs, C.API_AVG_RESPONSE_KB)))
        items.append(LineItem("logs", "VARIABLE", _c(reqs * C.LOG_BYTES_PER_REQUEST / 1e9 * p["cloudwatch.logs_ingest_gb"])))
        items.append(LineItem("ecr", "FIXED", _c(0.5 * p["ecr.storage_gb_month"])))

    if cand.db == "DDB":
        writes = reqs * C.DDB_WRITE_RATIO
        reads = reqs - writes
        items.append(LineItem("dynamodb", "VARIABLE", _c(
            writes / 1e6 * p["dynamodb.write_per_1m"] + reads / 1e6 * p["dynamodb.read_per_1m"]
            + C.DATA_STORAGE_GB * p["dynamodb.storage_gb_month"]
            + (C.DATA_STORAGE_GB * p["dynamodb.pitr_gb_month"] if raw.tier == "PROD" else 0))))
    elif cand.db in ("RDS_SAZ", "RDS_MAZ"):
        n = 2 if cand.db == "RDS_MAZ" else 1
        cls = rds_class(req)
        items.append(LineItem("rds-instance", "FIXED", _c(n * H * p[f"rds.{cls}.hour"])))
        items.append(LineItem("rds-storage", "FIXED", _c(n * C.RDS_STORAGE_GB * p["rds.gp3_gb_month"])))
        items.append(LineItem("db-secret", "FIXED", _c(p["secretsmanager.secret_month"])))
    elif cand.db in ("AUR_PAUSE", "AUR_MAZ"):
        demand = C.AURORA_BASE_ACU_WHEN_ACTIVE + load.avg_rps / C.AURORA_RPS_PER_ACU
        if cand.db == "AUR_PAUSE":
            lo, hi = C.AURORA_PAUSE_ACU
            acu_hours = min(hi, max(lo, demand)) * req.active_ratio * H
            items.append(LineItem("aurora-acu", "VARIABLE", _c(acu_hours * p["aurora.acu_hour"])))
        else:
            lo, hi = C.AURORA_MAZ_ACU
            floor_hours = 2 * lo * H
            used_hours = 2 * min(hi, max(lo, demand)) * H
            items.append(LineItem("aurora-min-acu", "FIXED", _c(floor_hours * p["aurora.acu_hour"])))
            if used_hours > floor_hours:
                items.append(LineItem("aurora-acu", "VARIABLE", _c((used_hours - floor_hours) * p["aurora.acu_hour"])))
        items.append(LineItem("aurora-storage-io", "VARIABLE", _c(
            C.DATA_STORAGE_GB * p["aurora.storage_gb_month"] + reqs * C.IO_PER_REQUEST / 1e6 * p["aurora.io_per_1m"])))
        items.append(LineItem("db-secret", "FIXED", _c(p["secretsmanager.secret_month"])))

    if raw.secret_keys:
        items.append(LineItem("app-secrets", "FIXED", _c(len(raw.secret_keys) * p["secretsmanager.secret_month"])))

    if raw.tier == "PROD":
        items.append(LineItem("prod-monitoring", "FIXED", _c(
            C.PROD_ALARM_COUNT * p["cloudwatch.alarm_month"] + p["cloudwatch.dashboard_month"])))

    return items


@dataclass(frozen=True)
class CostEstimate:
    items: tuple[LineItem, ...]

    @property
    def total_cents(self) -> int:
        return sum(i.cents for i in self.items)

    @property
    def fixed_cents(self) -> int:
        return sum(i.cents for i in self.items if i.kind == "FIXED")

    @property
    def shared_cents(self) -> int:
        return sum(i.cents for i in self.items if i.shared)


def estimate(cand: Candidate, req: Requirement, load: Load, p: PriceTable) -> CostEstimate:
    return CostEstimate(tuple(line_items(cand, req, load, p)))


def dollars(cents: int) -> int:
    """§9.4: compare in whole dollars (round half up)."""
    return (cents + 50) // 100
