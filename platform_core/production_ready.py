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


def checks_for(candidate_id: str) -> list[dict]:
    if candidate_id != "NONE+NONE":
        return []
    return [{"id": i, "dimension": d, "layer": layer, "title": t} for i, d, layer, t in STATIC_CHECKS]


# Every check id a validator can emit for Static, in non-expert wording (UI + tests).
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
}
