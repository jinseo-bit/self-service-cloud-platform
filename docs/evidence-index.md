# Evidence Index — 주장과 근거

> 2026-11-06 기준. 발표·제안서·README에서 하는 주장마다, 그것을 확인할 수 있는 **파일·테스트·실측 기록**을 적는다. 근거가 약하거나 없는 주장은 "한계"에 그대로 적는다. 결정 번호(D-xxx)는 [project-decisions.md](project-decisions.md).

## 1. 고르기: 실행 방식 추천과 저장소 분석

| 주장 | 근거 | 종류 |
|---|---|---|
| 요구사항에서 정적 / 요청 단위 실행 / 상시 실행을 고르고 이유를 남긴다 | [recommendation-engine.md](recommendation-engine.md), `platform_core/engine/`, `tests/engine/` | 설계 + 단위 테스트 |
| DB 엔진은 추천하지 않고 코드에서 감지해 개발자가 확인한다 | D-183, [research/boundary-and-comparison.md](research/boundary-and-comparison.md) §2, `agent/repo_analyzer.py` | 조사 + 구현 |
| 저장소 분석기는 처음 보는 앱에서 결과 일치 67% (확인 후 83%) | [baseline/heldout3-results.md](baseline/heldout3-results.md), D-190·D-191 | 측정 (held-out, 정답 사전 고정·검토) |
| 평가는 규칙을 만들 때 보지 않은 앱으로만 보고한다 | D-184, [baseline/README.md](baseline/README.md) §1·§4·§6 | 절차 |
| 분석기 v1→v2는 처음 보는 앱에서 개선됐다 (20% → 60%, 초안 그대로) | [baseline/heldout-results.md](baseline/heldout-results.md), [baseline/heldout2-results.md](baseline/heldout2-results.md) | 측정 (서로 다른 held-out 세트) |

## 2. 지키기: 보안 기준과 검증

| 주장 | 근거 | 종류 |
|---|---|---|
| 보안 기준 항목마다 검사가 있다 | [security-baseline.md](security-baseline.md), `platform_core/baseline.py`, `tests/validation/test_artifact_and_coverage.py` | 카탈로그 + 커버리지 테스트 |
| 만들기 전(PLAN)에 위반이면 apply하지 않는다 | D-138, `platform_core/validation/plan.py`, `tests/validation/` | 단위 테스트 |
| 만든 뒤(STATE·HTTP) 기준을 통과해야만 READY | D-139, `platform_core/validation/state.py`·`http.py`, `tests/validation/test_state_validator.py` | 단위 테스트 + E2E |
| 보안 FAIL이면 접속을 즉시 차단한다 | D-65·D-139, `tests/ui/test_ui_flow.py::test_injected_security_failure_shows_failed`, 2026-10-08 E2E(env-1ee973e8, 오탐이었지만 차단 동작은 실측) | UI 테스트 + 실측 |
| 배포하는 이미지에 CRITICAL 취약점이 있으면 생성 실패 | D-192, `tests/validation/test_image_findings.py` | 단위 테스트 (**AWS 실측 전**) |
| 로그 버킷이 지워지거나 덮어써져도 되살릴 수 있다 | D-189, `tests/test_log_versioning_rollout.py`, 2026-11-05 apply 후 재검사 READY | 테스트 + 실측 |
| 워커 권한은 AWS가 실제로 평가하는 리소스 기준 | D-177, `tests/test_worker_iam_map.py` | 테스트 |

## 3. 지키기: 배포 후 (Day-2)

| 주장 | 근거 | 종류 |
|---|---|---|
| 콘솔에서 보안 설정을 바꾸면 9~11초 안에 감지 | D-188, 1차 E2E(env-c9fc390e) 9초, 2차 E2E(env-9183234c) 11초, `scripts/day2_report.ps1` | 실측 2회 |
| 감지 후 복구는 사람 승인 → 저장된 plan만 적용 → 재검증 | D-186, `control_plane/worker.py`, `tests/control_plane/test_drift.py` | 테스트 + 실측 |
| 승인 후 복구 완료까지 3분 30초~3분 47초 | D-188 (`apply_seconds`) | 실측 2회 |
| 차단 중 재검사가 같은 drift를 중복 기록하지 않는다 | D-188 ②, 2차 E2E에서 `drift_history` 1건 유지 | 실측 + 테스트 |
| 모든 환경의 상태·보안·비용을 한 화면에서 본다 (관리자 전체, 사용자 자기 것) | D-194, `tests/ui/test_ui_flow.py::test_overview_shows_health_and_totals`, `tests/control_plane/test_api_flow.py` | UI·API 테스트 (**AWS 반영 전**) |

## 4. 이어 주기: Agent

| 주장 | 근거 | 종류 |
|---|---|---|
| Claude Code에서 "배포해 줘"로 초안까지 만든다 | D-185, `.claude/skills/sscp-deploy/`, `agent/mcp_server.py`, `tests/agent/test_agent_flow.py` | 구현 + 테스트 |
| 에이전트는 AWS 자격 증명이 없고 승인·삭제·복구를 할 수 없다 | D-181·D-185 (`AGENT_ROUTES`), 2026-11-04 실제 AWS에서 repair/approve/extend 403 | 테스트 + 실측 |
| 에이전트가 만든 초안으로 실제 환경을 만들었다 | 2차 E2E env-9183234c (`created_via: agent`), 승인 → READY 3분 54초 | 실측 |
| 비밀번호는 개발자 터미널에서만 입력 | `agent/login.py` (getpass, 저장 안 함) | 구현 |

## 5. 비용과 운영

| 주장 | 근거 | 종류 |
|---|---|---|
| 승인 전에 예상 비용을 보여 주고 상한을 넘으면 막는다 | D-158, `platform_core/engine/admission.py`, `tests/engine/test_admission.py` | 테스트 |
| drift·복구·재배포 중인 환경도 상한·한도에 포함 | D-193, `test_drifted_redeploying_and_repairing_environments_still_count` | 테스트 (버그 수정) |
| 단가는 공개 가격표를 손으로 입력한 snapshot | `platform_core/pricing/snapshot.json` (`seed-2026-10-04`, source=manual), D-142 | 데이터 |
| 생성 시간: Static 3분 58초, Serverless 1분 43초, Container 첫 생성 20분 2초(공유 인프라 12분 12초 포함) | [engineering-log.md](engineering-log.md), 2026-10-05·10-07·10-11 E2E | 실측 |
| Static 재배포 9초, 되돌리기 5초, 삭제 4분 5초 | 2026-10-05 E2E, README §5 | 실측 |

## 6. 비교 (피드백 ①④)

| 주장 | 근거 |
|---|---|
| "에이전트로 배포"만으로는 차별점이 아니다 (AWS ECS MCP Server가 있음) | [research/boundary-and-comparison.md](research/boundary-and-comparison.md) §3.2 |
| 차이는 에이전트가 AWS를 바꿀 수 없다는 구조 | 같은 문서 §3.2, §4 |
| drift 감지는 AWS Config에도 있다. 차이는 승인 기준·HTTP 검사·차단·사람 승인 복구 | 같은 문서 §3.3 |

## 7. 한계 (근거가 없거나 약한 것)

| 한계 | 상태 |
|---|---|
| 분석기 v3 규칙의 효과는 처음 보는 앱에서 미입증 | D-191. 세트 3에 해당 구조가 없었음 |
| 조용한 데이터 손실 유형(주석 처리된 DB 드라이버 등)을 분석기가 놓칠 수 있다 | held-out 1 thelounge, held-out 3 python-getting-started |
| 이미지 취약점은 배포 때 한 번만. 운영 중 새 CVE는 못 잡음 | D-192 (사용자 결정) |
| OS·런타임 자동 패치 없음 | 비교 문서 §3.1 |
| Negative E2E(B-2) 미실행 | Backlog B-2 |
| Container 두 번째 환경(warm) 생성 시간 미측정 | 남은 일 |
| 계정 1개, DEV 위주, 사용자 소수 | 학습 프로젝트 범위 |
| 단가는 수동 snapshot (Price List API 자동 갱신 안 함) | O-08 |
