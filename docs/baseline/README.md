# 베이스라인 공개 앱과 저장소 분석기 평가

강사님 피드백("베이스라인으로 쓸 공개 웹 앱 조사")과 Agent의 저장소 분석기(D-183) 평가를 한곳에 모은 문서다.

| 파일 | 내용 |
|---|---|
| [apps.json](apps.json) | 개발 세트 13개. commit 고정, 근거 파일, SA 기대 결과 |
| [heldout.json](heldout.json) | held-out 세트 5개. 분석기를 돌리기 **전에** 고르고 정답을 씀 (D-184) |
| [heldout3.json](heldout3.json) | held-out 세트 3, 6개. 분석기 v3를 만들기 **전에** 고정 (D-190) |
| [heldout3-results.md](heldout3-results.md) | v3를 세트 3으로 한 번 잰 결과 (D-191) |
| [eval-results.md](eval-results.md) / [heldout-results.md](heldout-results.md) | `python scripts/eval_analyzer.py [--heldout] --write` 결과 |

## 1. 결과: 개발 세트와 held-out 세트

| 측정 | 개발 세트 (13, 규칙을 보며 고침) | **held-out (5, 처음 봄)** |
|---|---|---|
| 항목 정확도 | 98% | **84%** |
| SA와 결과 일치, 초안 그대로 | 92% (12/13) | **20% (1/5)** |
| SA와 결과 일치, 개발자 확인 후 | 100% | **60% (3/5)** |

**v1 규칙은 처음 보는 앱에 잘 일반화되지 않는다.** 개발 세트 92%는 규칙을 그 앱들에 맞춘 결과였다. 공정한 숫자는 held-out의 20% / 60%다.

## 2. held-out 실패 원인 (규칙은 고치지 않고 기록만)

| 앱 | 틀린 것 | 원인 | 일반화된 교훈 |
|---|---|---|---|
| memos | 실시간 연결을 있다고 판정 | `go.mod`의 `gorilla/websocket // indirect`를 직접 의존성으로 셈 | 간접 의존성(`// indirect`)은 앱이 쓰는 기능이 아니다 |
| memos | 포트 변수 못 찾음 (MEMOS_PORT) | viper `SetEnvPrefix("memos")` + 플래그 이름을 환경변수로 바꾸는 방식을 모름 | 설정 라이브러리(viper, envconfig 등)의 이름 변환을 읽어야 한다 |
| thelounge | 로컬 저장 없음으로 판정 → **그대로 만들 수 있다고 추천** | 저장소가 `node:sqlite`(Node 내장)와 설정 파일 홈 디렉터리라 의존성에 안 보임 | **"저장 근거 없음"은 "저장 안 함"이 아니다.** 근거가 약하면 확인을 요청해야 한다 |
| httpbin (Python) | 포트를 못 바꾸는 앱을 **만들 수 있다고 추천** | Dockerfile `CMD gunicorn -b 0.0.0.0:80`처럼 CMD에 박힌 포트를 안 읽음 | 실행 인자로 고정된 포트는 Go 플래그만이 아니라 Dockerfile CMD에도 있다 |
| vaultwarden | 실시간 연결 놓침, 포트 변수를 MARIADB_PORT로 착각 | Rust 규칙이 없음, `playwright/` 테스트 코드의 변수를 앱 설정으로 읽음 | 언어별 규칙 부족 / 테스트·도구 폴더는 앱이 아니다 |

### 위험한 오류와 덜 위험한 오류

| 종류 | 예 | 실제로 생기는 일 |
|---|---|---|
| **조용한 데이터 손실** | thelounge (저장소가 필요한데 상시 실행 컨테이너로 생성 가능 판정) | 만들어지고 검사도 통과한다. 그런데 컨테이너가 재시작되면 사용자·로그가 사라진다. **플랫폼 검증이 잡지 못한다** |
| 생성 후 실패 | httpbin (포트 80 고정) | 사람이 승인해도 상태 확인에서 FAILED로 끝난다. 비용은 조금 들지만 잘못된 환경이 READY가 되지는 않는다 |
| 확인 요청으로 걸러짐 | memos, vaultwarden의 DB | 개발자가 확인 단계에서 고치면 맞는 결과(지원 불가)가 나온다 |

가장 중요한 교훈은 첫 줄이다. 분석기가 틀렸을 때 **사람 승인과 3단계 검증이 대부분을 막아 주지만, "상태를 어디에 저장하는가"는 검증으로도 못 잡는다.** 그래서 v2는 이 항목을 근거가 없을 때도 개발자에게 묻는 방향으로 바꾼다.

## 3. v2 규칙 계획과 평가 방법

| 우선순위 | 규칙 | 근거 |
|---|---|---|
| 1 | 상태 저장 근거가 약하면(서버 앱인데 DB 드라이버도, 저장 근거도 없음) **항상 확인 요청** | thelounge의 조용한 데이터 손실 |
| 2 | Dockerfile `CMD`/`ENTRYPOINT`에 고정된 bind 주소·포트 감지 → 계약 불일치 | httpbin |
| 3 | Go `// indirect` 의존성 제외 | memos |
| 4 | 테스트·도구 폴더(`playwright/`, `scripts/`, `e2e/`)를 포트·설정 탐색에서 제외 | vaultwarden |
| 5 | viper 등 설정 라이브러리의 환경변수 이름 변환 | memos |
| 6 | Rust(Cargo) 규칙: axum/actix/rocket, tokio-tungstenite, diesel/sqlx | vaultwarden |

**평가 방법**: v2를 이 5개로 다시 재면 또 개발 세트가 된다. v2 성능은 **새 held-out 5개 이상**으로 잰다. 앱과 정답은 v2를 돌리기 전에 정한다. 이 순서를 매번 지킨다.

## 4. v2 결과 (2026-10-27)

v2 규칙을 먼저 확정한 뒤 held-out 세트 2([heldout2.json](heldout2.json), 5개)의 앱과 정답을 정했고, 그다음에 v2를 처음 돌렸다.

| 측정 | v1 → held-out 1 | **v2 → held-out 2** | (참고) v2 → held-out 1 |
|---|---|---|---|
| 항목 정확도 | 84% | **88%** | 92% |
| SA와 결과 일치, 초안 그대로 | 20% | **60%** | 40% |
| SA와 결과 일치, 개발자 확인 후 | 60% | **60%** | 80% |
| 틀린 항목 중 확인 요청된 것 | 3/8 | **0/6** | 3/4 |

마지막 열은 규칙을 만든 세트로 잰 값이라 공정한 숫자가 아니다. 개발 세트(13개)는 v2에서도 92% / 100%로 그대로라, 회귀는 없었다.

**좋아진 점**: 초안 그대로의 결과 일치가 20% → 60%. 실시간 + 로컬 저장 앱(socketio-chat)을 지원 불가로 정확히 판정했다.

**새로 드러난 약점: 틀린 6개 항목이 모두 확인 요청 없이 지나갔다.** 원인은 세 가지다.

| 앱 | 틀린 것 | 원인 | v3 규칙 |
|---|---|---|---|
| fastapi-template | 화면+API+Postgres를 **정적 사이트로 만들 수 있다**고 추천 | 백엔드가 `backend/`에 있는데, manifest는 저장소 루트만 읽음 | 하위 폴더(`backend/`, `frontend/`, `complete/`, `app/`)의 manifest도 읽고, 프런트엔드와 백엔드가 함께 있으면 화면+API |
| wagtail-bakery | Postgres를 못 봄 → **DB 없이 만들 수 있다**고 추천 | 의존성이 `requirements/base.txt`, `production.txt`에 있음 | `requirements/*.txt`, `requirements-*.txt` 읽기 |
| wagtail-bakery | 포트 변수를 `CSP_REPORT_URI`로 착각 | 정규식 `*PORT*`가 "RE**PORT**"에 걸림 (단순 버그) | 단어 경계: `PORT` 앞은 `_` 또는 시작 |
| spring-rest | 포트 변수 못 찾음 | 앱이 `complete/`에 있음 | 위의 하위 폴더 규칙 |

**위험도**: 두 앱이 "만들 수 있다"로 잘못 나왔다. fastapi-template은 화면만 배포되고 API가 빠지며, wagtail은 DB 없이 만들어진다. 둘 다 3단계 검증에서 걸릴 가능성이 높다(앱이 응답하지 않거나, 화면이 API 호출에 실패함). 그래도 사람이 승인하기 전에 잡아야 하는 오류다.

**결론**: v2 규칙은 "저장소 루트에 앱이 하나"인 구조에서는 잘 맞지만, **실제 프로젝트에 흔한 모노레포·하위 폴더 구조**에서는 앱을 제대로 보지 못한다. v3는 이 구조를 먼저 다루고, 다시 새 held-out 세트로 잰다.

## 5. held-out 세트 3 (2026-11-05, v3 구현 전에 고정)

[heldout3.json](heldout3.json). v3는 아직 구현하지 않았다. 순서는 ① 이 세트를 고정한다 → ② §4 표의 v3 규칙만 구현한다 → ③ 이 세트로 처음 잰다. 앱을 본 뒤 표에 없는 규칙을 더하면 이 세트는 다시 개발 세트가 된다.

| 앱 | 유형 | SA 기대 결과 | 겨냥한 것 |
|---|---|---|---|
| h5bp | 정적 템플릿 | static / PROVISIONABLE | `dist/`, `src/`에 각자 package.json → 하위 폴더를 앱으로 오인하지 않는가 |
| cloud-run-hello | Go, PORT, 저장 없음 | serverless / PROVISIONABLE | `job/`, `worker-pool/`(다른 샘플) → 하위 폴더 오인 |
| todo-app | client/ + backend/ 한 이미지, MySQL 또는 SQLite, 포트 3000 고정 | none / UNSUPPORTED | 모노레포를 화면+API가 아니라 서버 하나로 보는가, MySQL·SQLite |
| voting-app | 서비스 3개 + Redis + Postgres | none / UNSUPPORTED | 하위 폴더 manifest를 합쳐 앱 하나로 만들지 않고 멀티 서비스로 보는가 |
| excalidraw-room | socket.io 중계 | container / PROVISIONABLE | 목표 밖: 실시간 + 프로세스 메모리 상태 |
| python-getting-started | Django, 운영은 Postgres(DATABASE_URL), Dockerfile 없음 | serverless / RECOMMEND_ONLY | 목표 밖: 주석 처리된 드라이버, 설정으로만 보이는 DB |

정답은 이진서가 2026-11-05에 검토했고 값을 바꾸지 않았다(`reviewed_by`). 판단이 갈릴 수 있던 항목은 python-getting-started의 `data = POSTGRES`(드라이버 줄이 주석), excalidraw-room의 `in_memory_state`, todo-app의 `app_form`이다.

## 6. v3 결과 (2026-11-05)

순서: 세트 3 고정·검토 → §4 표의 규칙 3개만 구현(하위 폴더 `backend/`·`frontend/`·`complete/`·`app/`의 manifest, `frontend/`+`backend/` = 화면+API, `requirements/*.txt`·`requirements-*.txt`, 포트 변수 단어 경계) → 세트 3으로 한 번 측정. 같은 세트로 규칙이 오래전에 굳은 v2도 쟀다(v2도 이 앱들을 본 적이 없으므로 공정한 비교).

| 측정 | v2 → held-out 2 | **v3 → held-out 3** | v2 → held-out 3 | (참고) v3 → held-out 2 |
|---|---|---|---|---|
| 항목 정확도 | 88% | **93%** | 93% | 94% |
| SA와 결과 일치, 초안 그대로 | 60% | **67% (4/6)** | 67% | 80% |
| SA와 결과 일치, 개발자 확인 후 | 60% | **83% (5/6)** | 83% | 100% |
| 틀린 항목 중 확인 요청된 것 | 0/6 | **2/4** | 2/4 | 1/3 |

개발 세트(13개)와 held-out 1은 v2와 같은 값이라 회귀는 없다. 마지막 열은 v3 규칙을 만든 세트라 공정한 숫자가 아니다.

**정직한 결론: 세트 3은 v3 규칙의 효과를 보여 주지 못했다.** v3와 v2의 결과가 앱 하나하나까지 같다. 세트 3의 앱 중 v3 규칙이 발동할 구조(`frontend/`+`backend/`, `complete/`, `requirements/` 폴더, `*REPORT*` 변수)가 없었기 때문이다. 이 세트가 보여 준 것은 두 가지다.

1. **v3는 처음 보는 앱에서 나빠지지 않았다.** 하위 폴더를 오인하기 쉬운 h5bp(`dist/`·`src/`), cloud-run-hello(`job/`·`worker-pool/`), todo-app(`client/`+`backend/`)이 모두 맞았다. 하위 폴더 규칙을 표에 적힌 폴더 이름으로만 한정한 것이 이 결과를 만들었다.
2. **남은 오류는 v3가 다루지 않는 종류다.**

| 앱 | 틀린 것 | 원인 | 위험도 |
|---|---|---|---|
| voting-app | 멀티 서비스를 못 봄 | 기존 규칙은 `src/<서비스>/Dockerfile`(깊이 2)만 셈. 이 앱은 `vote/`·`result/`·`worker/`가 루트 바로 아래 | 낮음: DB 확인 단계에서 지원 불가로 걸러짐(확인 후 ✅) |
| python-getting-started | DB를 못 봄 → **DB 없이 만들 수 있다고 추천** | Postgres 드라이버가 requirements.txt에 주석으로만 있고, `DATABASE_URL` 설정만 보임 | **높음**: 운영 설정이 아니면 Django가 컨테이너 안 SQLite 파일에 쓴다. held-out 1 thelounge와 같은 "조용한 데이터 손실" 유형 |
| excalidraw-room | 프로세스 메모리 상태 놓침 | socket.io 방(room)은 의존성에 드러나지 않음 | 낮음: 확인 요청됨 |

v3 규칙의 효과는 지금 **규칙의 출처인 held-out 2에서만** 확인됐다(60% → 80% / 100%). 처음 보는 앱에서 효과를 주장하려면 그 구조를 가진 새 held-out 세트가 필요하다. 이 결과를 보고 규칙은 고치지 않는다(D-184). 다음 규칙 후보(v4 목록)는 위 표의 원인에서 정한다: 루트 바로 아래 형제 폴더 Dockerfile 3개 이상 = 멀티 서비스, `DATABASE_URL`·`dj_database_url` 같은 DB 연결 설정은 드라이버가 없어도 DB 확인 요청.
