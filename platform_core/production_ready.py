"""Production-ready checks per blueprint (docs/production-ready.md) as data.

Shown to the user before approval, and tests assert that the validators actually emit
every listed check id for the blueprint.
"""

STATIC_CHECKS = [
    # id, dimension, layer, title (Korean, non-expert wording)
    ("ART-INDEX", "Reachability", "ARTIFACT", "업로드한 파일에 index.html이 있는지"),
    ("SB-SEC-04", "Security", "ARTIFACT", "파일 안에 비밀번호·키 같은 비밀값이 없는지"),
    ("SB-PUB-01", "Security", "PLAN", "저장소가 인터넷에 공개되지 않도록 막혀 있는지"),
    ("SB-ENC-01", "Security", "PLAN", "저장된 파일이 암호화되는지"),
    ("SB-TLS-01", "Security", "PLAN", "HTTPS로만 접속되도록 설정됐는지"),
    ("SB-TAG-01", "Cost", "PLAN", "비용 추적용 태그가 붙는지"),
    ("SB-PUB-09", "Cost", "PLAN", "고정비가 큰 NAT Gateway를 만들지 않는지"),
    ("SB-TLS-04", "Security", "STATE", "암호화되지 않은 접근을 거부하는지"),
    ("SB-PUB-01/policy", "Security", "STATE", "CDN만 파일을 읽을 수 있는지"),
    ("SB-LOG-02", "Security", "STATE", "접속 기록(log)이 남는지"),
    ("SB-LOG-01", "Security", "STATE", "계정 활동 기록(CloudTrail)이 켜져 있는지"),
    ("SB-LOG-01/versioning", "Security", "STATE", "기록이 지워져도 이전 버전으로 되살릴 수 있는지"),
    ("PR-ALL-09", "Cost", "STATE", "실제 리소스에 태그가 붙었는지"),
    ("PR-ALL-08", "Operability", "STATE", "개발 환경의 자동 만료일이 설정됐는지"),
    ("PR-ALL-05", "Observability", "STATE", "기록 보관 기간이 정해져 있는지"),
    ("PR-ALL-06", "Observability", "STATE", "운영 환경에 장애 알람이 연결됐는지(운영용인 경우)"),
    ("PR-ALL-02", "Reachability", "HTTP", "사이트 주소로 접속하면 정상 응답하는지"),
    ("PR-ALL-03", "Security", "HTTP", "http:// 접속이 https://로 넘어가는지"),
    ("PR-ALL-04", "Security", "HTTP", "보안 헤더가 응답에 포함되는지"),
    ("PR-S-01", "Security", "HTTP", "저장소 주소로 직접 접근하면 막히는지"),
    ("PR-S-02", "Reachability", "HTTP", "SPA 경로가 index.html로 연결되는지(선택한 경우)"),
    ("PR-S-03", "Operability", "HTTP", "방금 배포한 버전의 화면이 실제로 보이는지"),
    ("PR-S-04", "Security", "HTTP", "허용하지 않은 IP는 차단되는지(IP 제한을 선택한 경우)"),
]


SERVERLESS_CHECKS = [
    ("IMG-REF", "Reachability", "ARTIFACT", "이미지 주소가 올바르고 공개 저장소에 있는지"),
    ("SB-SEC-02", "Security", "ARTIFACT", "설정값에 비밀번호·키 같은 비밀값이 없는지"),
    ("PLAN-ALLOWLIST", "Cost", "PLAN", "정해진 종류의 리소스만 만드는지"),
    ("SB-IAM-04", "Security", "PLAN", "앱 실행 권한에 상한선(권한 경계)이 걸리는지"),
    ("SB-PUB-06", "Security", "PLAN", "Lambda로 바로 들어오는 주소가 없는지"),
    ("SB-LOG-04", "Observability", "PLAN", "앱 로그 보관 기간이 정해졌는지"),
    ("SB-PUB-09", "Cost", "PLAN", "고정비가 큰 NAT Gateway를 만들지 않는지"),
    ("SB-TAG-01", "Cost", "PLAN", "비용 추적용 태그가 붙는지"),
    ("PR-L-01", "Reachability", "STATE", "앱 함수가 실행 가능한 상태인지"),
    ("PR-L-02", "Security", "STATE", "플랫폼이 만든 이미지로 실행되는지"),
    ("SB-IAM-01", "Security", "STATE", "환경 전용 실행 권한을 쓰는지"),
    ("SB-IAM-02", "Security", "STATE", "실행 권한이 자기 로그에만 한정되는지"),
    ("SB-LOG-03", "Security", "STATE", "API 접속 기록(log)이 남는지"),
    ("SB-PUB-07", "Security", "STATE", "이미지 저장소가 비공개이고 취약점을 검사하는지"),
    ("SB-ENC-05", "Security", "STATE", "로그와 이미지가 암호화되는지"),
    ("SB-LOG-01", "Security", "STATE", "계정 활동 기록(CloudTrail)이 켜져 있는지"),
    ("PR-ALL-09", "Cost", "STATE", "실제 리소스에 태그가 붙었는지"),
    ("PR-ALL-08", "Operability", "STATE", "개발 환경의 자동 만료일이 설정됐는지"),
    ("PR-ALL-06", "Observability", "STATE", "운영 환경에 장애 알람이 연결됐는지(운영용인 경우)"),
    ("PR-ALL-02", "Reachability", "HTTP", "주소로 접속하면 정상 응답하는지"),
    ("PR-L-05", "Reachability", "HTTP", "앱이 직접 응답하는지(게이트웨이 오류가 아닌지)"),
    ("PR-ALL-03", "Security", "HTTP", "HTTPS로만 접속되는지"),
    ("PR-ALL-04", "Security", "HTTP", "보안 헤더가 응답에 포함되는지"),
]

CONTAINER_CHECKS = [
    ("IMG-REF", "Reachability", "ARTIFACT", "이미지 주소가 올바르고 공개 저장소에 있는지"),
    ("SB-SEC-02", "Security", "ARTIFACT", "설정값에 비밀번호·키 같은 비밀값이 없는지"),
    ("PLAN-ALLOWLIST", "Cost", "PLAN", "정해진 종류의 리소스만 만드는지"),
    ("PLAN-FIXED-COST", "Cost", "PLAN", "고정비가 드는 리소스는 추천 이유가 있는 것만 만드는지"),
    ("SB-PUB-09", "Cost", "PLAN", "고정비가 큰 NAT Gateway를 만들지 않는지"),
    ("SB-IAM-04", "Security", "PLAN", "앱 실행 권한에 상한선(권한 경계)이 걸리는지"),
    ("SB-TLS-01", "Security", "PLAN", "HTTPS로만 접속되도록 설정됐는지"),
    ("SB-TAG-01", "Cost", "PLAN", "비용 추적용 태그가 붙는지"),
    ("SB-PUB-02", "Security", "STATE", "부하분산기(ALB)가 인터넷에 직접 노출되지 않는지"),
    ("SB-PUB-03", "Security", "STATE", "컨테이너는 부하분산기에서 오는 요청만 받는지"),
    ("SB-PUB-04", "Security", "STATE", "부하분산기는 CloudFront에서 오는 요청만 받는지"),
    ("PR-C-04", "Resilience", "STATE", "컨테이너가 필요한 개수만큼 실행 중인지"),
    ("PR-C-03", "Reachability", "STATE", "상태 확인 경로가 모두 정상인지"),
    ("PR-C-05", "Operability", "STATE", "배포 실패 시 자동으로 되돌리는지"),
    ("SB-IAM-01", "Security", "STATE", "환경 전용 실행 권한을 쓰는지"),
    ("SB-PUB-07", "Security", "STATE", "이미지 저장소가 비공개이고 취약점을 검사하는지"),
    ("SB-LOG-02", "Security", "STATE", "접속 기록(log)이 남는지"),
    ("SB-LOG-04", "Observability", "STATE", "앱 로그 보관 기간이 정해졌는지"),
    ("SB-LOG-01", "Security", "STATE", "계정 활동 기록(CloudTrail)이 켜져 있는지"),
    ("PR-ALL-09", "Cost", "STATE", "실제 리소스에 태그가 붙었는지"),
    ("PR-ALL-08", "Operability", "STATE", "개발 환경의 자동 만료일이 설정됐는지"),
    ("PR-ALL-06", "Observability", "STATE", "운영 환경에 장애 알람이 연결됐는지(운영용인 경우)"),
    ("PR-ALL-02", "Reachability", "HTTP", "주소로 접속하면 앱이 정상 응답하는지"),
    ("PR-ALL-03", "Security", "HTTP", "http:// 접속이 https://로 넘어가는지"),
    ("PR-ALL-04", "Security", "HTTP", "보안 헤더가 응답에 포함되는지"),
    ("PR-C-01", "Security", "HTTP", "부하분산기 주소로 인터넷에서 직접 접속할 수 없는지"),
    ("PR-C-02", "Security", "HTTP", "컨테이너 공인 IP로 직접 접속하면 막히는지"),
    ("PR-C-07", "Security", "HTTP", "허용하지 않은 IP는 차단되는지(IP 제한을 선택한 경우)"),
]

_LISTS = {"NONE+NONE": STATIC_CHECKS, "SLS+NONE": SERVERLESS_CHECKS, "CTR+NONE": CONTAINER_CHECKS}


def checks_for(candidate_id: str) -> list[dict]:
    return [{"id": i, "dimension": d, "layer": layer, "title": t} for i, d, layer, t in _LISTS.get(candidate_id, [])]


# Every check id a validator can emit, in non-expert wording (UI + tests).
CHECK_TITLES = {i: t for i, _, _, t in STATIC_CHECKS} | {
    "ART-PATH": "파일 경로가 안전한지",
    "ART-SIZE": "파일 크기와 개수가 제한 안인지",
    "ART-ZIP": "올바른 zip 파일인지",
    "PR-ALL-02/enabled": "CDN이 켜져 있는지",
    "SB-PUB-01/acl": "저장소의 공개 권한(ACL) 기능이 꺼져 있는지",
    "SB-PUB-01/oac": "CDN이 전용 인증으로 저장소를 읽는지",
    "SB-PUB-08": "IP 제한 방화벽이 연결됐는지",
    "SB-LOG-04": "앱 로그 보관 기간이 정해졌는지",
    "SB-PUB-10": "모든 주소에 열린 방화벽 규칙이 없는지",
    "SB-IAM-02": "전체 권한을 주는 설정이 없는지",
    "SB-PUB-06": "Lambda로 바로 들어오는 주소가 없는지",
    "SB-TLS-05": "보안 헤더가 설정됐는지",
} | {i: t for i, _, _, t in SERVERLESS_CHECKS if i not in ("PR-ALL-02", "SB-SEC-02")} | {
    "SB-SEC-02": "비밀값을 설정에 그대로 적지 않았는지",
    "IMG-ENV": "환경변수 이름과 크기가 올바른지",
    "IMG-HEALTH": "상태 확인 경로가 올바른지",
    "SB-PUB-06/permission": "이 API만 앱 함수를 부를 수 있는지",
    "SB-PUB-07/immutable": "이미지 태그를 덮어쓸 수 없는지",
    "PR-L-03": "실행 시간·메모리 설정이 올바른지",
    "PR-L-04": "요청 폭주를 막는 제한이 걸려 있는지",
    "SB-SEC-01": "비밀값을 전용 보관소에 두는지",
    "SB-ENC-04": "비밀값이 암호화되는지",
    "SB-SEC-05": "자기 환경의 비밀값만 읽는지",
    "IMG-BUILD": "Lambda용 실행 이미지를 만들었는지",
}

# Container ids keep the wording above when shared with other blueprints (PR-ALL-02, SB-SEC-02, ...).
for _id, _dim, _layer, _title in CONTAINER_CHECKS:
    CHECK_TITLES.setdefault(_id, _title)
CHECK_TITLES["PR-C-06"] = "실행 개수가 추천한 최소 개수와 같은지(비용 상한)"
