# 개발자와 플랫폼의 경계, 그리고 기존 서비스와의 비교

2026-10-25 · 이진서 · 1차 발표 피드백(①②⑤) 조사 결과
**2026-11-05 개정**: Agent(MCP)와 Day-2를 구현하고 실제 AWS에서 측정한 뒤, 비교 대상에 **AI 에이전트 배포 도구(AWS ECS MCP Server)**와 **drift 감지(CloudFormation, AWS Config)**를 추가했다(피드백 ①④). 1주차 판단 중 틀린 것은 고쳐 적었다.

## 1. 질문

1. **경계**: 앱을 배포할 때 무엇을 개발자가 정하고, 무엇을 플랫폼(인프라 엔지니어)이 정해야 하는가? 특히 DB 종류는 추천할 대상인가?
2. **비교**: Elastic Beanstalk 같은 기존 서비스와 무엇이 다르며, 그 서비스들이 하지 않는 것은 무엇인가?
3. **(개정 추가)** "에이전트가 배포해 준다", "배포 후 drift를 잡는다"는 기존 도구에도 있지 않은가?

## 2. 경계: 업계는 이미 선을 그어 두었다

| 출처 | 개발자가 정하는 것 | 플랫폼이 정하는 것 |
|---|---|---|
| [Score](https://score.dev/) (CNCF sandbox) | `score.yaml`에 워크로드와 **필요한 자원**("Postgres가 필요하다")을 선언 | 그 선언을 실제 환경 설정(Compose, Kubernetes 등)으로 바꿈. "how to provision"은 플랫폼 몫 |
| [OAM](https://oam.dev/) | 앱을 인프라 세부 없이 "self-contained model"로 정의 | 재사용 가능한 플랫폼 기능(capability)을 만들어 제공 |
| [The Twelve-Factor App](https://12factor.net/ko/) | 설정은 환경변수로 받고, DB는 "연결된 자원(backing service)"으로 취급 | 그 자원을 붙이고 바꿈 (코드 수정 없이) |

### 결론: 우리 플랫폼에 적용하면

| 항목 | 누구 | 근거 | 현재 플랫폼 (2026-11-05) |
|---|---|---|---|
| DB **엔진** (PostgreSQL / MySQL / DynamoDB) | **개발자** | 코드(드라이버, ORM 설정)가 이미 정함. Score의 "필요한 자원 선언" | 저장소 분석기가 드라이버를 근거와 함께 감지하고 **개발자가 확인** (추천 대상 아님, D-183) |
| 포트, health 경로, 환경변수 이름 | 개발자 | 앱 코드의 일부 (12-factor) | 분석기가 코드에서 찾아 초안으로 채움 |
| compute 방식 (정적 / 요청 단위 / 상시 실행) | **플랫폼** | 요구사항과 비용에서 나오는 운영 결정 | 엔진이 추천하고 이유를 남김 |
| 네트워크, 보안 기준, 권한, 로그 | 플랫폼 | 조직 기준. 개발자가 끌 수 없어야 함 | 3단계 검사로 강제 + 배포 후 drift 감시 |
| 가용성(Multi-AZ), 예산 상한 | 함께 | 개발자가 요구를 말하고, 플랫폼이 구성과 상한을 정함 | 질문 + 상한 |
| DB **인스턴스 형태** (Aurora Serverless / RDS Multi-AZ) | 플랫폼 | 엔진이 정해진 뒤의 비용·가용성 결정 | 추천 (데이터 계층 생성은 아직 추천만) |

한 줄로: **"무엇이 필요한가"는 개발자가, "그것을 어떻게 만들고 지킬까"는 플랫폼이.**

## 3. 비교

### 3.1 배포 서비스: Beanstalk, ECS Express Mode

전제가 되는 사실:

- **AWS App Runner는 새 고객을 받지 않는다.** AWS는 대안으로 ECS Express Mode를 권장한다 ([App Runner availability change](https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html)).
- **ECS Express Mode**(2025-11)는 image와 IAM 역할 두 개로 Fargate 서비스, ALB, auto scaling을 API 한 번에 만든다 ([What's New](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode/)). **우리 Container Blueprint의 핵심(Fargate + 공유 ALB)은 이미 한 번의 호출로 된다.**
- **Elastic Beanstalk**은 플랫폼(언어 런타임)과 tier를 개발자가 고르고 ([concepts](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/concepts.html)), **managed platform updates**로 OS·런타임 패치를 정해진 시간에 immutable 방식으로 적용한다. 이 기능은 enhanced health로 앱이 정상인지 판단한다 ([managed platform updates](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/environment-platform-update-managed.html)).

| 비교 축 | Beanstalk | ECS Express Mode | 우리 플랫폼 |
|---|---|---|---|
| 실행 방식 선택 | 개발자가 플랫폼·tier 선택 | 컨테이너(Fargate) 하나 | 요구사항·저장소 분석으로 **정적 / 요청 단위 / 상시 실행** 중 추천, 이유 기록 |
| 안 쓸 때 비용 0 | Standard는 EC2 상시 실행 | Fargate 최소 task + ALB | 정적·요청 단위 실행은 0 |
| 만들기 **전** 정책 검사로 차단 | 확인하지 못함 | 확인하지 못함 | **PLAN 검사** (공개 S3·NAT·과한 권한을 apply 전에 차단) |
| 만든 **뒤** 보안 기준 통과해야 사용 | 상태(health) 확인 | 상태(health) 확인 | **STATE·HTTP 검사** 통과해야 READY |
| 예산 상한 강제 | 확인하지 못함 | 확인하지 못함 | 플랫폼 전체 상한, 사용자 quota |
| 배포 후 기준 유지 (drift) | 확인하지 못함 | 확인하지 못함 | **구현·실측** (§3.3) |
| OS·런타임 패치 자동 적용 | **있음** (managed platform updates) | Fargate 플랫폼이 관리 | **없음**. 대신 배포할 때 이미지 scan 결과를 한 번 읽고 CRITICAL이면 생성 실패(D-192). 운영 중 재검사는 하지 않음 |
| 개발자 도구 연동 | EB CLI | AWS CLI, ECS MCP Server (§3.2) | **구현**: MCP 서버 + Claude Code skill |
| 성숙도 | 오래 운영된 관리형 서비스 | AWS 관리형 | **학습 프로젝트, 계정 1개, DEV 위주** |

"확인하지 못함"은 공식 문서에서 기능을 확인하지 못했다는 뜻이며, 없다는 단정이 아니다.

### 3.2 AI 에이전트 배포 도구: AWS ECS MCP Server

1주차에는 "Claude Code에서 배포한다"를 차별점 후보로 적었다. **AWS가 이미 같은 형태의 도구를 낸 상태라, 이것만으로는 차별점이 아니다.**

[Amazon ECS MCP Server](https://awslabs.github.io/mcp/servers/ecs-mcp-server)는 AI 어시스턴트가 앱을 컨테이너로 만들고, ECR에 올리고, ECS Express Mode로 배포·삭제하게 해 준다. 쓰기 작업은 `ALLOW_WRITE=true`일 때만 열리고, 기본값은 false다.

| 비교 축 | AWS ECS MCP Server | 우리 Agent (D-181, D-185) |
|---|---|---|
| 에이전트가 쓰는 자격 증명 | **개발자의 로컬 AWS 자격 증명** (최소 권한 IAM 역할 권장) | **AWS 자격 증명 없음.** 플랫폼 API 토큰만 |
| 쓰기 권한 통제 | `ALLOW_WRITE` 스위치 하나. 켜면 생성·삭제 가능 | 서버가 강제: 에이전트 토큰은 초안·조회·재검사만, **승인·삭제·재시도·복구·연장·예산 초과는 403** |
| 사람 승인 | 공식 페이지에 승인 단계 설명 없음 | 실제 AWS 변경은 **사람이 웹 화면에서 클릭**해야만 |
| 실행 방식 | ECS(Fargate) | 정적 / Lambda / Fargate 중 추천 |
| 배포 전후 정책 검사 | 공식 페이지에서 확인하지 못함 | PLAN·STATE·HTTP 검사 |
| 진단 | 서비스 이벤트, task 실패, 로그 조회 | 실패 원인 설명(`explain_failure`), drift 설명 |
| 자체 평가 | "production 배포에 적합하지 않음"(AWS 공식) | 학습 프로젝트. 분석기 정확도를 held-out으로 공개 |

**차이의 핵심은 "누가 AWS를 바꾸는가"다.** ECS MCP Server에서는 에이전트가 개발자의 권한으로 AWS를 직접 바꾼다. 우리 Agent는 구조적으로 AWS를 바꿀 수 없고, 바꾸는 것은 플랫폼(검사 통과 시)이며, 그 시작은 사람의 클릭이다. 2차 Day-2 E2E(2026-11-04)에서 에이전트 토큰의 repair/approve/extend가 실제 AWS에서 403인 것을 확인했다.

### 3.3 배포 후 drift: CloudFormation drift detection, AWS Config

"배포 후 설정이 바뀐 것을 잡는다"도 AWS에 이미 있다.

- **CloudFormation drift detection**: 스택 리소스의 실제 설정이 템플릿과 다른지 비교한다 ([stack drift](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-stack-drift.html)).
- **AWS Config 규칙 `cloudformation-stack-drift-detection-check`**: 설정 변경 시와 주기적으로 위 검사를 돌려 비준수를 표시한다 ([rule](https://docs.aws.amazon.com/config/latest/developerguide/cloudformation-stack-drift-detection-check.html)). 고치는 동작은 별도의 remediation(SSM Automation 문서)을 붙여야 한다. 조사한 사례들은 `AWS-PublishSNSNotification`으로 **알림**을 보내는 구성이었고, drift를 자동으로 되돌리는 AWS 기본 runbook은 찾지 못했다(없다는 단정은 아님).

| 비교 축 | CloudFormation drift / AWS Config | 우리 Day-2 (D-186, D-188) |
|---|---|---|
| 무엇과 비교하나 | 리소스 속성 vs 템플릿 | **승인 당시 통과한 보안 기준·실제 HTTP 응답** vs 지금 (이미 실패하던 항목 제외) |
| 사이트가 실제로 열리는지 | 보지 않음 | HTTP 검사 (HTTPS 리다이렉트, 보안 헤더, 배포한 버전이 보이는지) |
| 감지 경로 | 설정 변경 이벤트 + 주기 | CloudTrail 쓰기 이벤트 → EventBridge (초 단위) + 매시 sweep (전역 서비스) |
| 감지 후 | 비준수 표시. 복구는 직접 구성한 SSM 문서 | 보안 drift면 **즉시 접속 차단** → 승인된 구성으로의 plan 자동 준비·PLAN 검사 → **사람 승인** → 저장된 그 plan만 적용 → 재검증 → READY |
| 누가 보나 | 인프라 담당자 (Config 콘솔) | 개발자 화면·에이전트가 누가·무엇을·언제 바꿨는지와 할 일을 설명 |
| 실측 | – | 콘솔 변경 → 감지 **9초·11초**, 승인 → 복구 완료 **3분 47초·3분 30초** (E2E 2회) |

정직한 한계: Config는 계정의 수백 종 리소스에 쓸 수 있는 범용 도구이고, 우리 Day-2는 플랫폼이 만든 환경의 **정해진 검사 항목**만 본다. 우리 것이 Config를 대체하는 것이 아니라, **"개발자의 환경이 승인 당시의 안전한 상태로 계속 있는가"라는 좁은 질문에 끝까지(차단·복구·재검증) 답한다**는 점이 다르다.

## 4. 그래서 차별점은 어디에 두어야 하나

1주차 결론(고르기·지키기·이어 주기)은 유지하되, 근거를 실측으로 바꾸고 과장을 걷어낸다.

| 차별점 | 기존 도구에 있는 것 | 우리만의 부분 | 근거 | 한계 |
|---|---|---|---|---|
| **고르기** | 개발자가 실행 방식을 고름 | 저장소·요구사항에서 정적/요청 단위/상시 실행 중 고르고 이유를 남김. DB 엔진은 감지 후 확인 | 분석기 held-out 3세트 측정: v3 → 세트 3 결과 일치 67%(확인 후 83%) | v3 규칙의 효과는 처음 보는 앱에서 미입증(D-191). 조용한 데이터 손실 유형이 남음 |
| **지키기** | 배포 후 health, 템플릿 drift | 만들기 전(PLAN)·만든 뒤(STATE·HTTP, 이미지 CRITICAL 취약점 포함)·운영 중(Day-2)에 **같은 기준**, 위반 시 차단, 복구도 사람 승인 + 재검증. 전체 환경 현황판(D-194) | E2E에서 감지 9~11초, 복구 3분 30초~47초, 차단 중 중복 drift 없음 | OS·런타임 패치 없음, 이미지 취약점은 배포 때 한 번만(운영 중 새 CVE는 못 잡음) |
| **이어 주기** | 에이전트가 개발자 권한으로 직접 배포 (ECS MCP Server) | 에이전트는 AWS 권한이 없고 초안만. AWS 변경은 사람 클릭 → 플랫폼 검사 → 생성 | 에이전트 토큰 403 실측, 에이전트가 만든 초안으로 2차 E2E | 다른 개발자 배포는 현재 저장소 clone 필요 (플러그인화 미완) |

**발표용 한 문장**: 기존 도구는 "고른 것을 만들어 주거나(Beanstalk·Express Mode)", "에이전트에게 AWS 권한을 주거나(ECS MCP)", "바뀐 걸 알려 준다(Config)". 이 플랫폼은 **에이전트에게 권한을 주지 않은 채, 고르고, 사람 승인 뒤에 만들고, 승인 당시의 안전한 상태를 운영 중에도 지킨다.**

## 5. 이 조사로 바꾼 것

| 결정 | 내용 |
|---|---|
| D-182 | Blueprint 이름을 실행 방식 기준으로: 정적 호스팅 / 요청 단위 실행 / 상시 실행 컨테이너 |
| D-183 | 베이스라인 공개 앱과 저장소 분석기. DB 엔진은 코드에서 감지하고 개발자가 확인 |
| D-181·D-185 | 에이전트에게 AWS 자격 증명을 주지 않고, 승인류 API는 서버에서 403 (§3.2의 ECS MCP Server와 갈리는 지점) |
| D-186·D-188 | Day-2 drift 감지·차단·사람 승인 복구, 실제 AWS E2E 2회 |
| D-192·D-194 | 이미지 취약점은 배포 때 한 번(CRITICAL이면 실패), 운영 중 재검사는 범위 제외. 전체 환경 현황판 구현 |
| 남은 판단 | ECS Express Mode를 Container Blueprint의 구현 대안으로 쓸지 |
