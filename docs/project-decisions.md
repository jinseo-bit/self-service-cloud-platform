# Project Decisions — Self-Service Cloud Platform

- 트랙: WorldSkills Cloud Computing 프로젝트 트랙 **D. Platform Engineering**
- 출발점: CFP D 예시 "10분 안에 production-ready 환경"
- 최종 갱신: 2026-10-04 (구현 착수 후 결정 §15 추가)
- 상태 표기: **확정** = Q&A로 확정되어 기준으로 사용 / **제안** = 설계 과정에서 나온 안, 승인 대기

이 문서는 Q&A로 확정된 결정의 단일 기준(source of truth)이다. 확정 항목은 기술적 불가능이나 충돌이 발견되지 않는 한 변경하지 않으며, 변경 시 ADR로 남긴다.

---

## 0. 프로젝트 정의

### 핵심 질문
> 사용자가 AWS Infrastructure를 직접 설계하지 않아도 Application 요구사항만 입력하면, 플랫폼이 비용·보안·가용성 등을 고려해 적절한 AWS Architecture를 선택하고 실제로 사용할 수 있는 production-ready AWS 환경을 제공할 수 있는가?

### 설계 원칙 (확정)
1. EKS를 쓰는 것이 목적이 아니다.
2. 단순한 Workload에는 단순한 Architecture를 사용한다.
3. 비용은 최소화하되 요구사항을 희생하지 않는다.
4. 비용 절감을 이유로 필수 Security Baseline을 제거하지 않는다.
5. 사용자는 AWS 서비스를 고르지 않고 자신의 요구사항을 입력한다.
6. 플랫폼이 Architecture를 선택했다면 그 이유를 설명할 수 있어야 한다.
7. 불필요하게 복잡한 Architecture와 기술 도입을 피한다.

### Architecture 선택 우선순위 (확정)
1. 기능 요구사항 만족
2. 필수 Security Baseline 만족
3. 요구된 Availability·Performance 만족
4. 1~3을 만족하는 후보 중 예상 비용 최저 우선
5. 불필요한 AWS 서비스·고정비 금지
6. NAT Gateway·ALB·RDS 등 고정비 서비스는 필요성이 있을 때만
7. EKS는 현재 Blueprint 범위 밖
8. 추천 결과에 선택 이유와 주요 탈락 후보의 제외 이유를 남긴다

---

## 1. 사용자와 범위

| ID | 결정 | 상태 |
|---|---|---|
| D-01 | 설계 기준 페르소나: **인프라 비전문가 애플리케이션 개발자**. 평가는 선수들이 페르소나를 연기하는 대리 평가 | 확정 |
| D-02 | 환경 범위: **인프라 + 사용자 코드 배포**. Repository 생성과 범용 CI/CD는 초기 범위 제외 | 확정 |
| D-03 | 입력 artifact: **Container image / Static 빌드 결과물 / Git repo**. Function zip은 미지원 | 확정 |
| D-04 | Git repo 빌드: **자동 감지(package.json, Dockerfile 등) + platform.yaml 선택적 override**(build command, port, health check 등) | 확정 |
| D-05 | Git repo는 **public 우선**, private repo 접근은 후순위 | 확정 |
| D-06 | 구현 Blueprint 수: **2~3개를 깊게** | 확정 |

## 2. 사용자 입력

| ID | 결정 | 상태 |
|---|---|---|
| D-10 | 인터페이스: **Web UI 단계별 form** | 확정 |
| D-11 | 입력 형식: **사용 사례 기반 프리셋 + 미세 조정** | 확정 |
| D-12 | 입력 8차원: 앱 형태 / 트래픽 규모 / 트래픽 패턴 / 데이터 저장 / 멈춤 허용도 / 응답 속도 민감도 / 공개 범위 / 월 예산 상한 | 확정 |
| D-13 | 환경 등급(dev/prod)은 **8개 입력과 별도 필드**로 받음. Multi-AZ 여부는 등급이 아닌 **멈춤 허용도 입력대로** 결정 | 확정 |
| D-14 | Serverless 부적합 workload는 **'앱 형태' 하위 체크(특수 기능) + Git 의존성 감지**로 판별. 최종 판단 기준은 사용자 확인값 | 확정 |
| D-15 | 앱 설정값: Git repo는 **platform.yaml에 선언**(비밀값은 이름만, 값은 UI 입력). Container image/Static은 **UI key-value 입력 + platform.yaml 업로드** 모두 지원. 내부 모델은 하나로 통일 | 확정 |
| D-16 | **Static artifact에는 secret을 받지 않음**(빌드 결과물은 브라우저에 그대로 노출되므로 공개 설정만 허용) | 확정 |

## 3. Architecture 추천

| ID | 결정 | 상태 |
|---|---|---|
| D-20 | 추천 엔진: **deterministic rule-based**. 같은 입력이면 같은 결과, 결과마다 reason code | 확정 |
| D-21 | **AI Agent 미사용**. 근거: 결정성, 감사 가능성, 설명 가능성 | 확정 |
| D-22 | DB 결정: **코드 감지 + 데이터 특성 질문 보완** | 확정 |
| D-23 | 응답 속도 민감 + 저트래픽 API: Lambda+Provisioned Concurrency와 Fargate 최소 task를 **비용 비교로 선택** | 확정 |
| D-24 | 비용 기준값: 후보 간 순위는 **트래픽 구간 대표값**, 예산 검사는 **구간 상한값** | 확정 |
| D-25 | ~~예산 초과 시 승인 차단~~ → **D-158로 변경**: 사용자 예산은 soft guard(완화안 또는 명시적 초과 승인), 보안 항목은 완화 대상에서 제외 | 변경됨 |
| D-26 | 지원 범위 밖 요구사항: **UNSUPPORTED + 근접안 제시**. 근접안 적용은 사용자의 명시적 확인이 필요 | 확정 |
| D-27 | 상세 규칙: [recommendation-engine.md](recommendation-engine.md). 설계 제안 P-01~P-10 승인(P-04는 균등 분할 상수로 변경) | 확정(구현 착수는 최종 승인 대기) |

## 4. Blueprint

| ID | 결정 | 상태 |
|---|---|---|
| D-30 | 초기 Blueprint: **Static / Serverless / Container**. EKS는 범위 외 | 확정 |
| D-31 | Static: **S3 + CloudFront** | 확정 |
| D-32 | Serverless: **Lambda(container image) + Lambda Web Adapter + API Gateway** | 확정 |
| D-33 | Container: **ECS Fargate**, edge는 **CloudFront + VPC Origin → internal ALB** | 확정 |
| D-34 | 앱 규약: **일반 HTTP 서버(PORT, health check)**. 같은 image가 Lambda(LWA)와 Fargate에서 모두 실행됨 | 확정 |
| D-35 | '화면 + API': **한 환경, 두 구성요소**. CloudFront 하나에서 `/*` → S3, `/api/*` → backend로 라우팅(같은 도메인이라 CORS 불필요) | 확정 |
| D-36 | (정정) container image Lambda는 layer를 지원하지 않으므로, LWA는 **플랫폼이 adapter를 `/opt/extensions`에 복사한 파생 image를 빌드**하는 방식으로 적용. Serverless 경로에는 wrap 빌드 단계가 추가됨 | 확정(기술 정정) |

## 5. Network / Domain / Region

| ID | 결정 | 상태 |
|---|---|---|
| D-40 | 계정: **개인 AWS 계정 1개** | 확정 |
| D-41 | Region: **ap-northeast-2 고정**. Multi-Region은 추후 확장 | 확정 |
| D-42 | 도메인: **AWS 기본 도메인**(*.cloudfront.net, execute-api). 사용자 도메인은 추후 확장 | 확정 |
| D-43 | Shared infra: **Blueprint별로 다르게**. VPC가 필요 없는 Blueprint는 VPC를 쓰지 않음 | 확정 |
| D-44 | Fargate task: **public subnet + public IP**(NAT 미사용). Security Baseline의 Public 차단은 **"지정된 edge 외에는 인터넷에서 들어오는 inbound 경로 0"**으로 정의하고 SG를 자동 검증. DB는 항상 private | 확정 |
| D-46 | Container용 internal ALB는 **환경 간 공유**(P-01) | 확정 |
| D-45 | Serverless + RDS: **VPC Lambda + 필수 interface endpoint**(Secrets Manager). endpoint는 공유 VPC에 한 번만 둠. 외부 인터넷 호출 불가 제약은 사용자에게 고지 | 확정 |

## 6. Database

| ID | 결정 | 상태 |
|---|---|---|
| D-50 | 지원: **DB 없음 / DynamoDB / PostgreSQL** | 확정 |
| D-51 | 기본은 DB 없음. 관계형이 꼭 필요할 때만 **가장 작은 RDS PostgreSQL**. dev는 Single-AZ·최소 사양 | 확정 |
| D-52 | Aurora Serverless v2는 **사용량과 최소 ACU 비용을 비교해 더 저렴할 때만** 선택 | 확정 |

## 7. 등급 / Lifecycle / 실패 처리

| ID | 결정 | 상태 |
|---|---|---|
| D-60 | 등급: **dev / prod 2등급**, 정책 묶음(TTL, 데이터 보호, monitoring, 승인). prod는 Admin 승인 | 확정 |
| D-61 | 삭제 시 데이터: dev는 즉시 삭제, prod는 **deletion protection + 최종 snapshot** | 확정 |
| D-62 | Monitoring: **등급별**. dev는 log 중심, prod는 알람과 dashboard 포함 | 확정 |
| D-63 | Lifecycle: **Create / Delete / 앱 재배포 + TTL 자동 만료** | 확정 |
| D-64 | Provisioning 실패: **FAILED 상태 보존 + 사용자 retry/delete**. 방치하면 TTL로 정리 | 확정 |
| D-65 | 배포 후 검증 실패: **종류별 처리**. 보안 검사 실패 → endpoint 비활성화 + FAILED, 앱 health 실패 → DEGRADED + 원인 안내 | 확정 |

## 8. Cost

| ID | 결정 | 상태 |
|---|---|---|
| D-70 | Guardrail: **사용자 예산 soft guard(D-158) / 플랫폼 상한·quota hard guard / 허용 리소스 상한 / 환경 TTL / 실비용 추적·알림** | 확정(D-158 반영) |
| D-71 | Quota: **사용자별 + 플랫폼 전체**(수치는 설정값) | 확정 |
| D-72 | 예상 비용 계산: **단가표(Price List API 주기 수집) + 사용량 모델**, 고정비와 변동비를 분리해 표시 | 확정 |
| D-73 | 예상과 실제 비교: **고정비/변동비 분리 검증**. Cost Allocation Tag 활성화, 대표 환경 7일 이상 운영, 48시간 지연 수집. 고정비 ±10%, 변동비(부하 스크립트 요청 수 기준) ±25%. 실시간 guardrail은 청구 데이터가 아닌 예측치와 리소스 상한으로 처리 | 확정 |

## 9. Security

| ID | 결정 | 상태 |
|---|---|---|
| D-80 | 필수 Baseline(비용과 무관하게 항상 적용): **IAM 최소권한 + permission boundary / 저장 암호화 + HTTPS 강제 / Secret 관리 / Public 차단 + Logging** | 확정 |
| D-81 | 상세 항목: [security-baseline.md](security-baseline.md). 기본 도메인에 따른 TLS 예외 2건 승인(P-10) | 확정(세부 항목 검토 대기) |

## 10. 검증 / production-ready

| ID | 결정 | 상태 |
|---|---|---|
| D-90 | production-ready는 **자동 검증 가능한 기준**으로 정의 | 확정 |
| D-91 | 검증 3계층: **Apply 전 정적 분석(plan) / 배포 후 AWS 상태 검사 / 외부 HTTP 검사** | 확정 |
| D-92 | 생성 시간 SLO: **Blueprint × DB 조합별 SLO + 병렬 생성**. 측정 구간은 승인 클릭부터 검증 3계층 통과까지. 초기값은 2주 실측 후 p95로 재설정 | 확정 |
| D-93 | 상세 기준: [production-ready.md](production-ready.md) | 제안(승인 대기) |

초기 SLO 값(실측 전 가설):

| 조합 | 초기 SLO |
|---|---|
| Static | ≤ 5분 |
| Serverless (DB 없음 / DynamoDB) | ≤ 7분 (LWA wrap 빌드 포함) |
| Serverless + RDS | ≤ 20분 |
| Container (DB 없음 / DynamoDB) | ≤ 12분 |
| Container + RDS | ≤ 20분 |
| Git repo 입력 | 위 값 + 빌드 시간(별도 측정, SLO 외 항목으로 기록) |

## 11. 플랫폼 구조 / IaC

| ID | 결정 | 상태 |
|---|---|---|
| D-100 | Control plane: **Serverless**(S3/CloudFront UI + API Gateway + Lambda + DynamoDB + Step Functions 계열) | 확정 |
| D-101 | 인증: **로그인 + User/Admin 역할**(Admin = 코치 역할: 예산 초과·prod 승인, 전체 조회) | 확정 |
| D-102 | Terraform 실행: **자체 runner(CodeBuild/ECS)**. state는 S3에 환경별 key로 저장 | 확정 |
| D-103 | Terraform 구조: **부품 module + Blueprint 조합**(edge, compute-ecs, compute-lambda, data-rds, data-dynamodb, observability / blueprints / shared / platform) | 확정 |

## 12. 성공 지표

| ID | 지표 | 종류 | 상태 |
|---|---|---|---|
| D-110 | Blueprint × DB 조합별 생성 시간 SLO 달성률 | 정량 | 확정 |
| D-111 | Baseline 자동 검증 통과율(생성 환경 100%, 위반을 주입하면 차단되는지까지 확인) | 정량 | 확정 |
| D-112 | 추천 적합도: optimal 일치율, acceptable 포함률, 과잉 설계 0건, 충돌·지원 불가 정확 보고 | 정량 | 확정 |
| D-113 | 페르소나 사용성 테스트(도움 없이 완료, 추천 이유 이해) | 정성 | 확정 |
| D-114 | 추천 설명 품질(reviewer rubric) | 정성 | 확정 |
| D-115 | 추천 적합도 정답 세트: **시나리오 약 24개(대표 12 / 경계 6 / 충돌 3 / 지원 불가 3)**. 코치와 reviewer 2명이 엔진 결과를 보지 않고 라벨링(블라인드). 정답 = optimal 1개 + acceptable 집합. acceptable보다 비싼 구성은 과잉 설계로 분류 | 확정 |

## 13. 진행 방식

| ID | 결정 | 상태 |
|---|---|---|
| D-120 | 구현 순서: **수직 슬라이스 먼저**. Static으로 입력 → 추천 → 승인 → apply → 검증 → 삭제를 끝까지 완성한 뒤 Blueprint를 추가 | 확정 |
| D-121 | 결정 방식: AI가 임의로 정하지 않고 질문 → 대안 비교 → 사용자 확인 → 확정. 확정 결정은 이 문서에 기록 | 확정 |

---

## 14. 미결 / 승인 대기

| ID | 항목 | 위치 |
|---|---|---|
| O-01 | Recommendation Engine 설계 최종 승인(구현 착수 조건) | recommendation-engine.md |
| O-02 | Security Baseline 세부 항목 검토 | security-baseline.md |
| O-03 | production-ready 판정 항목 | production-ready.md |
| O-04 | 프리셋: Static용 2개 확정(D-132). 나머지는 Blueprint 추가 시 | 부분 확정 |
| O-05 | quota·예산 확정(D-133), TTL dev 7일(문서 기존값) | 확정 |
| O-06 | 재배포: UI에서 새 artifact 업로드 → 수동 배포(D-150). webhook은 범위 외 | 확정 |
| O-07 | 10주 milestone | 불필요(사용자 결정 2026-10-04) |
| O-08 | Price List API 단가 자동 갱신(D-72). 현재는 수동 가격표 + source/region/updated_at 표기(D-153) | 첫 AWS E2E 성공 후 재판단 |

---

## 15. 구현 착수 후 결정 (2026-10-04)

| ID | 결정 | 근거 / 비고 | 상태 |
|---|---|---|---|
| D-130 | 기술 스택: Engine·API·validator·runner는 **Python**(Lambda python3.12), Web UI는 **build 없는 Vanilla JS** | 개발 PC에 Node·Docker 없음. UI도 Static Blueprint와 같은 module로 배포(dogfooding) | 확정 |
| D-131 | AWS 자격 증명: **root 사용 금지**, 사용자가 전용 IAM 사용자를 발급. 그 전까지는 AWS 없이 구현·테스트만 진행 | root key가 CLI에 설정돼 있었음. SB-IAM·CFP 6장 권한 분리 원칙 | 확정 |
| D-132 | ~~프리셋: **포트폴리오·블로그 / 회사·서비스 소개 페이지** 2개(Static)~~ → **D-178로 변경**: 맞춤형 경로에서 프리셋 화면 제거 | Static 우선 구현 | 변경됨 |
| D-133 | Quota: 사용자당 **DEV 2개 · PROD 1개**, 플랫폼 전체 예상비 합계 **월 $30**, 예산 미입력 시 환경 기본 상한 **월 $10**, AWS Budgets 월 $30(80% 실제·100% 예측 알림) | 사용자 결정("더 낮게") | 확정 |
| D-134 | DB 입력 모델: `engine_hint` 단일 필드 → **`detected`(코드 감지) + `declared`(사용자 선언)** 두 필드 | 단일 enum으로는 `CONF_DB_HINT_MISMATCH`를 표현할 수 없음. 규칙(§5 사다리)은 동일 | 확정(기술 정정) |
| D-135 | API Gateway throttle 하한 **10 rps**(burst 20) | 페이지 하나가 여러 API 호출을 만들기 때문에 2×peak(T1≈0.1rps)만으로는 정상 사용도 차단됨 | 확정 |
| D-136 | 예산 완화안: 선호 입력의 **더 낮은 모든 값**을 각각 시도(한 단계만이 아님) | X02처럼 한 단계로는 예산에 못 들어오는 경우에도 완화안을 보여주기 위함. 여전히 입력 하나만 바꿈 | 확정 |
| D-137 | Orchestration: **Step Functions(Standard) + CodeBuild `startBuild.sync`**. Terraform state는 환경별 S3 key + **S3 native lockfile**(DynamoDB lock table 없음) | 고정비 0, 상태 전이가 실행 이력으로 남음 | 확정 |
| D-138 | PLAN validator는 runner 안에서 **apply 전에 실행**하고 FAIL이면 apply 하지 않음(`FAILED`, stage=PLAN, 재시도 불가). 값이 apply 후에 정해지는 항목은 `DEFERRED`로 두고 STATE에서 같은 baseline ID로 검사 | D-91, D-65 | 확정 |
| D-139 | 판정 규칙: Reachability 외 차원의 FAIL → `FAILED`, Reachability만 FAIL → `DEGRADED`. Security FAIL이면 **CloudFront distribution을 즉시 disable** | production-ready §2 | 확정 |
| D-140 | Static PROD monitoring: CloudFront 5xx/4xx 오류율 알람 2개 + dashboard + SNS 이메일(소유자). CloudFront 지표가 us-east-1에만 있어 알람도 us-east-1에 둠 | PR-ALL-06. Engine 비용 모델(알람 4개 가정)은 실제보다 약간 보수적 | 확정 |
| D-141 | 공유 log bucket은 CloudFront 표준 로그(legacy) 때문에 **ACL 사용(BucketOwnerPreferred)**. BPA 4개 항목은 유지 | CloudFront legacy logging은 log-delivery ACL grant가 필요. 환경 site bucket은 계속 ACL 비활성(BucketOwnerEnforced) | 확정 |
| D-142 | 단가 snapshot `seed-2026-10-04`: 공개 가격표를 손으로 입력한 값. Price List API 갱신(O-08) 전까지 비용은 근사치 | 정직한 표기를 위해 snapshot id에 seed 명시 | 확정 |
| D-143 | 시나리오 기대값 수정: R02 예산 $20→$30(WAF+전송량으로 상한 $21), R10 → `CTR+AUR_PAUSE`(업무시간 활성 35%에서 Aurora가 $1 저렴, D-52), R11 → `SLS_PC+RDS_MAZ`(Serverless 제외 조건 없음 + PC가 더 저렴) | 엔진 규칙이 문서의 직관적 예상보다 원칙 4를 더 일관되게 적용한 결과 | 확정 |
| D-144 | 환경 bucket 이름 `sscp-<env-id>-<account-id>`, 모든 리소스 tag: project, managed-by, env-id, owner, tier, blueprint(+DEV는 expires-at) | SB-TAG-01, IAM 리소스 패턴 `sscp-env-*` | 확정 |
| D-145 | **로컬 개발 서버**(`scripts/local_dev.py`): 실제 UI와 실제 API handler를 실행하고, AWS 경계(Cognito, S3 presigned 업로드, CodeBuild, STATE/HTTP 검사)만 로컬 대체물로 바꿈. 대체 부분은 UI 배너와 "로컬 미실행" 표시로 항상 구분. 배포 번들에는 포함하지 않음 | 사용자 요구: AWS 배포 전에 로컬 완성도 확보. mock이 최종 기능을 대체하지 않도록 경계를 명시 | 확정 |
| D-146 | UI 검증: Playwright + Chromium으로 실제 브라우저 흐름 테스트, axe-core로 serious/critical 접근성 위반 0건 유지, 색 대비 WCAG AA로 token 조정 | Node 없이 Python만으로 브라우저 검증 | 확정 |
| D-147 | UI 문구: 보안 기준과 검사 항목에 **쉬운 말 제목**(`baseline.PLAIN`, `production_ready.CHECK_TITLES`)을 두고, 검사 ID는 보조 표기로만 사용. 화면에 나올 수 있는 모든 ID에 제목이 있는지 테스트로 보장 | 비전문 개발자 페르소나(D-01) | 확정 |
| D-148 | 배포자 권한: `AdministratorAccess` 대신 범위를 좁힌 [infra/iam/deployer-policy.json](../infra/iam/deployer-policy.json) 사용 권장 | 최소 권한(SB-IAM) | 확정 |
| D-149 | Checkov: 개발 PC의 Windows 애플리케이션 제어 정책이 의존 DLL(orjson)을 차단해 로컬 실행 불가. 같은 역할은 자체 PLAN validator가 SB ID 단위로 수행 | 환경 제약 | 기록 |
| D-150 | **Static 재배포**: 같은 환경에 새 revision을 배포. revision마다 `uploads/<env>/rev-<n>.zip`(덮어쓰기 없음) → ARTIFACT 검사 → **publisher Lambda**(전용 role)가 S3 sync(새 파일 업로드 + 빠진 파일 삭제) + CloudFront invalidation → STATE/HTTP 재검증 → revision 기록. **terraform을 다시 실행하지 않음**. 이전 revision으로 되돌리기 지원. 인프라 설정(SPA, IP 제한)은 재배포 대상이 아님. 운영 중 환경의 재배포 실패는 TTL 정리 대상이 아니며 retry는 재배포 경로를 다시 탐 | 기존 구조와의 충돌(고정 업로드 key, 배포 로직이 runner에만 있음)을 revision key와 공용 `platform_core.publish`로 해소 | 확정 |
| D-151 | 내부 reason code·검사 ID는 **관리자(검토자)에게만** 표시. 일반 사용자는 문장만 보고, 관리자는 "검토자용 상세"(후보 비교, reason code·params, rules/pricing 버전, input_hash)를 봄 | 원칙 6(설명 가능성)은 유지하면서 비전문가 화면 단순화 | 확정 |
| D-152 | 다크 모드: 검증 전까지 비노출(`color-scheme: light`, 다크 색 정의 제거) | RC 범위 | 확정 |
| D-153 | 가격표 metadata: `source=manual`, `region`, `updated_at`, `approximate=true`를 snapshot과 엔진 출력(`pricing`)에 포함. UI의 모든 비용 표시에 "추정치"와 기준일 안내 | O-08 보류 동안의 정직한 표기 | 확정 |
| D-154 | PR-S-03 구현: 업로드 시 index.html의 SHA-256을 기록하고, 배포 후 실제 응답과 비교(캐시 무효화 대기 재시도). IP 제한 환경은 외부에서 확인할 수 없어 제외 | production-ready 문서의 기존 항목 | 확정 |
| D-155 | 재검증 시 이전 검사 결과는 PLAN만 이어받고 ARTIFACT는 현재 revision 것만 사용 | 재배포마다 검사 결과가 중복 누적되던 버그 수정 | 확정 |
| D-156 | 로컬 서버 진단성: `127.0.0.1` 기준 URL·static root·health(`/__sscp/health`)·`X-SSCP-App` header 출력, port 독점 bind(사용 중이면 종료), Service Worker script 요청 경고, 시뮬레이터는 실행 ARN당 1회·환경당 동시 1개 | 사용자 PC에서 `localhost:8000`에 예전 프로젝트의 Service Worker가 남아 다른 사이트가 보인 문제(서버 응답은 정상). 예전 파일·브라우저 데이터는 건드리지 않음 | 확정 |
| D-157 | **요청 시간 규칙 정정** (AWS 공식 문서 2026-10-04 확인). CloudFront origin response timeout은 quota 증설 없이 **1–120초**(기존 문서의 "최대 60초"는 틀림), response completion timeout은 설정하지 않으면 상한 없음, ALB idle timeout **1–4000초**, API Gateway HTTP API integration timeout **30초 hard limit**, REST API 29초(Regional/Private는 증설 가능하지만 throttle 감소 가능), Lambda 실행은 최대 900초지만 동기 호출은 앞단 timeout에 묶임. 입력은 "가장 오래 걸리는 요청" **단일 선택 5단계**(몇 초 / 30초 정도 / 30초~1분 / 1~2분 / 2분 넘게). Serverless는 "몇 초"만(30초 hard limit에 여유), Container는 **2분까지 동기 처리**(CloudFront origin timeout 30/60/90/120초, ALB idle = origin + 10초). **2분 초과만** UNSUPPORTED이며 "요청 접수 → 뒤에서 처리 → 결과 확인"(비동기) 대안과 "2분 이하로 나눴을 때의 구성"을 근접안으로 제시 | 출처: [CloudFront origin settings](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html), [CloudFront quotas](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html), [ALB attributes](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html), [HTTP API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html), [REST API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-execution-service-limits-table.html), [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) | 확정 |
| D-158 | **예산 정책 분리**: 사용자 입력 월 예산 = **soft guard**. 초과 시 상태 `OVER_BUDGET`, 사용자는 (A) 완화안 적용 또는 (B) 확인 화면을 거친 **명시적 초과 승인**(`budget_override` 기록: 예산·예상 비용·초과액·승인자·시각) 가능. **hard guard는 override 불가**: 플랫폼 전체 월 상한, 사용자별 quota, 지원 불가·미구현 Architecture, Security Baseline(PLAN/STATE/HTTP/ARTIFACT 검사). override는 admission 플래그일 뿐 엔진·검사기 입력이 아님 | 사용자 결정(2026-10-04), D-25 대체 | 확정 |
| D-159 | **예산 완화안 규칙**: 완화 대상은 선호 입력(멈춤 허용도, 응답 속도)과 예산뿐. 기능·데이터·트래픽·공개 범위·보안은 대상 아님. 예산 안에 들어오는 안이 있으면 **변경 개수가 가장 적은 안**(단일 → 2개 조합)만, 없으면 **비용을 낮추는 안**(최대 3개, "아직 예산 초과" 표시) + 예산 상향안. 화면에서 현재/제안을 비교하고 **‘이 변경 적용’을 눌러야만** 요구사항을 바꾸고 엔진을 다시 실행 | D-136 확장 | 확정 |
| D-160 | 지원 불가·확인 필요 결과에 **원인 답변(`causes`: code·field·value)** 포함. UI는 원인 답변과 이유를 보여주고 **"이 답변 바꾸기"로 해당 질문에 바로 이동** | UX 결함 수정 | 확정 |
| D-161 | 질문마다 **"예시 보기"**(기본 닫힘, 앱 사례 1~3개). 이전 답변으로 근거가 분명할 때만 deterministic한 약한 추천(정적 사이트 → DB 없음·첫 요청 지연 괜찮음, 거의 안 씀 → 잠깐 멈춤 허용). **자동 선택하지 않음**. 최종 Architecture 추천과 구분 | UX | 확정 |
| D-162 | 로고(홈) 클릭 시 항상 홈. 작성 중인 질문이 있으면 "작성 중인 내용이 사라질 수 있어요." 확인 후 이동 | UX | 확정 |
| D-163 | **환경 생성 방식 2가지**: ‘맞춤형으로 만들기’(프리셋 → 질문 → 추천, 기존 흐름)와 ‘빠르게 시작하기’(목적 Template 선택 → 최소 입력: 이름·용도·예산(선택)·SPA 여부 → 검토 → zip 업로드 → ‘이 구성으로 만들기’). **Template은 metadata**(`platform_core/templates.py`: `template_id`, `title`, `short_description`, `category`, `enabled`, `default_requirements`, `editable_fields`, `required_artifact`, `supported_tiers`, `blueprint_expectation`, `components`)이고, 서버가 `default_requirements + editable_fields 값`으로 Requirement Model 입력을 만들어 **같은 Recommendation Engine·Cost·Admission·Validator**를 실행. 편집 불가 필드를 보내면 400. 결과와 환경에 `origin`(CUSTOM / TEMPLATE + 기대 Blueprint 일치 여부) 기록. ‘세부 요구사항 바꾸기’는 Template 기본값이 채워진 질문 Wizard로 전환. **생성 가능한 Blueprint의 Template만 `enabled`**: 지금은 `STATIC_WEBSITE` 하나. 웹 API·백엔드·컨테이너·PostgreSQL Template은 해당 Blueprint 구현 후 항목 추가로 확장 | 사용자 요구(2026-10-04). 두 번째 추천 경로를 만들지 않기 위함 | 확정 |
| D-164 | **업로드 실패 안내**: 검사 결과에 zip 내용 요약(`summary`: 파일 수, 확장자 분포, html 수, 중첩 index.html 위치, 웹이 아닌 파일 수)을 함께 저장. 환경 상세에서 실패한 검사 이름 + 고치는 방법 + 찾은 것을 보여주고, 실행 파일만 있고 html이 없으면 "웹사이트 빌드 결과가 아닌 것 같아요"로 안내. 모든 실패 toast에 실패한 검사 이름 포함. **DRAFT에서는 zip을 다시 올려(rev-1 교체, 아직 서비스된 적 없음) 재검사 후 승인** 가능 | 로컬 UI 확인 중 발견(데스크톱 앱 zip 업로드 시 원인이 보이지 않음) | 확정 |
| D-165 | **Serverless Blueprint MVP**: HTTP container image → Lambda(container image) + Lambda Web Adapter → API Gateway HTTP API(`execute-api` HTTPS). **DB 없는 SERVER_APP만 생성 가능**(`SLS+NONE`); SLS_PC(고정비), 데이터 계층, 화면+API 조합, IP 제한은 추천만. 생성 가능 조합은 `platform_core/blueprints.py` 한 곳에서 관리(API·runner·worker·UI·시뮬레이터 공통). 리소스: 환경별 ECR(불변 태그·scan on push·AES256·force_delete), Lambda, 실행 role(환경 boundary), log group 2개(DEV 7일/PROD 30일), HTTP API(`$default` route·stage, access log, throttle 20/40), invoke 권한(이 API만). PROD는 Lambda Errors·API 5xx 알람 + SNS. NAT·ALB·ECS·EC2·VPC 없음(PLAN-ALLOWLIST로 강제) | 사용자 결정(2026-10-05): Static E2E 완료 후 속도 우선 | 확정 |
| D-166 | **Derived image는 `crane append`로 생성**: runner(CodeBuild, unprivileged)가 `public.ecr.aws/awsguru/aws-lambda-adapter`(버전 고정)에서 `lambda-adapter`를 꺼내 `/opt/extensions/lambda-adapter` 레이어로 **사용자 이미지 위에 덧붙여** 환경 ECR에 push, digest로 고정. 원본 이미지는 변경하지 않고 Docker daemon·privileged 모드가 필요 없음. 순서: 전체 plan → PLAN 검증 → ECR만 targeted apply → 이미지 빌드 → digest로 다시 plan → PLAN 검증 → apply. 빌드 실패는 `FAILED` + 단계 `IMAGE` + 원인 문장. 입력은 공개 레지스트리(docker.io, public.ecr.aws, ghcr.io, quay.io)의 태그/digest가 있는 이미지 | D-36 구현 방식 확정(D-36의 "파생 image 빌드"를 daemon 없이) | 확정 |
| D-167 | **Serverless MVP는 비밀값을 받지 않음**: 환경변수 값은 DynamoDB와 Terraform state에 평문으로 남으므로, 비밀처럼 보이는 key(secret/password/token/api key/credential 등)나 값(AWS key 등)은 생성 전에 거부(ARTIFACT SB-SEC-02, PLAN SB-SEC-02, STATE SB-SEC-02). SB-SEC-01/ENC-04/SEC-05는 NOT_APPLICABLE로 기록. Secrets Manager 연동(ARN 주입)은 이후 확장 | SB-SEC-02 준수와 범위 축소 | 확정 |
| D-168 | **Serverless 보안 헤더**: HTTP API에는 CloudFront의 response headers policy가 없으므로 integration `response_parameters`로 HSTS·nosniff·X-Frame-Options·Referrer-Policy를 덮어씀(주요 상태 코드). HTTPS-only는 `execute-api`가 HTTP를 제공하지 않는 것으로 검증(SB-TLS-02) | SB-TLS-05는 S/C 항목이지만 HTTP 검사(PR-ALL-04)는 동일하게 적용 | 확정 |
| D-169 | **보안 실패 시 Serverless endpoint 차단**: 예약 동시성 0(`PutFunctionConcurrency`)으로 즉시 차단(D-65). runner IAM은 Lambda·ECR·log group·role을 `sscp-env-*` 이름으로, API Gateway는 플랫폼 API를 명시적 Deny로 제외, IAM role 생성은 `iam:PermissionsBoundary = sscp-env-boundary` 조건일 때만 허용. 환경 boundary는 자기 log 쓰기만 허용 | SB-IAM-02, SB-IAM-04 | 확정 |
| D-170 | **Container Blueprint MVP** (`CTR+NONE`, DB 없음): D-33/D-44/D-46을 그대로 구현. 환경별 CloudFront(`redirect-to-https`, managed SecurityHeadersPolicy, 표준 로그, IP 제한 시 WAF) → **공유 VPC origin** → **공유 internal ALB**(private subnet, SG는 CloudFront origin-facing prefix list만) → 환경별 listener rule(origin custom header `X-Sscp-Env = env_id`, viewer가 덮어쓸 수 없음) → Fargate task(public subnet + public IP, **NAT 없음**, task SG는 ALB SG의 8080만). 이미지는 `crane copy`로 원본 그대로 환경 ECR에 복사해 digest로 고정(SB-PUB-07). 실행/앱 role 분리(둘 다 env boundary), log `/sscp/env/<id>/app`, DEV+A0는 FARGATE_SPOT, task 수 = 엔진 `ctr_min_tasks`, circuit breaker + rollback, `wait_for_steady_state`. PROD는 ALB UnHealthyHostCount·Target 5xx 알람 + SNS | 사용자 결정(2026-10-07): Serverless E2E 완료 후 속도 우선 | 확정 |
| D-171 | **공유 Container 인프라 수명(§12 refcount)**: `infra/shared/container`(VPC 2 AZ public/private, IGW, S3 gateway endpoint, internal ALB + HTTP listener 기본 404, ECS cluster FARGATE/FARGATE_SPOT, CloudFront VPC origin)를 runner가 **첫 Container 환경 생성 때** PLAN 검증(`container-shared`) 후 apply하고, **마지막 Container 환경 삭제 때** destroy. 동시 생성·삭제는 S3 lockfile + `-lock-timeout=20m`으로 직렬화, 자원을 가질 수 있는 상태(PROVISIONING~DELETE_FAILED)의 다른 환경이 있으면 유지. 공유 ALB idle timeout은 **130초**(최장 origin 120초 + 10초): ALB 하나가 모든 환경을 받으므로 환경별 timeout은 CloudFront origin timeout(30/60/90/120초)이 맡음. §12 표의 VPC flow log·DynamoDB endpoint는 C baseline에 없고 MVP에 쓰이지 않아 만들지 않음(데이터 계층 추가 때 재검토) | 고정비 0 유지(환경이 없으면 ALB도 없음), P-01 | 확정 |
| D-172 | **PLAN 검증 범위 = 최종 구성**: plan의 `no-op` 리소스도 검사(이미 만든 ECR 등). Serverless 최종 plan에서 SB-PUB-07이 N/A로 보이던 문제 수정. Container는 환경 plan에서 볼 수 없는 ALB 검사(SB-PUB-02/04)를 공유 plan 결과로 채움. 새 검사 `PLAN-FIXED-COST`: 고정비 리소스(ALB, ECS service, PC)는 추천의 `FIX_*` 이유가 있을 때만, NAT·EIP·interface endpoint는 항상 차단 | E2E 2026-10-07 발견 | 확정 |
| D-173 | **Container MVP 제외**: autoscaling(target tracking)은 만들지 않고 task 수를 추천 최소값으로 고정(PR-C-06은 desired = min 검사). Application Auto Scaling의 service-linked role이 계정에 없고, 만들려면 runner IAM을 더 넓혀야 함. 그 밖에 RDS/DynamoDB/EFS/Service Connect/Blue-Green/재배포·롤백/custom domain 제외 | 사용자 지정 MVP 범위 | 확정 |
| D-174 | **Container runner 권한**: EC2는 생성 계열만 리전 범위, 삭제 계열은 `aws:ResourceTag/project = sscp`만. ELB·ECS는 `sscp-*` 이름 범위, CloudFront VPC origin, service-linked role 생성은 VPC origin·ECS·ELB 3개 서비스만. Boundary에 ec2/elb/ecs 추가 + NAT·EIP·RunInstances·VPC peering Deny. env boundary에 ECR pull(환경 repository)과 `/sscp/env/*` 로그 추가. CodeBuild timeout 45→90분(VPC origin 생성·삭제 최대 15분씩) | SB-IAM-03, 최소 권한 | 확정 |
| D-175 | **runner 권한을 inline + managed로 분리**: 2026-10-07 platform apply가 `LimitExceeded`(role의 inline 정책 합계 10,240자)로 runner 정책에서 실패. Static·Serverless 문장은 inline(약 7,700자)에 두고, Container 문장은 managed policy `sscp-runner-container`(약 3,500자, 한도 6,144자)로 옮겨 runner role에 attach. 권한 범위는 그대로. deployer에 `iam:AttachRolePolicy`/`DetachRolePolicy`/`ListEntitiesForPolicy`를 `role/sscp-*`·`policy/sscp-*`(조건 `iam:PolicyARN = policy/sscp-*`)로만 추가. 정책 크기는 테스트가 Terraform 소스에서 렌더링해 한도의 92% 이하인지 검사 | [IAM quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html), 오프라인 테스트가 잡지 못한 한도 | 확정 |
| D-176 | **ECS 배포 추적 권한**: Container 첫 E2E(2026-10-07, env-74469f09)가 `CreateService` 성공 후 provider의 `wait_for_steady_state` 대기에서 `ecs:ListServiceDeployments` AccessDenied로 실패(task는 1개로 정상 기동, 교체 없음). `sscp-runner-container`에 읽기 전용 `EcsDeploymentTracking`(ListServiceDeployments·DescribeServiceDeployments·DescribeServiceRevisions, 리소스 `service`·`service-deployment`·`service-revision`의 `sscp-*/sscp-env-*`) 추가. managed policy 3,871/6,144자. 테스트가 `wait_for_steady_state = true`일 때 세 action과 필요한 resource type을 검사 | [Service Authorization Reference: ECS](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html), [service deployment permissions](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-deployment-permissions.html) | 확정 |
| D-177 | **worker 권한 = AWS 레퍼런스 기준 호출 지도**: Container E2E(2026-10-08, env-af602604)가 apply 성공 후 VALIDATE 첫 호출에서 `ecs:ListTasks` AccessDenied(평가 리소스 `container-instance/sscp-shared/*`). ListTasks의 필수 resource type은 `container-instance`인데 worker 정책 작성 때 이 ARN을 불필요하다고 판단해 제거했던 것이 원인. worker `InspectContainerServices`에 `container-instance/sscp-*/*` 추가(읽기 전용, worker inline 3,632/10,240자). `tests/test_worker_iam_map.py`가 Container 검증 경로의 모든 boto3 호출을 (IAM action, 실제 평가 ARN)으로 고정하고, 소스에서 새 호출이 지도에 없으면 실패. 권한을 줄일 때는 이 지도의 근거(레퍼런스 resource type)를 먼저 바꿔야 함 | [ECS Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html) | 확정 |
| D-178 | **맞춤형 경로의 프리셋 화면 제거**(D-132 대체): `맞춤형으로 만들기`는 첫 질문 "어떤 앱을 올리시나요?"로 바로 시작하고, 답을 미리 채우지 않음. 단일 선택 질문은 답을 골라야 `다음`이 활성화. 미리 채운 구성은 `빠르게 시작하기`(템플릿, D-163)가 담당 | 프리셋은 추천 결과에 영향이 없고(D-11·D-163), 두 개 모두 Static이라 Serverless·Container 사용자에게 맞는 선택지가 없었음. 첫 화면부터 결정에 쓰이는 질문으로 시작 | 확정 |
| D-179 | **SB-PUB-02 판정 기준 = 서브넷별 명시적 연결**: ALB 서브넷마다 route table에 명시적으로 연결돼 있고(연결 안 된 서브넷은 보이지 않는 main table을 쓰므로 실패), 그 table들에 IGW 경로가 없으면 PASS. 이전의 "route table 수 = 서브넷 수" 가정 제거. 테스트용 응답을 실제 AWS 형태(table 1개 + 연결 2개)로 교체 | Container E2E(2026-10-08, env-1ee973e8)에서 공유 private table 1개가 서브넷 2개를 맡는 정상 구성을 위반으로 판정해 접속 차단(오탐) | 확정 |
| D-180 | **업그레이드 방향(2026-10-11)**: ① Agent 배포 인터페이스(MCP 서버 + Claude Code skill) → ② Day-2 운영 관리(재검사·drift·image 취약점·비용) 순서로 확장. Blueprint 이름은 실행 방식 기준(정적 / 요청 단위 실행 / 상시 실행)으로 정리, 질문 구조는 유지 | 1차 발표 피드백: 기존 서비스(Beanstalk 등)와의 차별점·더 어려운 확장 필요, skill/MCP 통합과 배포 후 관리 제안 | 확정 |
| D-181 | **Agent 권한 = 생성 요청까지**: agent 도구는 저장소 분석·요구사항 초안·추천 조회·'승인 대기' 환경 생성 요청·상태 조회·실패 설명만. 승인(실제 AWS 변경)은 사람이 UI에서만. agent에게 AWS 자격 증명을 주지 않고 플랫폼 API만 사용 | CFP human-in-the-loop 원칙, agent가 AWS를 바꾸는 경로를 구조적으로 제거 | 확정 |
| D-182 | **Blueprint 표시 이름 = 실행 방식**: 정적 호스팅(CloudFront+S3) / 요청 단위 실행(Lambda) / 상시 실행 컨테이너(Fargate). 내부 id(static/serverless/container)는 유지. 추천 화면 제목이 "<실행 방식> 구성을 추천해요" | 1차 발표 피드백 ⑤: Fargate도 서버리스라 Serverless/Container 구분이 모호 | 확정 |
| D-183 | **베이스라인 공개 앱 13개 + 저장소 분석기 v1**: `docs/baseline/apps.json`(commit 고정, 근거 파일, SA 기대 결과; 정답은 이진서 검토 전 초안), `agent/repo_analyzer.py`(규칙 기반·결정적, 값마다 근거, blueprint를 바꾸는 답만 개발자 확인), `scripts/eval_analyzer.py`(필드 정확도 + 엔진 결과 일치율). DB 엔진은 코드에서 감지하고 개발자가 확인하는 대상(추천 아님). 경계·비교 조사: `docs/research/boundary-and-comparison.md` | 1차 발표 피드백 ②③과 최초 숙제(베이스라인 앱 조사), Agent 평가 세트 | 확정 |
| D-184 | **평가 규칙 = held-out 분리**: 분석기 정확도는 규칙을 만들 때 보지 않은 앱으로만 보고한다. 앱과 정답은 분석기를 돌리기 전에 기록하고, 그 결과로 규칙을 고치지 않는다. 고친 규칙(v2)은 새 held-out 세트로 잰다. 1차 held-out(5개): 결과 일치 20%(확인 후 60%), 개발 세트 92%와의 차이와 원인은 `docs/baseline/README.md` | 개발 세트 수치만으로는 과적합을 알 수 없음. 최종 지표로 쓰려면 공정한 측정이 필요 | 확정 |
| D-185 | **Agent 배포 인터페이스 v1**: Cognito agent 전용 클라이언트(토큰의 aud로 구분) + API 허용 목록(`AGENT_ROUTES`: 조회·추천·초안 생성·초안 업로드만). 승인·삭제·재시도·재배포·연장·예산 초과 동의는 agent 토큰이면 403, agent 토큰은 admin 권한도 없음. 비밀번호는 개발자 터미널(`python -m agent.login`)에서만 입력, MCP 서버는 refresh token만 사용. MCP 서버(`agent/mcp_server.py`, 도구 7개) + Claude Code skill(`.claude/skills/sscp-deploy`). UI는 agent가 만든 초안을 표시 | D-181을 도구 생략이 아니라 서버에서 강제 | 확정 |
| D-186 | **Day-2 drift 감지·복구**: 감지 = CloudTrail 쓰기 이벤트 → EventBridge 규칙(`infra/platform/day2.tf`) → worker `recheck` (초~1분) + 매시간 sweep의 전체 재검사(CloudFront·IAM 같은 전역 서비스 이벤트는 us-east-1에만 오므로 이 경로). drift = 승인 당시 통과한 STATE·HTTP 검사가 지금 실패(이미 실패하던 항목은 제외). 상태 DRIFTED + 누가·무엇을·언제 기록, 보안 drift는 즉시 접속 차단(D-65). 복구 = runner `repair_plan`(승인된 구성으로의 plan + PLAN 검사, 저장) → 사람이 화면에서 승인(`POST /repair`, agent 403) → runner `repair_apply`(저장된 그 plan만 적용) → 재검증 → READY, 감지·복구 시간 기록. 플랫폼 자신의 role이 한 변경은 drift 아님. DRIFTED·REPAIRING은 공유 컨테이너 인프라를 점유(HOLDS_SHARED) | 강사 피드백 '배포된 걸 어떻게 관리할지', 사용자 선택(이벤트+주기, 수정안+사람 승인) | 확정 |
| D-187 | **새 환경 경로 이름**: `맞춤형으로 만들기`(질문 → 추천) → **내 앱에 맞게 만들기**, `빠르게 시작하기`(준비된 구성으로 바로 생성) → **템플릿으로 시작하기**. 동작과 이름을 일치시킴(고정 템플릿 경로를 '맞춤형'으로 부르면 반대로 읽힘) | 사용자 결정(2026-10-28) | 확정 |
| D-188 | **Day-2 첫 AWS E2E(2026-10-28, env-c9fc390e, Static DEV)에서 고친 3가지**: ① API Gateway에 `/recheck`·`/repair` 라우트 누락 → 복구 클릭이 CORS 없는 404 = "Failed to fetch"(로컬 서버는 Lambda 라우트 표를 그대로 써서 못 잡음) → `api.tf` 추가 + `tests/test_api_routes_sync.py`. ② 차단(D-65)으로 생긴 실패(CloudFront 비활성 `PR-ALL-02/enabled`, HTTP 검사)를 매시 sweep이 새 drift로 판정 → drift 기록(누가·이벤트·감지 시간)을 덮어쓰고 수정안을 다시 만듦 → `drift.block_effects`로 제외, 차단 중엔 손으로 되돌려도 READY로 안 바꿈(복구 승인만 접속 재개). ③ `repair_seconds`가 사람 승인 대기를 포함 → `wait_seconds`(감지→승인)·`apply_seconds`(승인→READY) 분리. ②가 로컬에서 안 보인 이유: `local_dev`의 차단이 메시지만 남기고 아무것도 막지 않아 재검사가 계속 통과 → 차단 중 CDN·HTTP 검사가 AWS처럼 실패하도록 시뮬레이터 수정, 에이전트 E2E(`test_agent_flow`)가 수정 제거 시 AWS와 같은 증상(`event_name` None)으로 실패함을 확인. 증거 수집용 읽기 전용 `scripts/day2_report.ps1`. **2차 AWS E2E(2026-11-04, env-9183234c, 에이전트가 초안 생성)로 수정 확인**: 감지 11초 → 수정안 48초(plan 1회) → 차단 중 에이전트 MANUAL 재검사가 1차 sweep과 같은 실패 6개를 봤지만 drift 기록 1건·actor/이벤트 유지 → 승인 대기 6분 04초 / 승인 후 복구 3분 30초 → READY, BPA 4개 on, HTTPS 200. 에이전트 토큰의 repair/approve/extend는 403. 승인→READY 3분 54초(1차 3분 58초) 실측: 변경 07:26:42Z → 감지 **9초** → 수정안 준비 40초(PLAN 검사 통과, 변경 2개) → 승인 후 복구 **3분 47초**(SFN apply 실행) | AWS E2E 실측 | 확정 |
| D-189 | **B-1 로그 버킷 versioning (SB-LOG-01)**: `aws_s3_bucket_versioning.logs`(Enabled) + 수명 주기에 noncurrent 버전 30일 만료. STATE에 `SB-LOG-01/versioning`(Security) 추가, 우선 Static만(Serverless·Container 검증기는 S3 클라이언트가 없어 별도 작업). worker 읽기 권한 `s3:GetBucketVersioning`(로그 버킷 한정)은 별도 inline 정책 `log-bucket-versioning-read`(기존 worker 정책은 state machine을 참조하고 state machine은 worker Lambda를 참조해 순환이 생기므로 분리). **배포 순서 보장**: worker Lambda에 `depends_on = [versioning 읽기 정책, 로그 버킷 versioning]`(`tests/test_log_versioning_rollout.py`) → 새 검사 코드가 설정·권한보다 먼저 배포되어 모든 Static 환경이 보안 drift로 차단(D-65)되는 경우를 Terraform이 막음 | security-baseline SB-LOG-01("로그 bucket 보호 … versioning"), Finalization Backlog B-1 | 코드 완료, apply 대기 |
| D-190 | **held-out 세트 3 (분석기 v3용) 고정**: `docs/baseline/heldout3.json` 6개(h5bp, cloud-run-hello, todo-app, voting-app, excalidraw-room, python-getting-started). v3 구현·실행 **전**에 앱·commit·정답을 기록. v3 규칙은 README §4 표(2026-10-27)로 이미 정해져 있고, 이 앱들 때문에 표 밖의 규칙을 추가하지 않음. 3개는 v3의 목표(하위 폴더·모노레포)를 겨냥하되 **하위 폴더를 앱으로 오인하면 틀리는** 방향으로 고름(dist/·src/, job/·worker-pool/, vote/·result/·worker/). `expected_outcome`은 분석기가 아니라 정답 필드를 실제 엔진에 넣어 계산(`scripts/heldout3_expected.py`) | D-184 | 정답 초안, 이진서 검토 대기 |

## 16. Finalization Backlog (2026-10-05, 사용자 지정 — Serverless 이후 처리)

Static 결과는 **STATIC BLUEPRINT E2E COMPLETE**로 유지한다. 아래는 속도를 위해 미룬 항목이며 삭제하지 않는다.

| # | 항목 | 비고 |
|---|---|---|
| B-1 | SB-LOG-01 로그 버킷 versioning 보완 | `baseline.tf`에 `aws_s3_bucket_versioning.logs` + noncurrent 만료, STATE SB-LOG-01에 versioning 검사 추가 → **코드 완료(D-189), apply 대기** |
| B-2 | Negative E2E | `scripts/e2e_static.py --negative` (실제 리소스 생성, 별도 승인) |
| B-3 | Engineering log 정리 | 첫 apply, plan 리뷰(CloudFront 권한 축소), presigned URL 장애 → **초안 [engineering-log.md](engineering-log.md)** (CloudFront 권한 축소 세부는 기록 확인 필요) |
| B-4 | Postmortem 작성 | 업로드 "Failed to fetch": 글로벌 S3 endpoint 307 → CORS 실패, 로컬 시뮬레이터가 못 잡은 이유, 재발 방지 테스트 → **초안 [postmortem-failed-to-fetch.md](postmortem-failed-to-fetch.md)** (D-188 라우트 404와 함께) |
| B-5 | README / 제안서 실측값 반영 | 생성 3분 58초, 재배포 9초, rollback 5초, 삭제 4분 5초 → **README 반영**(제안서는 미반영) |
