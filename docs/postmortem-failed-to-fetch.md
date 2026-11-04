# Postmortem — 브라우저의 "Failed to fetch" 두 건

> 초안 (B-4, 2026-11-05). 비난 없는(blameless) 형식. 두 사건은 원인은 다르지만, 브라우저가 보여 준 증상과 로컬 테스트가 못 잡은 이유가 같아서 한 문서로 묶었다.

## 요약

| | 사건 1: 업로드 | 사건 2: 복구 버튼 |
|---|---|---|
| 날짜 | 2026-10-05 (플랫폼 첫 AWS 배포) | 2026-10-28 (Day-2 첫 E2E, env-c9fc390e) |
| 사용자에게 보인 것 | zip 업로드 시 "Failed to fetch" | "원래 구성으로 복구" 클릭 시 "Failed to fetch" |
| 실제 HTTP 응답 | S3 **307** Temporary Redirect | API Gateway **404** (라우트 없음) |
| 브라우저가 실패한 이유 | 교차 출처 요청이 리다이렉트됨 → CORS 실패 | 404 응답에 CORS 헤더 없음 → 브라우저가 응답을 숨김 |
| 근본 원인 | presigned POST URL이 전역 endpoint `s3.amazonaws.com` | `infra/platform/api.tf` 라우트 목록에 `/recheck`, `/repair` 누락 |
| 영향 | Static 환경을 만들 수 없음 (배포 직후 발견) | 복구를 승인할 수 없음. 환경은 차단된 채 안전하게 유지 |
| 수정 | 리전 virtual-hosted endpoint 고정 | 라우트 추가 |
| 재발 방지 | `test_presigned_upload_uses_regional_virtual_hosted_endpoint` | `tests/test_api_routes_sync.py` |

## 사건 1: 업로드 307

**무슨 일이 있었나.** 브라우저는 API가 만든 presigned POST로 S3에 직접 업로드한다. boto3 기본 설정이 만든 URL은 전역 host `https://<bucket>.s3.amazonaws.com/`이었다. 새로 만든 `ap-northeast-2` 버킷에 대해 S3는 전역 host 요청을 리전 host로 **307 리다이렉트**한다. 이 리다이렉트는 DNS가 전파되는 동안 일어난다([S3: Temporary request redirection](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingRouting.html#TemporaryRedirection)). 교차 출처 요청이 리다이렉트되면 브라우저는 CORS 검사를 통과시키지 않고 `TypeError: Failed to fetch`만 남긴다.

**수정.** `control_plane/api.py`의 S3 클라이언트를 리전 + SigV4 + virtual-hosted 방식으로 고정했다.

```python
_s3 = boto3.client("s3", region_name=..., config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
```

테스트는 URL이 정확히 `https://uploads.s3.ap-northeast-2.amazonaws.com/`인지, 서명이 `AWS4-HMAC-SHA256`인지 확인한다.

## 사건 2: 복구 라우트 404

**무슨 일이 있었나.** Day-2 기능을 만들면서 Lambda의 라우트 표(`control_plane.api.ROUTES`)에는 `POST /environments/{env_id}/recheck`, `POST /environments/{env_id}/repair`를 넣었다. 하지만 API Gateway HTTP API의 라우트 목록(`api.tf`)에는 넣지 않았다. HTTP API는 일치하는 라우트가 없으면 Lambda를 부르지 않고 **CORS 헤더 없는 404**를 돌려준다. 그래서 브라우저에는 사건 1과 똑같은 "Failed to fetch"가 보였다.

**안전 측면.** 보안 drift를 감지하면 접속은 이미 차단(D-65)된 상태였다. 그래서 복구가 늦어진 동안 사이트는 **닫힌 채로 안전하게** 남아 있었다. 고장이 위험한 쪽(열린 상태)이 아니라 안전한 쪽(닫힌 상태)에서 일어났다.

**수정과 확인.** 라우트를 추가하고 사용자가 apply했다. 그 뒤 탐침 요청으로 확인했다. 인증 없는 `/recheck`, `/repair` 요청은 401과 CORS 헤더를 받고, 존재하지 않는 라우트만 CORS 없는 404를 받는다. 2차 E2E(2026-11-04)에서는 복구가 정상 동작했다(승인 → READY 3분 30초).

## 왜 로컬 테스트가 못 잡았나

두 사건의 공통 원인이 여기에 있다. **로컬 개발 서버(D-145)가 바로 그 경계를 대체물로 바꿔 두었다.**

| 경계 | 로컬 대체물 | 그래서 안 보인 것 |
|---|---|---|
| S3 presigned 업로드 | 같은 origin의 `/local/upload/<env>` | 교차 출처도, 307도 없음 |
| API Gateway 라우팅 | Lambda 라우트 표를 그대로 사용 | `api.tf`에 빠진 라우트가 로컬에서는 존재함 |

UI 테스트(Playwright)는 로컬 서버를 대상으로 돌기 때문에 두 경계를 모두 통과해 버렸다.

## 재발 방지

1. **대체물의 차이를 테스트로 고정한다.** 대체물을 쓰는 지점마다 "실제 AWS에서는 무엇이 달라야 하는가"를 단위 테스트로 적는다. endpoint 형태, 라우트 동기화가 그 예다.
2. **두 곳에 적힌 목록은 동기화 테스트로 묶는다.** 라우트는 `ROUTES`와 `api.tf`에, 감시 이벤트는 `drift.WATCHED_EVENTS`와 `day2.tf`에 있다. 한쪽만 바꾸면 테스트가 실패한다.
3. **"Failed to fetch"를 보면 응답 코드부터 본다.** 브라우저 메시지는 원인을 숨긴다. 개발자 도구의 Network 탭이나 `curl -i`로 실제 상태 코드(307, 404, 403)와 `access-control-allow-origin` 헤더 유무를 먼저 확인한다.

## 남은 위험

- CORS 헤더 없는 404는 API Gateway 기본 동작이라 앞으로도 같은 증상이 나올 수 있다. HTTP API의 `$default` 라우트로 CORS가 붙은 404를 돌려주는 방법도 있지만, 인증 없이 Lambda가 호출되는 경로가 생긴다. 그래서 지금은 동기화 테스트로 막는 쪽을 택했다.
