"""Security Baseline catalog (docs/security-baseline.md) as data.

The engine attaches applicable IDs to every recommendation; validators register
checks against the same IDs, and tests assert that every applicable item for an
implemented blueprint has at least one validator.

Applicability tags: ALL, S (static), L (serverless), C (container), R (rds/aurora),
D (dynamodb), IP (ip allowlist), PLATFORM, ACCOUNT, SHARED_VPC.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaselineItem:
    id: str
    title: str
    applies: frozenset
    layers: frozenset  # PLAN | STATE | HTTP | REVIEW | TEST

    @property
    def plain(self) -> str:
        return PLAIN[self.id]


def _i(id, title, applies, layers):
    return BaselineItem(id, title, frozenset(applies.split()), frozenset(layers.split()))


ITEMS = [
    _i("SB-IAM-01", "환경 전용 실행 role", "L C", "PLAN STATE"),
    _i("SB-IAM-02", "Resource 한정, Action/Resource * 쓰기 금지", "ALL", "PLAN"),
    _i("SB-IAM-03", "runner permission boundary", "PLATFORM", "PLAN STATE"),
    _i("SB-IAM-04", "생성 role에 boundary 강제", "L C", "STATE"),
    _i("SB-IAM-05", "사용자에게 AWS credential 미제공", "PLATFORM", "REVIEW"),
    _i("SB-IAM-06", "control plane 기능별 role", "PLATFORM", "PLAN"),
    _i("SB-ENC-01", "S3 기본 암호화", "S", "STATE"),
    _i("SB-ENC-02", "RDS storage 암호화", "R", "PLAN STATE"),
    _i("SB-ENC-03", "DynamoDB 암호화", "D", "STATE"),
    _i("SB-ENC-04", "Secrets Manager KMS 암호화", "L C", "STATE"),
    _i("SB-ENC-05", "Logs·ECR 저장 암호화", "L C", "STATE"),
    _i("SB-TLS-01", "HTTPS only (redirect-to-https)", "S C", "PLAN HTTP"),
    _i("SB-TLS-02", "API Gateway HTTPS only", "L", "HTTP"),
    _i("SB-TLS-03", "RDS force_ssl", "R", "STATE"),
    _i("SB-TLS-04", "S3 비TLS 요청 Deny", "S", "PLAN STATE"),
    _i("SB-TLS-05", "보안 header(HSTS 등)", "S C", "HTTP"),
    _i("SB-SEC-01", "비밀값은 Secrets Manager", "L C R", "STATE"),
    _i("SB-SEC-02", "참조로만 주입, 평문 env 금지", "L C", "PLAN"),
    _i("SB-SEC-03", "RDS managed master password", "R", "PLAN"),
    _i("SB-SEC-04", "Static artifact secret 금지/탐지", "S", "PLAN"),
    _i("SB-SEC-05", "자기 환경 secret만 읽기", "L C", "PLAN"),
    _i("SB-SEC-06", "state bucket 보호", "PLATFORM", "STATE"),
    _i("SB-PUB-01", "S3 BPA + OAC only", "S", "PLAN STATE HTTP"),
    _i("SB-PUB-02", "ALB internal", "C", "STATE"),
    _i("SB-PUB-03", "task SG는 ALB SG만", "C", "STATE"),
    _i("SB-PUB-04", "ALB SG는 CloudFront VPC origin만", "C", "STATE"),
    _i("SB-PUB-05", "RDS private, SG 한정", "R", "PLAN STATE"),
    _i("SB-PUB-06", "Lambda Function URL 금지", "L", "PLAN"),
    _i("SB-PUB-07", "ECR private + scan on push", "L C", "STATE"),
    _i("SB-PUB-08", "IP allowlist는 WAF, 우회 경로 없음", "IP", "STATE HTTP"),
    _i("SB-PUB-09", "NAT Gateway 금지", "ALL", "PLAN"),
    _i("SB-PUB-10", "0.0.0.0/0 inbound SG 금지", "ALL", "PLAN STATE"),
    _i("SB-LOG-01", "계정 CloudTrail", "ACCOUNT", "STATE"),
    _i("SB-LOG-02", "CloudFront 접근 log", "S C", "STATE"),
    _i("SB-LOG-03", "API Gateway access log", "L", "STATE"),
    _i("SB-LOG-04", "앱 log 보관 기간 설정", "L C", "PLAN"),
    _i("SB-LOG-05", "공유 VPC flow log", "SHARED_VPC", "STATE"),
    _i("SB-LOG-06", "플랫폼 감사 log", "PLATFORM", "TEST"),
    _i("SB-TAG-01", "필수 tag", "ALL", "PLAN STATE"),
]

BY_ID = {i.id: i for i in ITEMS}


def applicable(components: set[str]) -> list[str]:
    """IDs that apply to an environment made of the given component tags.

    ACCOUNT/PLATFORM items are platform-wide and are not attached per environment;
    SHARED_VPC items are attached when the environment depends on the shared VPC.
    """
    tags = set(components) | {"ALL"}
    return sorted(i.id for i in ITEMS if i.applies & tags)


# Non-expert wording shown in the UI (the `title` above stays the precise engineering term).
PLAIN = {
    "SB-IAM-01": "환경마다 전용 실행 권한을 둡니다",
    "SB-IAM-02": "자기 환경의 리소스에만 권한을 줍니다 (전체 권한 금지)",
    "SB-IAM-03": "플랫폼 생성기의 권한에 상한선을 둡니다",
    "SB-IAM-04": "새로 만드는 권한에도 상한선을 강제합니다",
    "SB-IAM-05": "사용자에게 AWS 접속 열쇠를 나눠 주지 않습니다",
    "SB-IAM-06": "플랫폼 기능마다 권한을 나눕니다",
    "SB-ENC-01": "저장된 사이트 파일을 암호화합니다",
    "SB-ENC-02": "데이터베이스 저장소를 암호화합니다",
    "SB-ENC-03": "DynamoDB 데이터를 암호화합니다",
    "SB-ENC-04": "비밀값을 암호화해 보관합니다",
    "SB-ENC-05": "로그와 앱 이미지를 암호화해 보관합니다",
    "SB-TLS-01": "HTTPS로만 접속되게 합니다",
    "SB-TLS-02": "API도 HTTPS로만 제공합니다",
    "SB-TLS-03": "데이터베이스 접속도 암호화합니다",
    "SB-TLS-04": "암호화되지 않은 저장소 접근을 거부합니다",
    "SB-TLS-05": "브라우저 보안 헤더를 붙입니다",
    "SB-SEC-01": "비밀값은 전용 보관소(Secrets Manager)에 둡니다",
    "SB-SEC-02": "비밀값을 설정 파일에 그대로 적지 않습니다",
    "SB-SEC-03": "DB 관리자 비밀번호는 AWS가 관리합니다",
    "SB-SEC-04": "사이트 파일에 비밀값이 섞이지 않았는지 검사합니다",
    "SB-SEC-05": "자기 환경의 비밀값만 읽을 수 있습니다",
    "SB-SEC-06": "인프라 상태 파일을 보호합니다",
    "SB-PUB-01": "저장소는 공개하지 않고 CDN만 읽게 합니다",
    "SB-PUB-02": "로드밸런서를 인터넷에 직접 노출하지 않습니다",
    "SB-PUB-03": "컨테이너는 로드밸런서에서 오는 요청만 받습니다",
    "SB-PUB-04": "로드밸런서는 CDN에서 오는 요청만 받습니다",
    "SB-PUB-05": "데이터베이스는 내부망에만 둡니다",
    "SB-PUB-06": "Lambda로 바로 들어오는 주소를 만들지 않습니다",
    "SB-PUB-07": "앱 이미지 저장소를 비공개로 두고 취약점을 검사합니다",
    "SB-PUB-08": "IP 제한 시 방화벽으로 막고 우회 경로를 없앱니다",
    "SB-PUB-09": "고정비가 큰 NAT Gateway를 만들지 않습니다",
    "SB-PUB-10": "모든 인터넷 주소에 열린 방화벽 규칙을 금지합니다",
    "SB-LOG-01": "계정 활동 기록(CloudTrail)을 남깁니다",
    "SB-LOG-02": "사이트 접속 기록을 남깁니다",
    "SB-LOG-03": "API 접속 기록을 남깁니다",
    "SB-LOG-04": "앱 로그의 보관 기간을 정합니다",
    "SB-LOG-05": "내부망 트래픽 기록을 남깁니다",
    "SB-LOG-06": "누가 무엇을 승인·삭제했는지 기록합니다",
    "SB-TAG-01": "비용 추적용 태그를 모든 리소스에 붙입니다",
}
