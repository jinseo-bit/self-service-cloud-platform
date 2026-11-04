# Engineering Log — 실제 AWS에서 배운 것

> 초안 (B-3, 2026-11-05). 이 프로젝트에서 실제 AWS를 돌릴 때마다 무엇이 깨졌고, 왜 로컬 테스트가 그것을 못 잡았으며, 무엇을 남겨 재발을 막았는지를 시간순으로 적는다. 결정 번호(D-xxx)는 [project-decisions.md](project-decisions.md)에 있다.

## 한눈에 보기

| 날짜 | 무엇을 돌렸나 | 깨진 것 | 근본 원인 | 남긴 것 |
|---|---|---|---|---|
| 2026-10-05 | 플랫폼 첫 apply + Static E2E | 업로드 "Failed to fetch" | presigned POST가 전역 S3 endpoint를 가리킴 → 307 → 브라우저 CORS 실패 | 리전 virtual-hosted endpoint 고정 + 테스트 ([postmortem](postmortem-failed-to-fetch.md)) |
| 2026-10-07 | Serverless E2E (env-26de63db) | lock 파일, runner 권한 3종, 실패 UX | `zh:` 해시 없음, log delivery·`apigateway:TagResource` 권한 누락 | 다중 플랫폼 lock, runner 권한, `TERRAFORM_PREPARE` 단계 |
| 2026-10-07 | Container 첫 E2E (env-74469f09) | apply 대기 중 AccessDenied | `ecs:ListServiceDeployments` 등 배포 추적 권한 누락 | D-176, 권한 테스트 |
| 2026-10-08 | Container E2E (env-af602604) | VALIDATE 첫 호출 AccessDenied | `ecs:ListTasks`의 resource type(`container-instance`)을 "불필요"하다고 지움 | D-177, `test_worker_iam_map.py` 호출 지도 |
| 2026-10-08 | Container E2E (env-1ee973e8) | 정상 구성을 위반으로 판정, 접속 차단 | "route table 수 = 서브넷 수" 가정 (오탐) | D-179, 실제 AWS 형태의 응답으로 테스트 교체 |
| 2026-10-28 | Day-2 첫 E2E (env-c9fc390e) | 복구 클릭 "Failed to fetch", drift 중복 기록, 복구 시간 과대 | API Gateway 라우트 누락 / 차단 효과를 drift로 판정 / 승인 대기 포함 | D-188 ①②③, 라우트 동기화 테스트, 시뮬레이터 차단 |
| 2026-11-04 | Day-2 2차 E2E (env-9183234c, 에이전트 생성) | 없음 | — | D-188 수정 실측 확인 |

## 반복된 패턴 세 가지

### 1. "로컬은 AWS의 경계를 흉내만 낸다"

깨진 것의 절반이 **로컬 대체물과 실제 AWS의 차이**에서 나왔다.

- 업로드: 로컬 서버는 같은 origin(`/local/upload/<env>`)으로 받으므로 CORS도, 307도 일어나지 않는다.
- 라우트: 로컬 서버는 Lambda의 라우트 표를 그대로 쓰지만, 실제 요청은 API Gateway 라우트 목록을 먼저 지난다.
- 차단: 로컬 시뮬레이터의 "접속 차단"은 메시지만 남기고 실제로 막지 않아서, 차단 뒤의 재검사가 계속 통과했다.

**대응**: 대체물을 없애는 대신, 대체물이 **AWS와 다른 지점을 테스트로 고정**했다(endpoint 형태, `api.tf` ↔ `ROUTES` 동기화, 차단 시 CDN·HTTP 검사 실패). 그리고 수정을 되돌리면 테스트가 AWS와 같은 증상으로 실패하는지까지 확인했다(D-188, `event_name` None).

### 2. IAM은 "줄이는 순간" 깨진다

Container에서 두 번 연속 AccessDenied가 났다. 둘 다 최소 권한으로 줄이는 과정에서 **AWS가 실제로 평가하는 resource type**을 추측으로 지운 것이 원인이었다.

**대응**: worker가 호출하는 모든 boto3 호출을 (IAM action, IAM이 평가하는 ARN)으로 고정한 호출 지도(`tests/test_worker_iam_map.py`)를 만들었다. 소스에 지도에 없는 호출이 생기면 테스트가 실패한다. 권한을 줄이려면 먼저 이 지도의 근거([Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html))를 바꿔야 한다.

### 3. 검사기의 오탐은 사용자 사이트를 내린다

보안 FAIL이면 접속을 즉시 차단한다(D-65, D-139). 그래서 검사기의 오탐은 단순한 경고가 아니라 **장애**다. env-1ee973e8은 정상 구성인데 차단됐다.

**대응**: 테스트용 AWS 응답을 손으로 만든 이상적인 형태가 아니라 **실제 E2E에서 관찰한 형태**(table 1개 + 연결 2개)로 바꿨다. B-1(로그 버킷 versioning, D-189)에서도 같은 위험을 미리 막았다. 새 보안 검사가 versioning 설정보다 먼저 배포되면 모든 Static 환경이 보안 drift로 차단될 수 있어서, Terraform `depends_on`으로 배포 순서를 보장했다.

## 실측값 (README에도 반영)

| 경로 | 측정 | 값 | 출처 |
|---|---|---|---|
| Static | 생성 (승인 → READY) | 3분 58초 | 2026-10-05 E2E |
| Static | 재배포 | 9초 | 2026-10-05 E2E |
| Static | 되돌리기 (rollback) | 5초 | 2026-10-05 E2E |
| Static | 삭제 | 4분 5초 | 2026-10-05 E2E |
| Serverless | 생성 (승인 → READY) | 1분 43초 | env-26de63db |
| Serverless | cold start Init | 129ms | env-26de63db |
| Day-2 | 변경 → 감지 | 9초 / 11초 | 1차 / 2차 E2E |
| Day-2 | 승인 → 복구 완료 | 3분 47초 / 3분 30초 | 1차 / 2차 E2E |

## 확인이 필요한 항목

- **plan 리뷰에서 CloudFront 권한을 줄인 건 (B-3 원래 범위)**: 결정 기록에 세부 내용이 남아 있지 않다. 당시 plan 출력이나 커밋 메모를 찾으면 이 표에 한 줄로 추가한다.
- Container의 warm 요청 시간은 아직 실측하지 않았다.
