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
| D-132 | 프리셋: **포트폴리오·블로그 / 회사·서비스 소개 페이지** 2개(Static) | Static 우선 구현 | 확정 |
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
