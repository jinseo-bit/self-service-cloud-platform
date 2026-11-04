"""Policy constants bound to RULES_VERSION (docs/recommendation-engine.md §2.2, §8, §9).

Changing any value here changes engine output, so RULES_VERSION must be bumped.
"""

RULES_VERSION = "1.0.0"

HOURS_PER_MONTH = 730
DAYS_PER_MONTH = 30

# §2.2 normalization
REQUESTS_PER_USER_PER_DAY = 50
REP_RATIO = 0.3
BUCKET_UPPER_USERS = {"T1": 100, "T2": 1_000, "T3": 10_000, "T4": 100_000}
PEAK_FACTOR = {"IDLE_MOSTLY": 2.0, "BUSINESS_HOURS": 3.0, "STEADY": 1.5, "SPIKY": 10.0}
ACTIVE_RATIO = {"IDLE_MOSTLY": 0.1, "BUSINESS_HOURS": 0.35, "STEADY": 1.0, "SPIKY": 1.0}
LAMBDA_AVG_DURATION_S = 0.2
LAMBDA_MEMORY_GB = 0.5
TASK_VCPU = 0.25
TASK_MEMORY_GB = 0.5
TASK_RPS_CAPACITY = 50
STATIC_AVG_RESPONSE_KB = 50
API_AVG_RESPONSE_KB = 5
STATIC_CACHE_MISS_RATIO = 0.1
LOG_BYTES_PER_REQUEST = 1_000

# §9.3 shared infra allocation (P-04)
SHARED_SPLIT_N = 4

# §8 scaling / resource caps
LAMBDA_CONCURRENCY_BUCKET_CAP = {"T1": 10, "T2": 20, "T3": 50}
LAMBDA_CONCURRENCY_PLATFORM_CAP = 50
LAMBDA_CONCURRENCY_MIN = 5
CTR_MAX_TASKS_PLATFORM_CAP = 6
RDS_MAX_CONNECTIONS = {"db.t4g.micro": 100, "db.t4g.small": 200}
RDS_CLASS_BY_SCALE = {"T1": "db.t4g.micro", "T2": "db.t4g.micro", "T3": "db.t4g.small"}
RDS_STORAGE_GB = 20
AURORA_PAUSE_ACU = (0.0, 2.0)
AURORA_MAZ_ACU = (0.5, 4.0)
AURORA_BASE_ACU_WHEN_ACTIVE = 0.5
AURORA_RPS_PER_ACU = 50
DATA_STORAGE_GB = 1
STATIC_STORAGE_GB = 0.1
DDB_WRITE_RATIO = 0.2
IO_PER_REQUEST = 2

# §11 tier policy
TTL_DAYS_DEV = 7
LOG_RETENTION_DAYS = {"DEV": 7, "PROD": 30}
RDS_BACKUP_RETENTION_DAYS = {"DEV": 1, "PROD": 7}
PROD_ALARM_COUNT = 4

# §9.5 budget / quota (D-71, set 2026-10-04)
DEFAULT_ENV_BUDGET_USD = 10
PLATFORM_MONTHLY_CAP_USD = 30
QUOTA_PER_USER = {"DEV": 2, "PROD": 1}
NEAR_BUDGET_RATIO = 0.8

# §9.4 ranking
COMPLEXITY = {
    "NONE": 0, "SLS": 1, "SLS_PC": 2, "CTR": 3,
    "DB_NONE": 0, "DDB": 1, "RDS_SAZ": 2, "AUR_PAUSE": 3, "RDS_MAZ": 4, "AUR_MAZ": 5,
}

# Blueprints that the platform can actually provision live in platform_core/blueprints.py (D-165).

# D-157 front-door timeouts (AWS docs checked 2026-10-04)
APIGW_HTTP_TIMEOUT_S = 30          # HTTP API integration timeout, hard limit
CLOUDFRONT_ORIGIN_TIMEOUT_MAX_S = 120  # origin response timeout settable without a quota increase (1-120 s)
CTR_ORIGIN_TIMEOUT_S = {"SECONDS": 30, "ABOUT_30S": 60, "S30_TO_60S": 90, "S60_TO_120S": 120}
ALB_IDLE_MARGIN_S = 10             # ALB idle timeout (1-4000 s) = origin timeout + margin

# §8 API Gateway throttling floor: a single page view fans out into several calls,
# so 2×peak alone is too strict at T1 (peak ≈ 0.1 rps).
THROTTLE_MIN_RATE = 10
