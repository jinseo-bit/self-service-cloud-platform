# 개발자와 플랫폼의 경계, 그리고 기존 서비스와의 비교

2026-10-25 · 이진서 · 1차 발표 피드백(①②⑤) 조사 결과

## 1. 질문

1. **경계**: 앱을 배포할 때 무엇을 개발자가 정하고, 무엇을 플랫폼(인프라 엔지니어)이 정해야 하는가? 특히 DB 종류는 추천할 대상인가?
2. **비교**: Elastic Beanstalk 같은 기존 서비스와 무엇이 다르며, 그 서비스들이 하지 않는 것은 무엇인가?

## 2. 경계: 업계는 이미 선을 그어 두었다

| 출처 | 개발자가 정하는 것 | 플랫폼이 정하는 것 |
|---|---|---|
| [Score](https://score.dev/) (CNCF sandbox) | `score.yaml`에 워크로드와 **필요한 자원**("Postgres가 필요하다")을 선언 | 그 선언을 실제 환경 설정(Compose, Kubernetes 등)으로 바꿈. "how to provision"은 플랫폼 몫 |
| [OAM](https://oam.dev/) | 앱을 인프라 세부 없이 "self-contained model"로 정의 | 재사용 가능한 플랫폼 기능(capability)을 만들어 제공 |
| [The Twelve-Factor App](https://12factor.net/ko/) | 설정은 환경변수로 받고, DB는 "연결된 자원(backing service)"으로 취급 | 그 자원을 붙이고 바꿈 (코드 수정 없이) |

### 결론: 우리 플랫폼에 적용하면

| 항목 | 누구 | 근거 | 현재 플랫폼 | 바꿀 점 |
|---|---|---|---|---|
| DB **엔진** (PostgreSQL / MySQL / DynamoDB) | **개발자** | 코드(드라이버, ORM 설정)가 이미 정함. Score의 "필요한 자원 선언" | 모르면 데이터 특성 질문으로 **추천** | 추천 대신 **코드 감지 + 개발자 확인**. 저장소 분석기가 드라이버를 근거와 함께 보여 줌 |
| 포트, health 경로, 환경변수 이름 | 개발자 | 앱 코드의 일부 (12-factor) | 입력받음 | 분석기가 코드에서 찾아 초안으로 채움 |
| compute 방식 (정적 / 요청 단위 / 상시 실행) | **플랫폼** | 요구사항과 비용에서 나오는 운영 결정 | 엔진이 추천 | 유지 |
| 네트워크, 보안 기준, 권한, 로그 | 플랫폼 | 조직 기준. 개발자가 끌 수 없어야 함 | 3단계 검사로 강제 | 유지 |
| 가용성(Multi-AZ), 예산 상한 | 함께 | 개발자가 요구를 말하고, 플랫폼이 구성과 상한을 정함 | 질문 + 상한 | 유지 |
| DB **인스턴스 형태** (Aurora Serverless / RDS Multi-AZ) | 플랫폼 | 엔진이 정해진 뒤의 비용·가용성 결정 | 추천 | 유지 |

한 줄로: **"무엇이 필요한가"는 개발자가, "그것을 어떻게 만들고 지킬까"는 플랫폼이.** DB 엔진은 앞쪽, DB 인스턴스 형태는 뒤쪽이다.

## 3. 비교: 기존 서비스는 어디까지 하는가

조사 중 확인한 사실 두 가지가 비교의 전제를 바꾼다.

- **AWS App Runner는 새 고객을 받지 않는다.** AWS는 대안으로 ECS Express Mode를 권장한다 ([AWS App Runner availability change](https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html)).
- **ECS Express Mode**(2025-11 출시)는 container image와 IAM 역할 두 개만 주면, API 한 번으로 Fargate 서비스, ALB, auto scaling, 네트워크를 만든다. ALB는 같은 서브넷의 서비스끼리 공유할 수 있다 ([What's New](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode/), [만들어지는 리소스](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-work.html)).
- Elastic Beanstalk에는 EC2에서 바로 실행하는 **Standard** 모드와, EKS 위에서 컨테이너로 실행하는 **Cluster** 모드가 있다. 개발자는 플랫폼(언어 런타임)과 환경 tier(web / worker / cluster)를 고른다 ([Beanstalk concepts](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/concepts.html)).

**우리 Container Blueprint의 핵심(Fargate + 공유 ALB)은 ECS Express Mode가 이미 한 번의 호출로 한다.** "컨테이너를 쉽게 띄워 준다"는 것만으로는 차별점이 될 수 없다. 강사님 피드백 ④가 정확히 이 지점이다.

| 비교 축 | Beanstalk | ECS Express Mode | 우리 플랫폼 |
|---|---|---|---|
| 실행 방식 선택 | 개발자가 플랫폼·tier 선택 | 컨테이너(Fargate) 하나 | **요구사항·저장소 분석으로 정적 / 요청 단위 / 상시 실행 중 추천**하고 이유를 남김 |
| 안 쓸 때 비용 0 | Standard는 EC2 상시 실행 | Fargate 최소 task + ALB | 정적·요청 단위 실행은 0 (Lambda) |
| 입력 | 코드 묶음 또는 image | image + IAM 역할 2개 | 저장소 / 빌드 결과 / image + 요구사항 |
| 만들기 **전** 정책 검사로 차단 | 확인하지 못함 | 확인하지 못함 | **PLAN 검사** (NAT·공개 S3·과한 권한 등을 apply 전에 차단) |
| 만든 **뒤** 보안 기준 검증 후 사용 허가 | 상태(health) 확인 | 상태(health) 확인 | **STATE·HTTP 검사**를 통과해야만 READY |
| 예산 상한 강제 | 확인하지 못함 | 확인하지 못함 | 플랫폼 전체 상한, 사용자 quota |
| 배포 후 기준 유지 확인 (drift) | 확인하지 못함 | 확인하지 못함 | 계획 (Day-2) |
| 개발자 도구 연동 | EB CLI | AWS CLI, GitHub Action | 계획 (MCP + Claude Code skill) |
| 성숙도 | 오래 운영된 관리형 서비스 | AWS 관리형 | **학습 프로젝트, 계정 1개, DEV 위주** |

"확인하지 못함"은 이번 조사에서 공식 문서로 기능을 확인하지 못했다는 뜻이며, 없다는 단정이 아니다.

## 4. 그래서 차별점은 어디에 두어야 하나

기존 서비스는 모두 **"개발자가 고른 실행 방식을 잘 만들어 주는 것"** 에 강하다. 우리 쪽에 남는 영역은 세 가지다.

1. **고르기**: 저장소와 요구사항에서 실행 방식을 고르고, 이유를 남긴다. 저장소 분석기 v1이 공개 앱 13개 중 12개를 SA 판단과 같게 골랐다 ([eval-results.md](../baseline/eval-results.md), 개발 세트 기준).
2. **지키기**: 만들기 전, 만든 뒤, 운영 중에 같은 보안·비용 기준을 계속 확인한다 (PLAN·STATE·HTTP → Day-2).
3. **이어 주기**: 개발자의 도구(Claude Code)에서 시작해 사람 승인을 거쳐 배포하고, 운영 중 문제를 다시 개발자에게 설명한다 (MCP agent).

## 5. 이 조사로 바꾸는 것

| 결정 | 내용 |
|---|---|
| D-182 | Blueprint 이름을 실행 방식 기준으로: 정적 호스팅 / 요청 단위 실행 / 상시 실행 컨테이너 |
| D-183 | 베이스라인 공개 앱 13개와 저장소 분석기 v1. DB 엔진은 코드에서 감지하고 개발자가 확인 (추천 대상 아님) |
| 후속 | DB 질문을 "추천"에서 "감지 + 확인"으로 바꾸는 UI 변경, ECS Express Mode를 Container Blueprint의 구현 대안으로 검토 |
