# Production-Ready 판정 기준

- 상태: 정의 방식은 **확정**(D-90, D-91), 판정 항목은 **DRAFT — 승인 대기**
- 최종 갱신: 2026-10-04

## 1. 정의

> **production-ready = 해당 Workload를 실제로 안정적이고 안전하게 운영할 수 있는 표준화된 환경이며, 아래 기준을 플랫폼이 자동으로 검사해 모두 통과한 상태.**

특정 기술을 썼다는 의미가 아니다. "검사로 증명된 상태"를 뜻한다. 기준은 6개 차원으로 나뉘고, Blueprint마다 구체적인 검사 항목이 다르다.

| 차원 | 질문 |
|---|---|
| 1. Security | [security-baseline.md](security-baseline.md)를 100% 만족하는가 |
| 2. Reachability | 사용자가 HTTPS로 앱에 도달하고, 앱이 정상 응답하는가 |
| 3. Resilience | 멈춤 허용도 입력에 맞는 장애 내성을 갖췄는가 |
| 4. Observability | 등급에 맞는 log와 알람이 동작하는가 |
| 5. Operability | 재배포, 실패 시 rollback, 삭제, TTL이 동작하는가 |
| 6. Cost control | 비용 추적과 폭주 방어 장치가 걸려 있는가 |

## 2. 판정 결과

| 결과 | 조건 | 처리(D-65) |
|---|---|---|
| `READY` | 모든 필수 검사 통과 | 사용자에게 endpoint 제공 |
| `DEGRADED` | Security는 통과했지만 Reachability(앱 health) 실패 | endpoint 유지, 원인 안내(log, health 응답) |
| `FAILED` | Security·Resilience·Cost control 중 하나라도 실패, 또는 provisioning 실패 | endpoint 비활성화, retry/delete 선택, 방치하면 TTL로 정리 |

책임 구분: Security·Resilience·Cost는 **플랫폼 책임**(실패하면 플랫폼 결함)이고, 앱 health는 **사용자 코드 책임**이다. 그래서 둘을 다른 상태로 나눈다.

## 3. 공통 검사 (모든 Blueprint)

| ID | 차원 | 검사 | 계층 |
|---|---|---|---|
| PR-ALL-01 | Security | Security Baseline 해당 항목 전체 통과 | PLAN, STATE, HTTP |
| PR-ALL-02 | Reachability | 환경 URL HTTPS 200(또는 지정된 health 경로) | HTTP |
| PR-ALL-03 | Reachability | `http://` 요청 → HTTPS로 redirect | HTTP |
| PR-ALL-04 | Reachability | 보안 header(HSTS 등) 존재 | HTTP |
| PR-ALL-05 | Observability | 등급별 log 보관 기간 설정, 앱 log가 실제로 수집됨(검증 요청 후 log 존재 확인) | STATE |
| PR-ALL-06 | Observability | prod: 핵심 알람 생성 + SNS 구독 확인 | STATE |
| PR-ALL-07 | Operability | 환경 메타데이터(input_hash, rules_version, terraform state key, TTL) 기록 | 테스트 |
| PR-ALL-08 | Operability | dev: TTL 만료 시각 설정, prod: deletion protection 설정 | STATE |
| PR-ALL-09 | Cost | 필수 tag 전부 존재(SB-TAG-01) | STATE |
| PR-ALL-10 | Cost | 추천 출력의 리소스 상한(task max, concurrency, ACU 등)이 실제 설정과 일치 | STATE |
| PR-ALL-11 | Operability | 생성 소요 시간 기록(SLO 지표, D-92) | 측정 |

## 4. Static

| ID | 차원 | 검사 | 계층 |
|---|---|---|---|
| PR-S-01 | Security | S3 객체 URL 직접 접근 → 403 | HTTP |
| PR-S-02 | Reachability | SPA라면 존재하지 않는 경로가 index.html로 응답(설정된 경우) | HTTP |
| PR-S-03 | Operability | 재배포 후 CloudFront invalidation이 완료되고 새 버전이 응답 | HTTP |
| PR-S-04 | Security | `IP_ALLOWLIST`: 허용되지 않은 IP에서 403(검증 runner가 허용 목록 밖에서 요청) | HTTP |
| PR-S-05 | Resilience | 기본적으로 다중 AZ(S3 + CloudFront). 추가 검사 없음 | — |

## 5. Serverless

| ID | 차원 | 검사 | 계층 |
|---|---|---|---|
| PR-L-01 | Reachability | API endpoint health 경로 200, cold start 이후 응답 시간 기록 | HTTP |
| PR-L-02 | Security | Lambda Function URL이 없음, image는 private ECR에 있고 scan 결과 기록 | STATE |
| PR-L-03 | Cost | reserved concurrency와 API GW throttling 값이 추천 출력과 일치 | STATE |
| PR-L-04 | Performance | `SLS_PC`: provisioned concurrency가 READY 상태 | STATE |
| PR-L-05 | Security | + RDS: Lambda가 private subnet에 있고 SG가 DB만 허용 | STATE |
| PR-L-06 | Operability | 재배포: 새 version을 alias로 전환. 검증에 실패하면 이전 version으로 alias를 되돌림 | STATE, HTTP |

> **구현 상태(2026-10-05, D-165)**: Serverless MVP(DB 없음)의 실제 검사 목록은 `platform_core/production_ready.py`의 `SERVERLESS_CHECKS`(23개)다. ARTIFACT(IMG-REF·SB-SEC-02) → PLAN(PLAN-ALLOWLIST·SB-IAM-04·SB-PUB-06·SB-LOG-04·SB-PUB-09·SB-TAG-01 외) → STATE(PR-L-01~04, SB-IAM-01/02/04, SB-LOG-03/04, SB-PUB-06/07, SB-ENC-05, SB-LOG-01, 태그·TTL·PROD 알람) → HTTP(PR-ALL-02·PR-L-05 앱 직접 응답·PR-ALL-03 HTTPS only·PR-ALL-04 보안 헤더). 테스트가 목록의 모든 id가 실제로 계산되는지 확인한다.

## 6. Container

| ID | 차원 | 검사 | 계층 |
|---|---|---|---|
| PR-C-01 | Security | ALB DNS로 외부에서 직접 접근 불가(internal) | STATE, HTTP |
| PR-C-02 | Security | task public IP의 앱 port로 외부에서 직접 접근 → 연결 실패 | HTTP |
| PR-C-03 | Reachability | ALB target group의 모든 target이 healthy | STATE |
| PR-C-04 | Resilience | running task 수 ≥ min, A1/A2이면 task가 AZ 2개에 분산 | STATE |
| PR-C-05 | Operability | deployment circuit breaker + rollback 활성 | STATE |
| PR-C-06 | Cost | autoscaling min/max가 추천 출력과 일치 | STATE |
| PR-C-07 | Security | `IP_ALLOWLIST`: 허용되지 않은 IP에서 403 | HTTP |

> **구현 상태(2026-10-07, D-170)**: Container MVP(DB 없음)의 실제 검사 목록은 `platform_core/production_ready.py`의 `CONTAINER_CHECKS`(28개)다. PLAN은 환경 plan과 공유 인프라 plan을 함께 검사한다(SB-PUB-02/04는 공유 plan). PR-C-06은 autoscaling을 만들지 않는 MVP에서 "실행 개수 = 추천 최소값"을 검사한다(D-173). PR-C-02는 task의 public IP로 앱 포트에 직접 접속해 연결이 막히는지 실제로 확인한다.

## 7. Data

| ID | 대상 | 검사 | 계층 |
|---|---|---|---|
| PR-R-01 | RDS/Aurora | 암호화, `publicly_accessible=false`, force_ssl | STATE |
| PR-R-02 | RDS/Aurora | Multi-AZ 여부가 추천 출력과 일치(A1/A2) | STATE |
| PR-R-03 | RDS/Aurora | backup 보관 기간: dev 1일 / prod 7일, prod는 deletion protection | STATE |
| PR-R-04 | RDS/Aurora | 앱이 secret 참조로 DB에 접속 성공(health 경로가 DB를 거치게 하거나 플랫폼 probe) | HTTP |
| PR-D-01 | DynamoDB | 암호화, prod는 PITR + deletion protection | STATE |
| PR-D-02 | DynamoDB | on-demand maximum throughput 상한 설정 | STATE |
| PR-D-03 | DynamoDB | 실행 role 권한이 해당 table ARN으로 한정 | PLAN |

## 8. 차원별 적용 매트릭스

| 차원 | Static | Serverless | Container |
|---|---|---|---|
| Security | ALL-01, S-01, S-04 | ALL-01, L-02, L-05 | ALL-01, C-01, C-02, C-07 |
| Reachability | ALL-02~04, S-02 | ALL-02~04, L-01 | ALL-02~04, C-03 |
| Resilience | 기본 제공 | 기본 제공 | C-04 |
| Observability | ALL-05·06 | ALL-05·06 | ALL-05·06 |
| Operability | ALL-07·08, S-03 | ALL-07·08, L-06 | ALL-07·08, C-05 |
| Cost | ALL-09·10 | ALL-09·10, L-03 | ALL-09·10, C-06 |

## 9. 검사 자체의 검증

D-111("위반을 주입하면 차단되는지까지 확인")에 따라, 각 검사에 **음성 테스트**를 둔다. 의도적으로 기준을 위반한 구성을 넣었을 때 해당 검사가 실패하는지 확인한다.

예시:
- SG에 0.0.0.0/0 inbound를 추가한 plan → PLAN 단계에서 차단
- S3 BPA를 끈 상태로 배포 → PR-S-01·PR-ALL-01 실패 → FAILED
- `/healthz`에서 500을 반환하는 image → DEGRADED
- task SG에 앱 port 공개 → PR-C-02 실패 → FAILED + endpoint 비활성화
