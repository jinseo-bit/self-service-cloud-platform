# Security Baseline

- 상태: 범주는 **확정**(D-80), 세부 항목은 **DRAFT — 승인 대기**
- 최종 갱신: 2026-10-04
- 원칙: 이 문서의 모든 항목은 **비용을 이유로 제거하거나 완화할 수 없다**(원칙 4). Recommendation Engine은 이 항목을 끄는 출력을 만들 수 없고, 예산 완화안에도 포함하지 않는다.

## 0. 읽는 법

각 항목에는 다음 정보가 붙는다.

- **적용**: `ALL` 또는 특정 Blueprint/구성요소(Static `S`, Serverless `L`, Container `C`, RDS `R`, DynamoDB `D`)
- **검증 계층**: `PLAN`(terraform plan 정적 분석) / `STATE`(배포 후 AWS API 검사) / `HTTP`(외부 접근 검사)
- 위반 시 처리(D-65): `PLAN` 위반 → apply 차단. `STATE`·`HTTP` 위반 → endpoint 비활성화 + FAILED

---

## 1. IAM 최소 권한 (SB-IAM)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-IAM-01 | 환경마다 **전용 실행 role**(Lambda execution role, ECS task role/execution role)을 둔다. 환경 간 role 공유 금지 | L, C | PLAN, STATE |
| SB-IAM-02 | 정책 Resource는 해당 환경의 리소스 ARN 또는 `env-id` tag 조건으로 한정한다. `Action: "*"`나 `Resource: "*"`의 쓰기 권한 금지(서비스 특성상 불가피한 read 계열만 예외 목록으로 관리) | ALL | PLAN |
| SB-IAM-03 | Terraform runner role에 **permission boundary**를 적용한다. 허용 서비스 목록 밖의 리소스 생성, IAM 사용자·access key 생성, boundary 제거를 금지 | 플랫폼 | PLAN, STATE |
| SB-IAM-04 | runner가 만드는 모든 role에도 같은 boundary를 강제한다(`iam:PermissionsBoundary` 조건) | ALL | STATE |
| SB-IAM-05 | 플랫폼 사용자에게는 AWS credential을 주지 않는다. 모든 변경은 플랫폼 API를 거친다(User/Admin 권한은 앱 계층에서 처리) | 플랫폼 | 설계 검토 |
| SB-IAM-06 | 플랫폼 control plane Lambda도 기능별 role로 분리한다(추천 엔진은 AWS 쓰기 권한 0) | 플랫폼 | PLAN |

## 2. 저장 데이터 암호화 (SB-ENC)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-ENC-01 | S3 bucket 기본 암호화(SSE-S3 이상) | S, 공유 bucket | STATE |
| SB-ENC-02 | RDS/Aurora storage 암호화. snapshot도 암호화 상태로 생성 | R | PLAN, STATE |
| SB-ENC-03 | DynamoDB 서버 측 암호화(기본 AWS owned key 이상) | D | STATE |
| SB-ENC-04 | Secrets Manager secret은 KMS로 암호화(기본 aws/secretsmanager key 허용) | ALL | STATE |
| SB-ENC-05 | CloudWatch Logs, ECR image 저장 암호화(서비스 기본값 유지 확인) | L, C | STATE |

AWS managed key를 허용하는 근거: 기준은 "저장 시 암호화 여부"다. customer-managed KMS key는 key마다 월 고정비가 들고 이 기준을 강화하지 않으므로 선택 사항으로 둔다.

## 3. 전송 구간 TLS (SB-TLS)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-TLS-01 | 사용자에게 노출되는 endpoint는 **HTTPS만** 제공한다. CloudFront viewer protocol = `redirect-to-https` | S, C, (L via CloudFront) | PLAN, HTTP |
| SB-TLS-02 | API Gateway는 HTTPS endpoint만 제공(서비스 특성상 HTTP 미제공) | L | HTTP |
| SB-TLS-03 | RDS는 `rds.force_ssl = 1`(TLS가 아닌 DB 접속 거부) | R | STATE |
| SB-TLS-04 | S3 bucket policy에 `aws:SecureTransport = false` 요청 Deny | S, 공유 bucket | PLAN |
| SB-TLS-05 | CloudFront response headers policy로 HSTS 등 보안 header 적용(managed SecurityHeadersPolicy) | S, C | HTTP |

## 4. Secret 관리 (SB-SEC)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-SEC-01 | 사용자 비밀값과 DB credential은 Secrets Manager에 저장한다 | L, C, R | STATE |
| SB-SEC-02 | 실행 환경에는 **참조로만 주입**한다. ECS는 `secrets.valueFrom`, Lambda는 환경변수에 ARN만 두고 runtime에 조회. 평문 환경변수 금지 | L, C | PLAN |
| SB-SEC-03 | RDS master password는 RDS가 관리하는 Secrets Manager secret을 쓴다(`manage_master_user_password`). Terraform state에 평문 password가 남지 않게 함 | R | PLAN |
| SB-SEC-04 | Static artifact에는 secret을 받지 않는다(D-16). 업로드한 번들에서 흔한 key 패턴(AWS key 등)을 탐지하면 경고 | S | PLAN 전 검사 |
| SB-SEC-05 | 실행 role은 **자기 환경의 secret만** 읽을 수 있다(SB-IAM-02 적용) | L, C | PLAN |
| SB-SEC-06 | Terraform state bucket: 암호화, versioning, Block Public Access, runner role만 접근 | 플랫폼 | STATE |

## 5. Public 접근 차단 (SB-PUB)

정의(D-44): **지정된 edge(CloudFront 또는 API Gateway) 외에는 인터넷에서 환경으로 들어오는 inbound 경로가 0개다.**

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-PUB-01 | S3 Block Public Access 4개 항목 모두 on. CloudFront OAC로만 읽기 | S, 공유 bucket | STATE, HTTP(S3 직접 URL → 403) |
| SB-PUB-02 | ALB는 `internal` scheme이며 private subnet에 둔다. CloudFront VPC origin으로만 접근 | C | STATE |
| SB-PUB-03 | Fargate task SG inbound는 **ALB SG에서 오는 앱 port만** 허용한다. public IP가 있어도 인터넷 inbound는 0 | C | STATE(SG rule 검사) |
| SB-PUB-04 | ALB SG inbound는 CloudFront VPC origin 서비스 SG(또는 managed prefix list)만 허용한다 | C | STATE |
| SB-PUB-05 | RDS/Aurora는 private subnet에 두고 `publicly_accessible = false`. SG inbound는 앱 SG(task 또는 VPC Lambda)만 허용 | R | PLAN, STATE |
| SB-PUB-06 | Lambda Function URL 사용 금지. Serverless 입구는 API Gateway 하나 | L | PLAN |
| SB-PUB-07 | ECR repository는 private. scan on push 활성 | L, C | STATE |
| SB-PUB-08 | `exposure = IP_ALLOWLIST`이면 CloudFront에 WAF IP set을 적용하고, 우회 경로가 없는 Blueprint만 허용한다(Serverless 제외) | S, C | STATE, HTTP |
| SB-PUB-09 | NAT Gateway를 만들지 않는다(D-44). VPC Lambda는 interface endpoint로만 AWS 서비스에 접근 | ALL | PLAN |
| SB-PUB-10 | 0.0.0.0/0 inbound를 허용하는 SG가 하나도 없다 | ALL | PLAN, STATE |

## 6. Logging (SB-LOG)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-LOG-01 | 계정 CloudTrail(management event) 활성, 로그 bucket 보호(SB-ENC-01, SB-PUB-01, versioning) | 계정 | STATE |
| SB-LOG-02 | CloudFront 표준 접근 log → 공유 log bucket | S, C | STATE |
| SB-LOG-03 | API Gateway access log → CloudWatch Logs | L | STATE |
| SB-LOG-04 | 앱 stdout/stderr → CloudWatch Logs, 보관 기간을 반드시 설정(dev 7일 / prod 30일, 무기한 금지) | L, C | PLAN |
| SB-LOG-05 | 공유 VPC flow log → S3(lifecycle로 보관 기간 제한) | 공유 VPC | STATE |
| SB-LOG-06 | 플랫폼 감사 log: 누가 어떤 입력으로 어떤 추천을 받고 무엇을 승인·삭제했는지(input_hash, rules_version 포함)를 DynamoDB에 기록 | 플랫폼 | 테스트 |

## 7. 공통 tag (SB-TAG)

| ID | 기준 | 적용 | 검증 |
|---|---|---|---|
| SB-TAG-01 | 모든 리소스에 `env-id`, `owner`, `tier`, `blueprint`, `managed-by=platform` tag를 붙임. `env-id`는 IAM 조건과 Cost Allocation Tag에 사용 | ALL | PLAN(provider default_tags), STATE |

## 8. Blueprint별 적용 요약

| 범주 | Static | Serverless | Container | + RDS | + DynamoDB |
|---|---|---|---|---|---|
| IAM | IAM-02 | IAM-01·02·04 | IAM-01·02·04 | — | IAM-02 |
| ENC | ENC-01 | ENC-04·05 | ENC-04·05 | ENC-02 | ENC-03 |
| TLS | TLS-01·04·05 | TLS-02 | TLS-01·05 | TLS-03 | — |
| SEC | SEC-04 | SEC-01·02·05 | SEC-01·02·05 | SEC-01·03 | — |
| PUB | PUB-01 (+08) | PUB-06·07 | PUB-02·03·04·07 (+08) | PUB-05 | — |
| LOG | LOG-02 | LOG-03·04 | LOG-02·04 | — | — |
| 공통 | TAG-01, PUB-09·10, LOG-01 | 동일 | 동일 | 동일 | 동일 |

## 9. 기본 도메인 사용에 따른 예외 (승인됨 2026-10-04, P-10)

AWS 기본 도메인을 쓰기로 한 결정(D-42) 때문에 생기는 기술적 제약이다. 숨기지 않고 예외로 명시하며, 사용자 도메인이 도입되면 해소한다.

| ID | 예외 | 이유 | 보완책 | 해소 조건 |
|---|---|---|---|---|
| SB-EXC-01 | CloudFront → internal ALB 구간은 HTTP | internal ALB의 DNS 이름(amazonaws.com)으로는 ACM 인증서를 발급받을 수 없음 | VPC origin은 public 인터넷을 거치지 않는 AWS 내부 경로. ALB는 internal, SG로 CloudFront만 허용 | 사용자 도메인 도입 후 origin HTTPS |
| SB-EXC-02 | viewer TLS 최소 버전을 지정할 수 없음 | 기본 `*.cloudfront.net` 인증서에서는 security policy를 직접 설정할 수 없음 | HTTPS 강제(redirect)와 HSTS는 유지 | 사용자 도메인 + ACM 인증서에서 TLSv1.2_2021 이상 |

## 10. 범위 밖 (현재 baseline 아님)

비용이나 범위 때문에 **baseline에 넣지 않은** 항목이다. baseline을 줄인 것이 아니라 애초에 포함하지 않은 항목이며, 선택 기능 후보로 관리한다.

- 기본 WAF managed rule(공개 범위가 IP 제한일 때만 WAF 사용)
- GuardDuty, Security Hub, AWS Config 지속 감시(drift 감시는 범위 외, D-91)
- customer-managed KMS key
- secret 자동 rotation
