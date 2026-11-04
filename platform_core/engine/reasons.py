"""Reason code catalog (docs/recommendation-engine.md §14).

Every code has a Korean message template for non-expert users. Unknown codes raise,
so a typo in the engine fails tests instead of producing an unexplained decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MESSAGES = {
    # selection
    "SEL_STATIC_FRONTEND": "화면 파일은 서버 없이 CDN(CloudFront)으로 바로 제공합니다.",
    "SEL_LOWEST_COST": "조건을 모두 만족하는 후보 중 예상 비용이 가장 낮습니다(다음 후보 {runner_up}보다 월 ${delta_usd} 저렴).",
    "SEL_ONLY_CANDIDATE": "조건을 모두 만족하는 후보가 이것 하나입니다.",
    "SEL_TIEBREAK_SIMPLER": "비용이 같은 후보({other}) 중 더 단순한 구성을 골랐습니다.",
    "SEL_DB_NONE": "저장할 데이터가 없어서 데이터베이스를 만들지 않습니다.",
    "SEL_DB_DETECTED": "앱 코드에서 {engine} 사용을 감지했습니다.",
    "SEL_DB_DECLARED": "앱이 {engine}을(를) 사용한다고 알려주셨습니다.",
    "SEL_DB_INFERRED_RELATIONAL": "데이터끼리 연결되거나 다양한 조건으로 검색해서 관계형 DB(PostgreSQL)가 맞습니다.",
    "SEL_DB_INFERRED_KEY_VALUE": "ID로만 데이터를 꺼내 쓰므로 고정비가 없는 DynamoDB가 맞습니다.",
    "SEL_DB_DEFAULTED": "데이터 특성을 판단할 수 없어 가장 범용적인 PostgreSQL을 기본으로 골랐습니다.",
    "SEL_AZ_INHERENT": "{component}은(는) 추가 비용 없이 여러 가용 영역에 걸쳐 동작합니다.",
    "SEL_AZ_SINGLE_ALLOWED": "잠깐 멈춰도 괜찮다고 하셔서 단일 가용 영역으로 비용을 줄입니다.",
    "SEL_AZ_MULTI_REQUIRED": "멈추면 안 된다고 하셔서 {component}을(를) 두 개 이상의 가용 영역에 둡니다.",
    "SEL_STATIC_STABILITY": "가용 영역 하나가 통째로 멈춰도 남은 쪽이 최대 트래픽을 감당하도록 용량을 둡니다.",
    # exclusion
    "EXC_STATIC_NEEDS_SERVER": "서버 기능이 필요해서 정적 사이트만으로는 동작할 수 없습니다.",
    "EXC_SLS_REALTIME": "실시간 연결(WebSocket 등)은 Lambda에서 유지할 수 없습니다.",
    "EXC_SLS_TIMEOUT": "30초 가까이 또는 그보다 오래 걸리는 요청은 Serverless 입구(API Gateway)의 30초 한도에 걸립니다.",
    "EXC_SLS_PAYLOAD": "6MB가 넘는 요청·응답은 Lambda에서 처리할 수 없습니다.",
    "EXC_SLS_STATEFUL": "서버 메모리나 로컬 디스크에 상태를 두는 앱은 Lambda에서 요청 사이에 그 상태가 사라질 수 있습니다.",
    "EXC_SLS_IP_ALLOWLIST": ("특정 IP만 허용하려면 우회 경로가 없어야 하는데, "
                             "Serverless 구성은 기본 주소로 직접 접근하는 길을 막을 수 없습니다."),
    "EXC_SLS_COLD_START": "응답 속도가 중요해서 첫 요청이 1~2초 느려질 수 있는 구성은 제외합니다.",
    "EXC_DB_SINGLE_AZ": "멈추면 안 되는 서비스라 단일 가용 영역 DB는 제외합니다.",
    "EXC_AURORA_PAUSE_LATENCY": "자동 일시정지 DB는 다시 깨어날 때 약 15초가 걸려 응답 속도 요구를 만족하지 못합니다.",
    "EXC_DB_KIND_MISMATCH": "앱의 데이터 종류와 맞지 않는 DB입니다.",
    "EXC_HIGHER_COST": "조건은 만족하지만 월 ${delta_usd} 더 비쌉니다.",
    "EXC_TIEBREAK_COMPLEX": "비용은 같지만 더 복잡한 구성입니다.",
    "FIX_ALWAYS_ON_COMPUTE": "선택된 구성은 컨테이너가 항상 {count}개 이상 실행됩니다.",
    "FIX_IMAGE_REGISTRY": "앱 이미지를 비공개 저장소(ECR)에 보관합니다.",
    # fixed cost justification
    "FIX_ALB_FOR_CONTAINER": "컨테이너 앱으로 요청을 나눠 보내기 위해 로드밸런서(공유)를 씁니다.",
    "FIX_RDB_FOR_RELATIONAL_DATA": "관계형 데이터를 저장하기 위해 PostgreSQL을 씁니다.",
    "FIX_MULTI_AZ_FOR_AVAILABILITY": "멈추지 않는 서비스를 위해 예비 용량을 다른 가용 영역에 둡니다.",
    "FIX_PC_FOR_LATENCY": "첫 요청 지연을 없애기 위해 Lambda를 미리 {count}개 준비해 둡니다.",
    "FIX_ENDPOINT_FOR_VPC_LAMBDA": "DB 접속 정보를 안전하게 읽기 위해 Secrets Manager 전용 통로(공유)를 씁니다.",
    "FIX_WAF_FOR_IP_ALLOWLIST": "허용한 IP만 접속하도록 방화벽(WAF)을 씁니다.",
    "FIX_SECRET_STORAGE": "비밀값을 안전하게 보관하기 위해 Secrets Manager를 씁니다.",
    "FIX_PROD_MONITORING": "운영 환경이라 장애 알람과 dashboard를 둡니다.",
    # policy
    "POL_TIER_DEV": "개발·테스트용 환경 정책을 적용합니다(로그 7일 보관, 자동 만료).",
    "POL_TIER_PROD_APPROVAL": "실제 운영 환경은 관리자 승인 후에 만들어집니다.",
    "POL_TTL_DEV": "{days}일 뒤 자동으로 삭제됩니다(연장 가능).",
    "POL_SPOT_DEV_A0": "개발용이고 잠깐 멈춰도 되므로 저렴한 Spot 용량을 씁니다.",
    "POL_LAMBDA_CONCURRENCY_CAP": "동시 실행 수를 {limit}개로 제한해 비용 폭주를 막습니다.",
    "POL_DB_CONNECTION_GUARD": "DB 연결이 넘치지 않도록 동시 실행 수를 {limit}개로 제한합니다.",
    "POL_THROTTLE": "초당 요청을 {rate}건(순간 {burst}건)으로 제한해 비정상 트래픽 비용을 막습니다.",
    "POL_CTR_AUTOSCALING": "부하에 따라 컨테이너를 {min}~{max}개 사이에서 자동으로 늘리고 줄입니다.",
    "POL_DDB_MAX_THROUGHPUT": "DynamoDB 처리량 상한을 걸어 비용 폭주를 막습니다.",
    # warnings
    "WARN_DEV_MULTI_AZ_COST": "개발용 환경이지만 높은 가용성을 요청해서 비용이 늘어납니다.",
    "WARN_STICKY_STATE": "서버 메모리 상태는 컨테이너가 교체되면 사라질 수 있습니다(같은 사용자는 같은 컨테이너로 연결).",
    "WARN_SPIKE_PREWARM_COST": "갑작스러운 트래픽에도 빠르게 응답하도록 용량을 미리 확보해 비용이 늘어납니다.",
    "WARN_REGION_SCOPE": "서울 리전 전체 장애까지는 대비하지 않습니다(다중 리전은 지원 범위 밖).",
    "WARN_DB_DEFAULTED": "DB 종류를 추정했습니다. 앱이 다른 DB를 쓴다면 알려주세요.",
    "WARN_NEAR_BUDGET": "최대 예상 비용이 예산의 {percent}%입니다.",
    "WARN_NO_INTERNET_EGRESS": "이 구성에서 앱은 외부 인터넷 API를 호출할 수 없습니다.",
    # conflicts
    "CONF_STATIC_WITH_DATA": "화면만 있는 사이트는 데이터를 저장할 수 없습니다. '화면 + API'를 선택해 주세요.",
    "CONF_STATIC_SECRETS": "정적 사이트 파일은 브라우저에 그대로 노출되므로 비밀값을 넣을 수 없습니다.",
    "CONF_DB_HINT_MISMATCH": "코드에서 감지한 DB({detected})와 알려주신 DB({declared})가 다릅니다. 하나를 확인해 주세요.",
    "CONF_NO_CANDIDATE": "조건을 모두 만족하는 구성이 없습니다(플랫폼 규칙 오류로 기록됩니다).",
    # unsupported
    "UNSUP_TRAFFIC_SCALE": "하루 1만 명이 넘는 서버 앱은 아직 지원하지 않습니다.",
    "UNSUP_REQUEST_OVER_120S": "2분 넘게 걸리는 요청은 한 번의 요청·응답으로 처리할 수 없어요(CDN이 기다리는 최대 시간을 넘음).",
    "ALT_ASYNC_JOB": "오래 걸리는 작업은 '요청 접수 → 뒤에서 처리 → 결과 확인' 방식으로 바꾸면 처리할 수 있어요.",
    "ALT_SPLIT_UNDER_2MIN": "작업을 2분 안에 끝나도록 나누면 지금 구성으로 처리할 수 있어요.",
    "POL_LONG_REQUEST_TIMEOUTS": ("오래 걸리는 요청을 위해 CDN 대기 시간을 {origin_timeout_s}초, "
                                  "로드밸런서 대기 시간을 {alb_idle_timeout_s}초로 설정해요."),
    "UNSUP_DB_ENGINE": "PostgreSQL과 DynamoDB 외의 DB는 지원하지 않습니다.",
    "UNSUP_NON_HTTP": "HTTP가 아닌 통신은 지원하지 않습니다.",
    "UNSUP_BLUEPRINT_NOT_YET": "추천 결과({blueprint})는 설계에 포함되어 있지만 아직 플랫폼에서 생성할 수 없습니다.",
    # budget
    "BUD_OVER_USER_LIMIT": "최대 예상 비용 월 ${cost_usd}이(가) 예산 ${budget_usd}을(를) 넘습니다.",
    "BUD_PLATFORM_CAP": "플랫폼 전체 월 상한(${cap_usd})을 넘게 됩니다.",
    "BUD_QUOTA_EXCEEDED": "{tier} 환경은 사용자당 {limit}개까지 만들 수 있습니다.",
    "BUD_RELAX_OPTION": "{changes} 바꾸면 최대 월 ${cost_usd}로 예산 안에 들어옵니다.",
    "BUD_RELAX_PARTIAL": "{changes} 바꾸면 최대 월 ${cost_usd}로 ${saved_usd} 줄지만 아직 예산보다 높아요.",
    "BUD_OVERRIDDEN": "예산(월 ${budget_usd})을 넘는 것을 확인하고 진행했습니다(최대 월 ${cost_usd}).",
    "BUD_RAISE_BUDGET": "예산을 월 ${cost_usd} 이상으로 올리면 요구사항을 모두 유지할 수 있습니다.",
    # info
    "INFO_EKS_OUT_OF_SCOPE": "Kubernetes(EKS)는 이 규모에 필요하지 않아 후보에 넣지 않았습니다.",
    "INFO_SHARED_MARGINAL_COST": "공유 리소스 비용은 환경 {n}개가 나눠 쓴다고 보고 계산했습니다.",
    "SEC_BASELINE_APPLIED": "필수 보안 기준 {count}개 항목이 모두 적용됩니다.",
}


@dataclass(frozen=True)
class Reason:
    code: str
    params: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.code not in MESSAGES:
            raise KeyError(f"unknown reason code: {self.code}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "params": dict(sorted(self.params.items())),
            "message": MESSAGES[self.code].format(**self.params),
        }


def R(code: str, **params) -> Reason:
    return Reason(code, params)
