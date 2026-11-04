# Recommendation Engine 설계

- 상태: **v1.0 설계 — 제안 P-01~P-10 승인 반영, 구현 착수는 최종 승인 대기** (승인 전에는 코드 작성 안 함)
- 최종 갱신: 2026-10-04
- 전제: [project-decisions.md](project-decisions.md)의 확정 결정
- 비용 수치는 **설명용 근사값**이다. 실제 계산은 Price List API 단가 snapshot으로 한다.

---

## 1. 설계 원칙과 결정성 계약

Engine은 **순수 함수**다.

```
recommend(input, rules_version, pricing_snapshot_id) -> Recommendation
```

| 계약 | 구현 방법 |
|---|---|
| 같은 입력이면 같은 출력 | 현재 시각, 난수, 외부 상태를 입력으로 쓰지 않음. 플랫폼에 이미 존재하는 공유 리소스 상태도 사용하지 않음(§9.3) |
| 계약의 범위 | architecture 선택과 사용자 예산 검사까지가 순수 함수. 플랫폼 전체 상한과 quota 검사(`BUD_PLATFORM_CAP`, `BUD_QUOTA_EXCEEDED`)는 계정 상태에 의존하는 **별도 admission 단계**이며, architecture를 바꾸지 않고 승인 가능 여부만 바꿈 |
| 버전 고정 | 출력에 `rules_version`, `pricing_snapshot_id`, `input_hash`를 기록. 둘 중 하나가 바뀌면 다른 결과가 나와도 정상 |
| 순서 안정성 | 후보 열거와 정렬 key를 고정(§9.4). 해시맵 순회 순서에 의존하지 않음 |
| 정수 연산 | 비용은 **cent 정수**로 계산하고, 비교할 때는 **달러 단위로 반올림**한 값을 씀 |
| 설명 가능성 | 모든 선택·제외·고정비 사용에 reason code를 붙임(§14). reason code 없는 결정은 테스트에서 실패 처리 |

거대한 if/else 대신 **4단계 구조**를 쓴다. 각 단계는 독립적으로 테스트할 수 있다.

```
RawInput
  │ ① Normalize      : UI 입력 → Requirement Model (기술 값으로 변환, 출처 기록)
  │ ② Gate           : 지원 불가 / 충돌 판정 → UNSUPPORTED · NEEDS_INPUT 조기 종료
  │ ③ Enumerate      : 후보 공간 = Compute variant × DB variant (최대 수십 개)
  │ ④ Filter         : 우선순위 순서의 제약 필터 (기능 → 보안 → 가용성 → 성능)
  │                     탈락할 때마다 EXC_* reason 기록
  │ ⑤ Rank           : 대표값 비용 → 복잡도 → 고정비 → candidate_id
  │ ⑥ Budget check   : 상한값 비용 vs 사용자 예산 / 플랫폼 상한 / quota
  │                     초과하면 BLOCKED_BUDGET + 완화안(엔진 재실행)
  │ ⑦ Attach policy  : 등급 정책, Security Baseline, Shared infra, Guardrail
  ▼
Recommendation (status, architecture, reasons[], exclusions[], warnings[], cost)
```

필터 순서가 곧 선택 우선순위(project-decisions §0)다. 그래서 **상위 원칙이 하위 원칙 때문에 희생되는 경로가 구조적으로 존재하지 않는다.** 비용(⑤)은 앞 단계의 필터를 통과한 후보끼리만 비교한다.

---

## 2. Requirement Model

### 2.1 Raw Input (UI가 수집하는 값)

| 필드 | 값 | UI 표현(예) |
|---|---|---|
| `app_form` | `STATIC_SITE` / `SERVER_APP` / `STATIC_PLUS_API` | 화면만 / 서버 앱(API·서버 렌더링) / 화면 + API |
| `capabilities` (app_form ≠ STATIC_SITE일 때만) | `realtime_connection: bool`<br>`request_duration: SECONDS \| ABOUT_30S \| S30_TO_60S \| S60_TO_120S \| OVER_120S` (D-157, 단일 선택)<br>`large_payload: bool` (요청/응답 6MB 초과)<br>`in_memory_state: bool`<br>`non_http: bool` | 실시간 연결(채팅, 알림 push) / **가장 오래 걸리는 요청**(몇 초 / 30초 정도 / 30초~1분 / 1~2분 / 2분 넘게) / 큰 파일 업로드·다운로드 / 서버 메모리에 로그인 상태 등 보관 / HTTP가 아닌 통신 |
| `traffic_scale` | `T1` ≤100 / `T2` ≤1,000 / `T3` ≤10,000 / `T4` >10,000 (하루 사용자) | 하루 몇 명 정도 쓰나요? |
| `traffic_pattern` | `IDLE_MOSTLY` / `BUSINESS_HOURS` / `STEADY` / `SPIKY` | 거의 안 씀 / 업무시간에만 / 하루 종일 꾸준히 / 갑자기 몰림 |
| `data.needed` | bool | 데이터를 저장해야 하나요? |
| `data.detected` / `data.declared` (D-134) | detected: `NONE` / `POSTGRES` / `DYNAMODB` / `OTHER`, declared: `UNKNOWN` / `POSTGRES` / `DYNAMODB` | (감지 결과 확인) 앱이 쓰는 DB를 아시나요? |
| `data.traits` (hint가 UNKNOWN일 때만) | `relational: bool`, `flexible_query: bool`, `key_lookup_only: bool` | 데이터끼리 연결이 많나요? / 여러 조건으로 검색·집계하나요? / ID로만 꺼내 쓰나요? |
| `downtime_tolerance` | `OK` / `NOT_IN_BUSINESS_HOURS` / `NEVER` | 잠깐 멈춰도 됨 / 업무시간엔 안 됨 / 절대 안 됨 |
| `latency_sensitivity` | `TOLERANT` / `SENSITIVE` | 첫 요청이 1~2초 느려도 괜찮나요? |
| `exposure` | `PUBLIC` / `APP_AUTH` / `IP_ALLOWLIST(cidrs)` | 누구나 / 로그인한 사용자만(앱이 처리) / 특정 IP만 |
| `budget_usd_month` | 정수 또는 null(null이면 플랫폼 기본 상한 적용) | 한 달 최대 예산 |
| `tier` | `DEV` / `PROD` | 개발·테스트용 / 실제 운영용 |
| `config` | `env{}`, `secrets[]` | 설정값 / 비밀값 |

**Quick Start Template (D-163)**: Template은 이 Raw Input을 만드는 또 하나의 방법일 뿐이다. `default_requirements`에 `editable_fields`(현재 tier, budget_usd_month, spa_fallback)만 덮어써 Raw Input을 만들고, 이후 Normalize → … → Policy는 질문 흐름과 완전히 같다. 같은 Raw Input이면 Template이든 질문이든 `input_hash`와 결과가 같다(테스트로 고정). Template별 분기 규칙은 엔진에 없다.

각 필드에는 출처 `provenance ∈ {PRESET, USER, DETECTED}`가 붙는다. 이 출처는 reason 문장에 쓰인다(예: "코드에서 `pg` 의존성을 감지해…").

### 2.2 Normalized Requirement (Engine 내부 모델)

정규화 상수는 `rules_version`에 묶인 **정책 상수**다. 실측으로 보정한다(§20 P-02).

| 상수 | 값(초기) | 근거 |
|---|---|---|
| 사용자당 일 요청 수 `RPU` | 50 | 일반적인 웹/API 세션 가정 |
| 대표값 비율 `REP` | 구간 상한 × 0.3 | 구간 하단 쪽으로 치우친 분포 가정 |
| 패턴 peak 계수 | IDLE 2 / BUSINESS 3 / STEADY 1.5 / SPIKY 10 | 평균 rps 대비 peak rps |
| 패턴 활성 시간 비율 | IDLE 0.1 / BUSINESS 0.35 / STEADY 1.0 / SPIKY 1.0 | Aurora auto-pause, 최소 용량 계산 |
| Lambda 평균 처리 시간·메모리 | 200ms · 512MB | platform.yaml로 override 가능 |
| Fargate task 처리량 | 0.25 vCPU/0.5GB 기준 50 rps | 보수적 초기값, 부하 테스트로 보정 |

파생 값:

```
users_upper      = bucket_upper(traffic_scale)            # 100 / 1,000 / 10,000
users_rep        = users_upper × REP
monthly_req_X    = users_X × RPU × 30                     # X ∈ {rep, upper}
peak_rps_X       = users_X × RPU / 86,400 × peak_factor(pattern)
peak_concurrency = ceil(peak_rps_upper × avg_duration_s)
```

```
Requirement {
  components:    [FRONTEND?, BACKEND?]             # app_form에서 결정
  backend_needs: { realtime, max_request_s, large_payload, stateful, non_http }
  load:          { users_rep, users_upper, monthly_req_rep, monthly_req_upper,
                   peak_rps_rep, peak_rps_upper, active_ratio }
  data:          { kind: NONE | KEY_VALUE | RELATIONAL, source: DETECTED|DECLARED|INFERRED|DEFAULTED }
  availability:  A0 | A1 | A2                       # OK | NOT_IN_BUSINESS_HOURS | NEVER
  cold_start_ok: bool                               # TOLERANT → true
  exposure, has_secrets, budget_cents, tier
}
```

---

## 3. 후보 공간 (Candidate Space)

후보 = **Frontend variant × Backend compute variant × DB variant**. Frontend는 Static으로 고정이고, Backend가 없으면 compute는 `NONE`이다.

| Compute variant | 내용 |
|---|---|
| `NONE` | backend 없음(STATIC_SITE) |
| `SLS` | Lambda(on-demand) + LWA + API Gateway HTTP API |
| `SLS_PC` | 위와 같고 Provisioned Concurrency = `peak_concurrency` |
| `CTR` | ECS Fargate + internal ALB + CloudFront VPC Origin |

| DB variant | 내용 |
|---|---|
| `NONE` | DB 없음 |
| `DDB` | DynamoDB on-demand (regional, 기본적으로 다중 AZ) |
| `RDS_SAZ` | RDS PostgreSQL 최소 class, Single-AZ |
| `RDS_MAZ` | RDS PostgreSQL 최소 class, Multi-AZ |
| `AUR_PAUSE` | Aurora Serverless v2, min 0 ACU(auto-pause), writer 1 |
| `AUR_MAZ` | Aurora Serverless v2, min 0.5 ACU, writer + reader(다른 AZ) |

후보 수는 최대 4 × 6 = 24개다. 전체를 열거해도 비용이 미미하고, **모든 탈락 이유를 기록할 수 있다**는 장점이 있다.

---

## 4. Blueprint별 지원 조건 / 제외 조건

### 4.1 지원 매트릭스

| 조건 | Static | SLS / SLS_PC | CTR |
|---|---|---|---|
| 담당 구성요소 | FRONTEND | BACKEND | BACKEND |
| 실시간 연결(WebSocket/SSE 장기 연결) | — | ✗ `EXC_SLS_REALTIME` | ✓ |
| 요청 시간: 몇 초 | — | ✓ | ✓ (origin timeout 30초) |
| 요청 시간: 30초 정도 / 30초~1분 | — | ✗ `EXC_SLS_TIMEOUT` (HTTP API 30초 hard limit, 여유 없음) | ✓ (origin timeout 60 / 90초, ALB idle +10초) |
| 요청 시간: 1~2분 | — | ✗ | ✓ (origin timeout **120초** = quota 증설 없는 최대, ALB idle 130초) |
| 요청 시간: 2분 넘게 | — | ✗ | ✗ → `UNSUP_REQUEST_OVER_120S` + 대안 `ALT_ASYNC_JOB`, `ALT_SPLIT_UNDER_2MIN` |
| 요청/응답 6MB 초과 | — | ✗ `EXC_SLS_PAYLOAD` (Lambda 동기 호출 6MB) | ✓ |
| 서버 메모리·로컬 디스크 상태 | — | ✗ `EXC_SLS_STATEFUL` (실행 환경 재사용이 보장되지 않음, `/tmp`는 요청 사이에 남는다는 보장이 없음) | ✓ (task가 2개 이상이면 ALB stickiness + `WARN_STICKY_STATE`) |
| HTTP 외 프로토콜 | — | ✗ | ✗ → `UNSUP_NON_HTTP` |
| `exposure = IP_ALLOWLIST` | ✓ (CloudFront + WAF IP set) | ✗ `EXC_SLS_IP_ALLOWLIST` (§4.2) | ✓ (CloudFront + WAF, ALB는 internal이라 우회 경로 없음) |
| secret 보유 | ✗ `CONF_STATIC_SECRETS` | ✓ | ✓ |
| 트래픽 T1~T3 | ✓ | ✓ | ✓ |
| 트래픽 T4 | ✓ (CDN) | ✗ → `UNSUP_TRAFFIC_SCALE` | ✗ → `UNSUP_TRAFFIC_SCALE` |
| cold start 불허 | ✓ | `SLS` ✗ `EXC_SLS_COLD_START` / `SLS_PC` ✓ | ✓ |

### 4.2 제외 조건의 근거

- **`EXC_SLS_IP_ALLOWLIST`**: API Gateway HTTP API는 WAF를 붙일 수 없다. 앞에 CloudFront + WAF를 두더라도 `execute-api` 기본 endpoint로 직접 들어오는 우회 경로가 남아서 "edge 외 inbound 0" baseline을 지킬 수 없다. 대안인 REST API resource policy를 쓰면 Serverless variant가 하나 더 생기므로 v1에서는 제외한다(§20 P-07).
- **Static은 T4도 지원한다.** CloudFront/S3는 사용자 수가 늘어도 구조가 바뀌지 않고 비용만 선형으로 증가하기 때문이다. T4가 지원 불가인 것은 backend가 있는 경우뿐이다.
- **EKS**: 후보 공간에 존재하지 않는다(원칙 7). 출력에는 정보성 note `INFO_EKS_OUT_OF_SCOPE`를 T3 Container 추천에만 붙인다.

---

## 5. DB 선택 규칙

Data kind는 아래 **우선순위 사다리**로 결정한다. 위에서 먼저 결정되면 아래 단계는 보지 않는다.

| 순위 | 조건 | 결과 | reason |
|---|---|---|---|
| 0 | `data.needed = false` | `NONE` | `SEL_DB_NONE` |
| 1 | `DETECTED_OTHER` (mysql, mongodb 등) | Gate 종료 | `UNSUP_DB_ENGINE` (근접안: 앱을 PostgreSQL로 바꾸면 지원 가능) |
| 2 | DETECTED와 DECLARED가 불일치 | Gate 종료 | `CONF_DB_HINT_MISMATCH` → NEEDS_INPUT |
| 3 | `DETECTED_POSTGRES` / `DECLARED_POSTGRES` | `RELATIONAL` | `SEL_DB_DETECTED` / `SEL_DB_DECLARED` |
| 4 | `DETECTED_DYNAMODB` / `DECLARED_DYNAMODB` | `KEY_VALUE` | 동일 |
| 5 | UNKNOWN + (`relational` 또는 `flexible_query`) | `RELATIONAL` | `SEL_DB_INFERRED_RELATIONAL` |
| 6 | UNKNOWN + `key_lookup_only`만 true | `KEY_VALUE` | `SEL_DB_INFERRED_KEY_VALUE` |
| 7 | UNKNOWN + 특성 답이 모두 false | `RELATIONAL` | `SEL_DB_DEFAULTED` + `WARN_DB_DEFAULTED` |

7번에서 RELATIONAL을 기본값으로 둔 근거: 기존 앱 코드는 대부분 관계형을 전제로 쓰여 있다. DynamoDB는 앱이 처음부터 그렇게 설계됐을 때만 동작하므로, 잘못 고르면 원칙 1(기능)이 깨진다. 반면 잘못된 RELATIONAL은 비용만 늘어난다.

DB variant 후보:

| Data kind | 열거되는 variant |
|---|---|
| `NONE` | `NONE` |
| `KEY_VALUE` | `DDB` |
| `RELATIONAL` | `RDS_SAZ`, `RDS_MAZ`, `AUR_PAUSE`, `AUR_MAZ` → §6, §7, §9 필터에서 하나로 좁혀짐 |

Compute와의 결합 규칙:

- `SLS*` + RDS/Aurora → **VPC Lambda**(private subnet) + 공유 Secrets Manager endpoint. 고정비 정당화 `FIX_ENDPOINT_FOR_VPC_LAMBDA`가 붙고 `WARN_NO_INTERNET_EGRESS`로 외부 API 호출 불가를 고지한다.
- `SLS*` + RDS/Aurora → Lambda **reserved concurrency 상한** = `min(bucket_cap, floor(db_max_connections × 0.5))`. connection 폭주를 막는 장치로, RDS Proxy의 고정비를 쓰지 않는다(`POL_DB_CONNECTION_GUARD`).
- `DDB`는 VPC가 필요 없다. VPC를 만들지 않는다.

---

## 6. Availability / Multi-AZ 결정 규칙

멈춤 허용도를 기술 목표로 바꾼다(등급과 독립, D-13).

| 수준 | 입력 | 의미 | 기술 목표 |
|---|---|---|---|
| A0 | 잠깐 멈춰도 됨 | 단일 AZ 장애 시 수십 분 중단 허용 | Single-AZ 허용 |
| A1 | 업무시간엔 안 됨 | 단일 AZ 장애에도 서비스 유지, 일시적 용량 감소는 허용 | 모든 stateful 구성요소 Multi-AZ |
| A2 | 절대 안 됨 | 단일 AZ 장애에도 **용량 손실 없이** 유지 | Multi-AZ + static stability(AZ 하나가 빠져도 peak 감당) |

구성요소별 규칙:

| 구성요소 | A0 | A1 | A2 |
|---|---|---|---|
| Static (S3+CloudFront) | 자체 다중 AZ `SEL_AZ_INHERENT` | 동일 | 동일 |
| SLS (Lambda, API GW) | 자체 다중 AZ `SEL_AZ_INHERENT` | 동일 | 동일 |
| CTR task | 1 task | AZ 2개에 최소 2 task | AZ 2개에 각각 peak 용량 (min = 2 × ceil(peak_rps_upper / 50)) `SEL_STATIC_STABILITY` |
| DDB | 자체 다중 AZ | 동일 | 동일 |
| RDS/Aurora | `RDS_SAZ`, `AUR_PAUSE` 허용 | `RDS_SAZ`, `AUR_PAUSE` 제외 `EXC_DB_SINGLE_AZ` | 동일 |

추가 규칙:

- A2는 Region 장애까지는 보장하지 않는다 → `WARN_REGION_SCOPE`(Multi-Region은 범위 외).
- dev 등급 + A1/A2 → 허용하되 `WARN_DEV_MULTI_AZ_COST`(D-13: AZ는 입력대로).
- "Multi-AZ가 추가 비용 없이 기본 제공되는가"가 Serverless·DDB가 비용 경쟁에서 유리해지는 핵심 이유다. 그래서 이 점을 reason 문장에 명시한다.

---

## 7. Performance 규칙 (cold start)

| 조건 | 규칙 | reason |
|---|---|---|
| `cold_start_ok = false` | `SLS` 제외, `SLS_PC` 유지 | `EXC_SLS_COLD_START` |
| `cold_start_ok = false` | `AUR_PAUSE` 제외(재개에 약 15초) | `EXC_AURORA_PAUSE_LATENCY` |
| `cold_start_ok = false` + `SPIKY` | `SLS_PC`의 PC 수 = `peak_concurrency`, `CTR` min = peak 용량 | `WARN_SPIKE_PREWARM_COST` |
| `cold_start_ok = true` + `SPIKY` | 추가 규칙 없음(Lambda가 burst를 흡수하고, CTR은 autoscaling) | — |

`SLS_PC`와 `CTR` 중 무엇을 고를지는 규칙으로 정하지 않고 **§9 비용 순위**로 정한다(D-23). 예를 들어 T1 + PC 1개(512MB)는 월 약 $5, Fargate task 1개는 월 약 $10에 ALB가 더해지므로 보통 `SLS_PC`가 이긴다. PC 수가 커지면 역전된다.

---

## 8. Scaling 결정 규칙

| 대상 | 규칙 | 근거 / reason |
|---|---|---|
| Static | 없음(관리형) | — |
| SLS | on-demand. reserved concurrency = `max(5, 2 × peak_concurrency)`, 플랫폼 상한으로 cap. DB가 있으면 §5 connection guard와 비교해 작은 값 | 비용 폭주와 DB 보호 `POL_LAMBDA_CONCURRENCY_CAP` |
| API Gateway | stage throttling: rate = `max(10, ceil(2 × peak_rps_upper))`(D-135), burst = `2 × rate` | 남용·loop 방어 `POL_THROTTLE` |
| CTR | ECS target tracking(CPU 60%). min = §6 표, max = `min(platform_cap, 2 × ceil(peak_rps_upper / 50))`, 단 min 이상 | `POL_CTR_AUTOSCALING` |
| CTR capacity | dev + A0 → **FARGATE_SPOT**, 그 외 → FARGATE | 중단 허용 + 개발 환경 `POL_SPOT_DEV_A0` |
| CTR 배포 | deployment circuit breaker + rollback 활성 | production-ready PR-CTR-05 |
| DDB | on-demand + **maximum throughput 상한** 설정 | 비용 폭주 방어 `POL_DDB_MAX_THROUGHPUT` |
| RDS | compute autoscaling 없음. class: T1~T2 → 최소 class, T3 → 한 단계 위. storage autoscaling 상한 = 할당량의 2배 | 최소 사양 원칙(D-51) |
| Aurora | `AUR_PAUSE` min 0 / max 2 ACU, `AUR_MAZ` min 0.5 / max 4 ACU | 플랫폼 리소스 상한 |

---

## 9. Cost 결정 규칙

### 9.1 비용 모델

```
cost(candidate, load_X) = Σ fixed_monthly(resource) + Σ variable(resource, load_X)
```

| 구성요소 | 고정비(월) | 변동비 |
|---|---|---|
| CloudFront | 0 | 요청 수, 전송량 |
| S3 | 0 | 저장량, 요청 수 |
| API Gateway HTTP | 0 | 요청 100만 건당 |
| Lambda | 0 (`SLS_PC`는 PC GB-초 고정비) | 요청 수, GB-초 |
| Fargate | task-시간 × min task | 확장된 task-시간(peak 비율로 추정) |
| internal ALB | 시간 요금(약 $16) | LCU |
| Secrets Manager endpoint | AZ 수 × 시간 요금(약 $7/AZ) | GB |
| RDS | instance-시간(Multi-AZ는 ×2), storage | — |
| Aurora SLv2 | (AUR_MAZ) 최소 ACU × 시간 × instance 수 | ACU-시간 × active_ratio, storage, I/O |
| DynamoDB | 0 | 요청 단위, 저장량 |
| WAF (IP_ALLOWLIST) | web ACL + rule | 요청 100만 건당 |
| Secret | secret 개수 × $0.4 | API 호출 |
| CloudWatch | prod: 알람 개수, dashboard | log 수집량, 보관량 |

### 9.2 두 개의 기준값 (D-24)

- **순위(§9.4)**: `load_rep`(대표값) 비용
- **예산 검사(§9.5)**: `load_upper`(상한값) 비용
- 승인 화면에는 `rep ~ upper` 범위를 고정비와 변동비로 나눠 표시한다.

### 9.3 Shared infra 비용 처리 — 결정성 유지

순위와 예산 검사 모두 **균등 분할 상수**로 공유 리소스 비용을 배분한다(P-04 승인).

```
allocated_shared_cost(resource) = monthly_fixed(resource) / SHARED_SPLIT_N
```

| 상수 | 초기값 | 근거 |
|---|---|---|
| `SHARED_SPLIT_N` | 4 | 플랫폼의 예상 동시 사용자 = 페르소나를 연기하는 선수 4명(CFP §2), 사용자당 해당 공유 리소스를 쓰는 환경 1개를 가정 |

- 결정성: 상수는 `rules_version`에 묶인다. 실제 공유 리소스가 있는지, 환경이 몇 개인지는 **보지 않으므로** 같은 입력이면 항상 같은 결과가 나온다.
- 왜 standalone이 아닌가: 공유 리소스 비용을 환경 하나가 전부 떠안는다고 계산하면, 공유로 설계된 리소스(ALB, endpoint)를 쓰는 후보가 구조적으로 과대평가된다.
- 왜 실제 증분이 아닌가: 플랫폼 상태에 따라 같은 입력의 추천이 달라져 결정성 계약이 깨진다.
- 실제 증분 비용(이미 있으면 0, 처음이면 전액)은 승인 화면에 `INFO_SHARED_MARGINAL_COST`로 **참고 표시**한다.
- 보정: 운영 데이터(공유 리소스별 평균 의존 환경 수)로 `SHARED_SPLIT_N`을 다시 정할 수 있다. 바꾸면 `rules_version`을 올린다.
- 플랫폼 전체 상한 검사(§9.5의 `BUD_PLATFORM_CAP`)에는 분할값이 아닌 **실제 공유 리소스 전액**을 쓴다. 계정의 실제 지출을 막는 장치이기 때문이다.

### 9.4 순위 규칙 (total order)

필터를 통과한 후보를 아래 key로 사전식 정렬하고 첫 번째를 선택한다.

1. `round_to_dollar(cost_rep)` 오름차순
2. 복잡도 순위 오름차순: compute `NONE 0 < SLS 1 < SLS_PC 2 < CTR 3`, db `NONE 0 < DDB 1 < RDS_SAZ 2 < AUR_PAUSE 3 < RDS_MAZ 4 < AUR_MAZ 5`(합산)
3. 고정비 합계 오름차순(원칙 6)
4. `candidate_id` 사전순(최종 tie-break, 항상 유일)

선택 reason: `SEL_LOWEST_COST`(2위와의 차액 포함). 1번이 같아 2번 이후로 결정되면 `SEL_TIEBREAK_SIMPLER`. 2위 이하 후보는 모두 `EXC_HIGHER_COST(+$Δ)`로 기록한다.

### 9.5 예산 검사와 완화안 (D-158, D-159 — D-25 대체)

> **변경(2026-10-04)**: 사용자 예산은 soft guard다. 초과 시 상태는 `OVER_BUDGET`이고, 사용자는 완화안을 적용하거나 확인 화면을 거쳐 **명시적으로 초과를 승인**(`budget_override`)할 수 있다. 플랫폼 전체 상한·quota·지원 불가·Security Baseline은 hard guard라 override로 우회할 수 없다. 아래 원문 중 "BLOCKED_BUDGET"은 `OVER_BUDGET`으로 읽는다. 완화안은 예산 안에 들어오는 안 중 변경 개수가 가장 적은 것(단일 → 2개 조합), 없으면 비용을 낮추는 안(최대 3개)을 보여주며, 사용자가 ‘이 변경 적용’을 누를 때만 반영한다.

```
if cost_upper(selected) > user_budget       → BLOCKED_BUDGET (BUD_OVER_USER_LIMIT)
if Σ platform 예상비 + cost_upper > 전체 상한 → BLOCKED_BUDGET (BUD_PLATFORM_CAP)
if 사용자 quota 초과                          → BLOCKED_BUDGET (BUD_QUOTA_EXCEEDED)
```

완화안 생성(결정적):

- 완화 가능한 입력은 **사용자의 선호에 해당하는 값만**이다: `downtime_tolerance` 한 단계 낮춤, `latency_sensitivity` → TOLERANT, `budget` 상향(필요 금액 제시).
- 트래픽 규모·패턴·데이터·기능은 **사실**이므로 완화 대상이 아니다. 공개 범위와 보안 항목도 완화 대상이 아니다.
- 완화 가능한 입력 하나씩만 바꿔(그 입력의 더 낮은 값 각각, D-136) 엔진을 다시 실행하고, 예산 안에 들어오는 결과만 `BUD_RELAX_OPTION`으로 제시한다. 비용 오름차순, 같으면 입력 필드 고정 순서로 정렬한다.
- `cost_upper`가 예산의 80% 이상이면 `WARN_NEAR_BUDGET`.

### 9.6 고정비 정당화 규칙

출력 architecture에 들어간 **모든 고정비 리소스에는 `FIX_*` reason이 하나 이상** 있어야 한다. 없으면 테스트에서 실패 처리한다(원칙 5·6의 기계적 보장).

| 리소스 | 정당화 reason |
|---|---|
| internal ALB | `FIX_ALB_FOR_CONTAINER` |
| RDS / Aurora | `FIX_RDB_FOR_RELATIONAL_DATA` |
| Multi-AZ 추가분 | `FIX_MULTI_AZ_FOR_AVAILABILITY` |
| Provisioned Concurrency | `FIX_PC_FOR_LATENCY` |
| Secrets Manager endpoint | `FIX_ENDPOINT_FOR_VPC_LAMBDA` |
| WAF | `FIX_WAF_FOR_IP_ALLOWLIST` |
| NAT Gateway | 사용하지 않음(D-44). 생성 시도는 정적 분석에서 차단 |

---

## 10. Security 강제 규칙

Security는 **순위 요소가 아니다.** 모든 후보는 baseline을 포함한 상태로 비용이 계산되고, baseline을 만족할 수 없는 후보는 §4에서 제외된다. Engine은 baseline 항목을 끄는 출력을 만들 수 없다.

| 규칙 | 내용 |
|---|---|
| SR-1 | 모든 출력에 [security-baseline.md](security-baseline.md)의 공통 항목 전체를 첨부(`SEC_BASELINE_APPLIED`) |
| SR-2 | Blueprint·DB·exposure별 조건부 항목을 결정해서 첨부(예: IP_ALLOWLIST → WAF, RDS → force_ssl·private subnet) |
| SR-3 | 완화안(§9.5)에는 보안 항목 변경이 절대 포함되지 않음 |
| SR-4 | Static + secret 입력 → `CONF_STATIC_SECRETS`로 NEEDS_INPUT(값을 받지 않음) |
| SR-5 | baseline을 만족할 수 없는 조합은 제외(`EXC_SLS_IP_ALLOWLIST` 등). 예외로 통과시키지 않음 |

---

## 11. Tier(dev/prod) 정책 규칙

등급은 **구조(Blueprint/DB/AZ)를 바꾸지 않고 정책만 바꾼다**(D-13, D-60). 단 하나의 예외는 `POL_SPOT_DEV_A0`(capacity provider)다.

| 정책 | DEV | PROD |
|---|---|---|
| 승인 | 사용자 본인 | Admin 승인 필요 `POL_TIER_PROD_APPROVAL` |
| TTL | 기본 7일, 연장 요청 가능 `POL_TTL_DEV` | 없음(quota로 관리) |
| 삭제 시 데이터 | 즉시 삭제 | deletion protection + 최종 snapshot |
| RDS backup 보관 | 1일 | 7일 |
| DynamoDB PITR | off | on |
| Log 보관 | 7일 | 30일 |
| Monitoring | log만 | 핵심 알람(5xx, latency, DB CPU/storage) + SNS + dashboard |
| Fargate Spot | A0이면 사용 | 사용 안 함 |

---

## 12. Shared Infrastructure 결정 규칙

출력의 `shared_dependencies[]`는 후보 구성에서 기계적으로 도출한다.

| 공유 리소스 | 필요 조건 | 고정비 |
|---|---|---|
| `ACCOUNT_BASELINE` (CloudTrail, log bucket, state bucket, 단가 snapshot) | 항상(플랫폼 bootstrap) | 거의 0 |
| `SHARED_VPC` (2 AZ, public + private subnet, NAT 없음, S3/DDB gateway endpoint, flow log) | CTR, RDS/Aurora, VPC Lambda 중 하나라도 있으면 | 0 |
| `SHARED_ECS_CLUSTER` | CTR | 0 |
| `SHARED_INTERNAL_ALB` (환경별 listener rule) | CTR | 있음 — §20 P-01 |
| `SHARED_SM_ENDPOINT` | SLS* + RDS/Aurora | 있음 |
| `SHARED_ECR` (환경별 repository) | SLS*, CTR | 저장량 |

공유 리소스는 첫 의존 환경이 생길 때 만들고, 의존 환경 수(refcount)가 0이 되면 정리한다.

---

## 13. Decision Tables (요약)

### 13.1 Backend compute (BACKEND가 있을 때)

| # | realtime | 요청 시간 | payload>6MB | stateful | exposure=IP | cold start 불허 | 남는 후보 | 최종 결정 |
|---|---|---|---|---|---|---|---|---|
| 1 | N | ≤30s | N | N | N | N | SLS, SLS_PC, CTR | 비용 순위(보통 SLS) |
| 2 | N | ≤30s | N | N | N | Y | SLS_PC, CTR | 비용 순위 |
| 3 | 하나라도 Y(realtime/30~60s/payload/stateful) | | | | | any | CTR | CTR |
| 4 | any | any | any | any | Y | any | CTR | CTR |
| 5 | — | >60s | — | — | — | — | 없음 | UNSUPPORTED |
| 6 | non_http=Y | | | | | | 없음 | UNSUPPORTED |

### 13.2 DB variant (RELATIONAL일 때)

| 가용성 | cold start 불허 | 남는 후보 | 최종 결정 |
|---|---|---|---|
| A0 | N | RDS_SAZ, AUR_PAUSE, RDS_MAZ, AUR_MAZ | 비용 순위 → 보통 RDS_SAZ. IDLE이면 AUR_PAUSE 가능 |
| A0 | Y | RDS_SAZ, RDS_MAZ, AUR_MAZ | 보통 RDS_SAZ |
| A1/A2 | any | RDS_MAZ, AUR_MAZ | 비용 순위 |

### 13.3 Result status

| 상태 | 조건 |
|---|---|
| `UNSUPPORTED` | Gate에서 `UNSUP_*` 발생 |
| `NEEDS_INPUT` | Gate에서 사용자가 해결해야 하는 `CONF_*` 발생 |
| `OVER_BUDGET` | 선택안의 상한 비용이 **사용자 예산**을 초과(soft, 완화 또는 명시적 승인 가능). 플랫폼 상한·quota는 admission에서 hard로 차단 |
| `RECOMMENDED_WITH_WARNINGS` | 추천했지만 `WARN_*`가 있음 |
| `RECOMMENDED` | 그 외 |

---

## 14. Reason Code 카탈로그

모든 code는 `code`, `params`, `message_ko`(비전문가용 문장 템플릿)를 갖는다.

**선택 (SEL)**
| code | 의미 |
|---|---|
| `SEL_STATIC_FRONTEND` | 화면 파일은 서버 없이 CDN으로 제공 |
| `SEL_LOWEST_COST` | 조건을 만족하는 후보 중 최저 비용(2위와 $Δ 차이) |
| `SEL_TIEBREAK_SIMPLER` | 비용이 같아 더 단순한 구성을 선택 |
| `SEL_DB_NONE` / `SEL_DB_DETECTED` / `SEL_DB_DECLARED` / `SEL_DB_INFERRED_RELATIONAL` / `SEL_DB_INFERRED_KEY_VALUE` / `SEL_DB_DEFAULTED` | DB 결정 경로 |
| `SEL_AZ_INHERENT` | 추가 비용 없이 기본적으로 다중 AZ |
| `SEL_AZ_SINGLE_ALLOWED` | 멈춤 허용 → Single-AZ |
| `SEL_AZ_MULTI_REQUIRED` | 멈춤 불허 → Multi-AZ |
| `SEL_STATIC_STABILITY` | AZ 하나가 빠져도 peak를 감당할 용량 |

**제외 (EXC)**
| code | 의미 |
|---|---|
| `EXC_STATIC_NEEDS_SERVER` | 서버 기능이 필요해 Static만으로 불가 |
| `EXC_SLS_REALTIME` / `EXC_SLS_TIMEOUT` / `EXC_SLS_PAYLOAD` / `EXC_SLS_STATEFUL` | Lambda/API GW 제약 |
| `EXC_SLS_IP_ALLOWLIST` | IP 제한 시 우회 경로 차단 불가 |
| `EXC_SLS_COLD_START` | on-demand Lambda의 cold start |
| `EXC_DB_SINGLE_AZ` | 가용성 요구 미달 |
| `EXC_AURORA_PAUSE_LATENCY` | auto-pause 재개 지연 |
| `EXC_DB_KIND_MISMATCH` | 데이터 종류와 맞지 않는 DB |
| `EXC_HIGHER_COST` | 조건은 만족하지만 더 비쌈(+$Δ) |

**고정비 정당화 (FIX)**: §9.6 표

**정책 (POL)**: `POL_TIER_DEV`, `POL_TIER_PROD_APPROVAL`, `POL_TTL_DEV`, `POL_SPOT_DEV_A0`, `POL_LAMBDA_CONCURRENCY_CAP`, `POL_DB_CONNECTION_GUARD`, `POL_THROTTLE`, `POL_CTR_AUTOSCALING`, `POL_DDB_MAX_THROUGHPUT`

**경고 (WARN)**: `WARN_DEV_MULTI_AZ_COST`, `WARN_STICKY_STATE`, `WARN_SPIKE_PREWARM_COST`, `WARN_REGION_SCOPE`, `WARN_DB_DEFAULTED`, `WARN_NEAR_BUDGET`, `WARN_NO_INTERNET_EGRESS`

**충돌 (CONF)**: `CONF_STATIC_WITH_DATA`, `CONF_STATIC_SECRETS`, `CONF_DB_HINT_MISMATCH`, `CONF_NO_CANDIDATE`

**지원 불가 (UNSUP)**: `UNSUP_TRAFFIC_SCALE`, `UNSUP_REQUEST_OVER_60S`, `UNSUP_DB_ENGINE`, `UNSUP_NON_HTTP`, `UNSUP_MULTI_REGION`, `UNSUP_CUSTOM_DOMAIN`

**예산 (BUD)**: `BUD_OVER_USER_LIMIT`, `BUD_PLATFORM_CAP`, `BUD_QUOTA_EXCEEDED`, `BUD_RELAX_OPTION`

**정보 (INFO)**: `INFO_EKS_OUT_OF_SCOPE`, `INFO_SHARED_MARGINAL_COST`

---

## 15. 후보 제외 이유 기록 방식

모든 후보는 출력의 `candidates[]`에 남는다.

```json
{ "candidate_id": "CTR+RDS_SAZ", "status": "EXCLUDED",
  "stage": "AVAILABILITY", "reasons": [{"code": "EXC_DB_SINGLE_AZ"}] }
{ "candidate_id": "SLS_PC+RDS_MAZ", "status": "RANKED", "rank": 2,
  "cost_rep_usd": 61, "reasons": [{"code": "EXC_HIGHER_COST", "params": {"delta_usd": 9}}] }
```

- `stage ∈ {FUNCTIONAL, SECURITY, AVAILABILITY, PERFORMANCE, COST}`. 어느 우선순위에서 탈락했는지 보여준다.
- 한 후보가 여러 단계에서 탈락할 수 있으면 **가장 앞 단계의 이유만** 기록한다(순서가 결정적이고, 설명도 짧아짐).
- UI는 "주요 탈락 후보"로 Blueprint별 최상위 후보 하나씩만 보여준다(예: "Serverless를 쓰지 않은 이유: 실시간 연결").

---

## 16. 충돌하는 요구사항 처리

| 충돌 | 처리 | 상태 |
|---|---|---|
| 화면만(STATIC_SITE) + 데이터 저장 필요 | 서버 없이는 저장 불가. "화면 + API"로 바꾸도록 안내 | NEEDS_INPUT `CONF_STATIC_WITH_DATA` |
| Static + secret | 값을 받지 않고 이유를 안내 | NEEDS_INPUT `CONF_STATIC_SECRETS` |
| 감지 DB ≠ 선언 DB | 사용자가 하나를 확정 | NEEDS_INPUT `CONF_DB_HINT_MISMATCH` |
| 예산 < 요구 충족 최저 비용 | 차단 + 완화안(§9.5) | BLOCKED_BUDGET |
| dev + 높은 가용성 | 허용 + 비용 경고 | WARN `WARN_DEV_MULTI_AZ_COST` |
| stateful + Multi-AZ | stickiness + 상태 유실 경고 | WARN `WARN_STICKY_STATE` |
| 필터 후 후보 0개(규칙상 이론적 상태) | 버그로 간주하고 테스트로 방지 | `CONF_NO_CANDIDATE` |

해소 원칙: **상위 원칙과 충돌하는 하위 요구를 몰래 낮추지 않는다.** 엔진이 직접 값을 바꾸는 일은 없다. 차단하고 선택지를 보여주거나, 허용하되 경고한다.

## 17. 지원할 수 없는 요구사항 처리

| 요구 | 근접안 | code |
|---|---|---|
| 하루 사용자 1만 명 초과(backend 있음) | T3 기준 구성(예상 한계 명시) | `UNSUP_TRAFFIC_SCALE` |
| 요청 2분 초과 (D-157) | "요청 접수 → 뒤에서 처리 → 결과 확인" 방식 안내(v1 Blueprint에 작업 큐 없음) + 2분 이하로 나눴을 때의 구성(근접안) | `UNSUP_REQUEST_OVER_120S`, `ALT_ASYNC_JOB`, `ALT_SPLIT_UNDER_2MIN` |
| MySQL·MongoDB 등 | 앱이 PostgreSQL/DynamoDB를 쓰면 지원 | `UNSUP_DB_ENGINE` |
| TCP/UDP 등 HTTP 외 프로토콜 | 없음 | `UNSUP_NON_HTTP` |
| Multi-Region, 사용자 도메인 | 단일 Region, 기본 도메인 | `UNSUP_MULTI_REGION`, `UNSUP_CUSTOM_DOMAIN` |

근접안은 **해당 입력을 지원 범위 경계로 바꿔 엔진을 다시 실행한 결과**다. 사용자가 명시적으로 확인해야만 진행된다(D-26).

---

## 18. 출력 스키마 (요약)

```json
{
  "status": "RECOMMENDED_WITH_WARNINGS",
  "rules_version": "1.0.0", "pricing_snapshot_id": "2026-10-01", "input_hash": "sha256:…",
  "architecture": {
    "frontend": "STATIC", "compute": "SLS", "db": "DDB",
    "multi_az": "INHERENT", "tier": "DEV",
    "scaling": { "lambda_reserved_concurrency": 10, "apigw_rate": 4, "apigw_burst": 8 },
    "shared_dependencies": ["ACCOUNT_BASELINE", "SHARED_ECR"]
  },
  "cost": { "rep_usd": 2, "upper_usd": 5, "fixed_usd": 0, "budget_usd": 20,
            "marginal_info_usd": 2 },
  "reasons": [ {"code": "SEL_LOWEST_COST", "params": {"runner_up": "CTR+DDB", "delta_usd": 24}} ],
  "candidates": [ ... ],
  "warnings": [ ... ],
  "policies": [ "POL_TIER_DEV", "POL_TTL_DEV", "POL_THROTTLE" ],
  "security_baseline": [ "SB-IAM-01", "...", "SB-LOG-05" ],
  "guardrails": { "ttl_days": 7, "budget_block": true, "resource_caps": {...} }
}
```

---

## 19. 대표 테스트 시나리오 (24개)

입력 표기: 앱형태 / 규모 / 패턴 / 데이터 / 멈춤 / 응답 / 공개 / 예산 / 등급. "엔진 예상"은 이 설계에서 나오는 결과이고, 정답 라벨은 코치·reviewer가 블라인드로 단다(D-115).

### 대표 (12)
| ID | 상황 | 입력 | 엔진 예상 |
|---|---|---|---|
| R01 | 개인 포트폴리오 | STATIC / T1 / IDLE / 없음 / OK / TOL / PUBLIC / $5 / DEV | Static |
| R02 | 사내 문서 사이트, 사무실 IP만 | STATIC / T2 / BUSINESS / 없음 / NOT_BH / TOL / IP / $30 / PROD | Static + WAF |
| R03 | 저트래픽 JSON API | SERVER / T1 / IDLE / 없음 / OK / TOL / PUBLIC / $10 / DEV | SLS |
| R04 | 설문 API, DynamoDB 감지 | SERVER / T2 / BUSINESS / DDB 감지 / OK / TOL / PUBLIC / $10 / DEV | SLS + DDB |
| R05 | SPA + API | STATIC+API / T2 / STEADY / DDB / NOT_BH / TOL / PUBLIC / $30 / PROD | Static + SLS + DDB |
| R06 | 이벤트 페이지 API | SERVER / T2 / SPIKY / 없음 / OK / TOL / PUBLIC / $20 / PROD | SLS |
| R07 | 응답 민감한 저트래픽 API | SERVER / T1 / IDLE / 없음 / OK / SENS / PUBLIC / $20 / PROD | SLS_PC (CTR보다 저렴) |
| R08 | 채팅(WebSocket) | SERVER(realtime) / T2 / STEADY / PG 감지 / NOT_BH / TOL / APP_AUTH / $120 / PROD | CTR(2 task) + RDS_MAZ |
| R09 | 파일 업로드 API | SERVER(payload) / T1 / BUSINESS / 없음 / OK / TOL / PUBLIC / $40 / DEV | CTR(Spot 1 task) |
| R10 | 30~60초 리포트 생성 | SERVER(30~60s) / T1 / BUSINESS / PG 감지 / OK / TOL / APP_AUTH / $60 / DEV | CTR + AUR_PAUSE (D-143) |
| R11 | 고객용 핵심 서비스 | STATIC+API / T3 / STEADY / PG 감지 / NEVER / SENS / PUBLIC / $300 / PROD | Static + SLS_PC + RDS_MAZ (D-143) |
| R12 | 사내 IP 제한 관리 도구 | SERVER / T1 / BUSINESS / PG 선언 / OK / TOL / IP / $60 / PROD | CTR + RDS_SAZ + WAF |

### 경계 (6)
| ID | 검증 대상 | 입력 핵심 | 확인할 것 |
|---|---|---|---|
| B01 | SLS↔CTR 비용 교차점 | SERVER / T3 / STEADY / 없음 / OK / TOL | 비용 순위 결과와 Δ가 reason에 기록되는지 |
| B02 | 요청 시간 경계 | UNDER_30S vs S30_TO_60S만 다른 두 입력 | SLS → CTR 전환 |
| B03 | 가용성 경계 | PG, OK vs NOT_BH | RDS_SAZ → RDS_MAZ |
| B04 | 예산 경계 | cost_upper = 예산, 예산 + $1, 예산 − $1 | 같으면 통과, 초과하면 차단 |
| B05 | SLS + RDS vs CTR + RDS | SERVER / T2 / BUSINESS / PG / OK / TOL / DEV | endpoint 고정비를 포함한 비교 |
| B06 | Aurora pause vs RDS 최소 | SERVER / T1 / IDLE / PG / OK / TOL | active_ratio에 따른 선택 |

### 충돌 (3)
| ID | 입력 | 기대 |
|---|---|---|
| X01 | STATIC_SITE + 데이터 필요 | NEEDS_INPUT `CONF_STATIC_WITH_DATA` |
| X02 | PG / NEVER / 예산 $15 | BLOCKED_BUDGET + 완화안(멈춤 허용도 하향, 필요 예산) |
| X03 | DDB 감지 + PG 선언 | NEEDS_INPUT `CONF_DB_HINT_MISMATCH` |

### 지원 불가 (3)
| ID | 입력 | 기대 |
|---|---|---|
| U01 | SERVER / T4 | UNSUPPORTED + T3 근접안 |
| U02 | 요청 2분 초과 | UNSUPPORTED + 원인 `request_duration` + 비동기 대안 + 근접안(1~2분 → Container) |
| U03 | MySQL 감지 | UNSUPPORTED `UNSUP_DB_ENGINE` |

### 결정성 테스트 (시나리오와 별도)
- 같은 입력을 100회 실행 → 출력 byte가 동일
- 입력 필드 순서와 cidr 목록 순서를 섞어도 출력이 동일
- 모든 출력의 모든 고정비 리소스에 `FIX_*` 존재
- 모든 `EXCLUDED` 후보에 stage와 reason 존재
- 완화안에 보안·공개 범위 변경이 포함되지 않음

---

## 20. 설계 제안과 승인 결과 (2026-10-04)

이 설계에서 새로 도입한 안과 그 승인 결과다.

| ID | 결정 | 상태 |
|---|---|---|
| P-01 | Container용 internal ALB를 **환경 간 공유**(환경별 listener rule, CloudFront origin custom header로 구분) | 승인 |
| P-02 | 정규화 상수(§2.2) 초기값, 2주 실측 후 보정 | 승인 |
| P-03 | backend가 있는 T4(하루 1만 명 초과)는 UNSUPPORTED + T3 근접안 | 승인 |
| P-04 | 공유 리소스 비용은 **균등 분할 상수** `SHARED_SPLIT_N`(초기 4)로 배분(§9.3). 제안했던 standalone 방식에서 변경 | 승인(변경) |
| P-05 | dev + A0 Container는 Fargate Spot | 승인 |
| P-06 | A1 = 모든 stateful 구성요소 Multi-AZ, A2 = 여기에 static stability 추가 | 승인 |
| P-07 | IP_ALLOWLIST이면 Serverless 제외 | 승인 |
| P-08 | DB 판단이 모호하면 PostgreSQL 기본 + 경고 | 승인 |
| P-09 | 완화안은 선호 입력(멈춤 허용도, 응답 민감도, 예산)만 대상 | 승인 |
| P-10 | 기본 도메인 사용에 따른 TLS 예외 2건(security-baseline §9) | 승인 |

> **생성 가능 범위(2026-10-05, D-165)**: 엔진 판단은 그대로이며, 생성 가능 여부(`provisionable`)만 `platform_core/blueprints.py`의 조합 `(frontend, compute, db)`으로 정한다: `(STATIC, NONE, NONE)` Static, `(NONE, SLS, NONE)` Serverless MVP. `in_memory_state` 입력은 로컬 디스크 상태까지 포함하도록 문구를 넓혔다(Lambda와 Fargate 모두 로컬 디스크는 영속되지 않지만, Lambda는 요청 사이에 실행 환경이 유지된다는 보장이 없음).
