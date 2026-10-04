## Reference Priority

- Toss:
  사용자 입력 UX, 단계형 Wizard, CTA, feedback, typography 참고

- Vercel:
  PC Web layout, design system, developer-oriented interface 참고

- Railway:
  cloud resource/service/deployment 화면과 provisioning 상태 표현 참고

- Linear:
  sidebar, navigation, information density, status/list 표현 참고

특정 제품의 UI를 그대로 복제하지 말고,
각 reference의 design principle을 추출해
Self-Service Cloud Platform에 맞게 재구성한다.

# Toss UI References

아래 자료는 이 프로젝트의 UI/UX 디자인 참고용이다.

특정 Toss 화면을 그대로 복제하지 말고,
다음 요소를 중심으로 분석해서 PC Web UI에 재해석한다.

- typography hierarchy
- spacing
- form 구성
- selection UX
- primary CTA
- feedback / result UI
- 낮은 information density
- 사용자에게 기술 용어를 최소한으로 노출하는 방식

## References

### 1. Signup Flow
https://toss.tech/article/toss-signup-process

참고할 부분:
- 한 화면에 하나의 주요 질문
- 큰 제목 + 짧은 설명
- 명확한 CTA
- 단계형 입력 UX

### 2. TextField
https://tossmini-docs.toss.im/tds-mobile/components/TextField/text-field/

참고할 부분:
- 입력 필드
- validation
- helper text
- error state

### 3. Top
https://tossmini-docs.toss.im/tds-mobile/components/top/

참고할 부분:
- heading hierarchy
- 설명 텍스트
- 여백

### 4. ListRow
https://tossmini-docs.toss.im/tds-mobile/components/ListRow/list-row-components/

참고할 부분:
- 선택 항목 표현
- Architecture / Blueprint 목록

### 5. Result
https://tossmini-docs.toss.im/tds-mobile/components/result/

참고할 부분:
- provisioning success
- provisioning failure
- 완료 상태 표현

## Desktop Web References

### Vercel Geist Design System
https://vercel.com/geist/introduction

참고할 부분:
- desktop layout
- typography
- spacing
- button / card / input
- developer tool 특유의 깔끔한 정보 밀도
- high-contrast UI

### Railway Quick Start / Project Canvas
https://docs.railway.com/quick-start

참고할 부분:
- project dashboard
- service 생성 flow
- infrastructure/service 상태 표현
- deployment 진행 상태
- developer platform UX

### Railway Services
https://docs.railway.com/services

참고할 부분:
- service detail 화면
- source / variables / deployment / settings 구조
- service lifecycle 표현

### Railway Deployments
https://docs.railway.com/deployments/reference

참고할 부분:
- Initializing / Building / Deploying / Success / Failed 등의 상태 표현
- deployment history
- provisioning progress UI

### Linear My Issues
https://linear.app/docs/my-issues

참고할 부분:
- PC sidebar
- list hierarchy
- 낮은 visual noise
- 정보 밀도 조절
- status / priority 표현