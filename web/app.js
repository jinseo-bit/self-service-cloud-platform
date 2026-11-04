// Self-Service Cloud — Web UI (no build step). Talks to the control-plane HTTP API.
const CFG = window.SSCP_CONFIG || {};
const $app = document.getElementById("app");
if (CFG.localMode) document.body.classList.add("has-banner");

// ------------------------------------------------------------------ utils
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (strings, ...vals) => strings.reduce((out, s, i) => out + s + (i < vals.length ? vals[i] : ""), "");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" }) : "-");
const usd = (n) => (n === undefined || n === null ? "-" : `$${n}`);
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.setAttribute("role", "status");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

function modal({ title, body, confirm, danger = false, cancel = "취소" }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const wrap = document.createElement("div");
    wrap.className = "modal-backdrop";
    wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <h2 id="modal-title">${esc(title)}</h2><div class="modal-body">${body}</div>
      <div class="btn-row modal-actions"><button class="btn" data-r="0">${esc(cancel)}</button>
      <button class="btn ${danger ? "danger-fill" : "primary"}" data-r="1">${esc(confirm)}</button></div></div>`;
    const close = (v) => { wrap.remove(); document.removeEventListener("keydown", onKey); prev?.focus?.(); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(false); };
    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(false); });
    wrap.querySelectorAll("[data-r]").forEach((b) => (b.onclick = () => close(b.dataset.r === "1")));
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    wrap.querySelector('[data-r="1"]').focus();
  });
}

function loadingPage(text) {
  return `<div class="page"><div class="card result run" role="status" aria-live="polite"><div class="icon spin" aria-hidden="true">◌</div><h2>${esc(text)}</h2></div></div>`;
}

const CIDR_RE = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}\/(3[0-2]|[12]?\d)$/;

// ------------------------------------------------------------------ auth (Cognito user pool, USER_PASSWORD_AUTH)
const auth = {
  get tokens() {
    try { return JSON.parse(sessionStorage.getItem("sscp.tokens") || "null"); } catch { return null; }
  },
  set tokens(t) {
    try { t ? sessionStorage.setItem("sscp.tokens", JSON.stringify(t)) : sessionStorage.removeItem("sscp.tokens"); } catch {}
  },
  claims() {
    const t = this.tokens;
    if (!t) return null;
    try { return JSON.parse(atob(t.id.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return null; }
  },
  async cognito(target, body) {
    const r = await fetch(`https://cognito-idp.${CFG.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.message || data.__type || "로그인에 실패했습니다.");
    return data;
  },
  save(result, refresh) {
    this.tokens = { id: result.IdToken, access: result.AccessToken, refresh: result.RefreshToken || refresh };
  },
  async login(email, password, admin = false) {
    if (CFG.authMode === "local") {
      // LOCAL ONLY: scripts/local_dev.py issues an unsigned dev token instead of Cognito.
      const res = await fetch("/local/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, admin }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "로그인에 실패했습니다.");
      this.save(data);
      return {};
    }
    const r = await this.cognito("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH", ClientId: CFG.clientId, AuthParameters: { USERNAME: email, PASSWORD: password },
    });
    if (r.ChallengeName === "NEW_PASSWORD_REQUIRED") return { challenge: r };
    this.save(r.AuthenticationResult);
    return {};
  },
  async newPassword(challenge, email, password) {
    const r = await this.cognito("RespondToAuthChallenge", {
      ChallengeName: "NEW_PASSWORD_REQUIRED", ClientId: CFG.clientId, Session: challenge.Session,
      ChallengeResponses: { USERNAME: email, NEW_PASSWORD: password },
    });
    this.save(r.AuthenticationResult);
  },
  async refresh() {
    const t = this.tokens;
    if (CFG.authMode === "local" || !t?.refresh) return false;
    try {
      const r = await this.cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: CFG.clientId, AuthParameters: { REFRESH_TOKEN: t.refresh } });
      this.save(r.AuthenticationResult, t.refresh);
      return true;
    } catch { return false; }
  },
  logout() { this.tokens = null; location.hash = "#/login"; render(); },
};

// ------------------------------------------------------------------ api
async function api(method, path, body, retried = false) {
  const t = auth.tokens;
  if (!t) { location.hash = "#/login"; throw new Error("로그인이 필요합니다."); }
  const r = await fetch(CFG.apiUrl.replace(/\/$/, "") + path, {
    method, headers: { Authorization: t.id, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && !retried && (await auth.refresh())) return api(method, path, body, true);
  if (r.status === 401) { auth.logout(); throw new Error("다시 로그인해 주세요."); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : {};
  if (!r.ok) { const e = new Error(data.error || `요청 실패 (${r.status})`); e.data = data; e.status = r.status; throw e; }
  return data;
}

async function uploadZip(upload, file) {
  const form = new FormData();
  Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
  form.append("file", file);
  const r = await fetch(upload.url, { method: "POST", body: form });
  if (!r.ok) throw new Error("파일 업로드에 실패했습니다. (zip, 50MB 이하)");
}

// ------------------------------------------------------------------ vocab
const STATUS = {
  DRAFT: ["초안", ""], PENDING_ADMIN_APPROVAL: ["관리자 승인 대기", "warn"], PROVISIONING: ["생성 중", "run"],
  VALIDATING: ["검증 중", "run"], READY: ["사용 가능", "ok"], DEGRADED: ["점검 필요", "warn"], FAILED: ["실패", "bad"],
  DELETING: ["삭제 중", "run"], DELETED: ["삭제됨", ""], DELETE_FAILED: ["삭제 실패", "bad"], REDEPLOYING: ["새 버전 배포 중", "run"],
  DRIFTED: ["설정 변경 감지", "warn"], REPAIRING: ["복구 중", "run"],  // Day-2 (D-186)
};
const badge = (s) => `<span class="badge ${STATUS[s]?.[1] || ""}">${esc(STATUS[s]?.[0] || s)}</span>`;
const IN_PROGRESS = new Set(["PROVISIONING", "VALIDATING", "DELETING", "REDEPLOYING", "REPAIRING"]);
// Blueprint names by how the app runs (D-182): Fargate is serverless too, so "Serverless vs Container" was ambiguous.
const RUN_MODEL = { STATIC: "정적 호스팅", SLS: "요청 단위 실행", SLS_PC: "요청 단위 실행", CTR: "상시 실행 컨테이너" };
const runModel = (a) => [a.frontend === "STATIC" ? RUN_MODEL.STATIC : null, RUN_MODEL[a.compute]].filter(Boolean).join(" + ");
const NODE = {
  STATIC: ["CloudFront + S3", "화면 파일 CDN"], SLS: ["Lambda + API Gateway", "요청 시에만 실행"],
  SLS_PC: ["Lambda (미리 준비) + API Gateway", "첫 요청 지연 없음"], CTR: ["ECS Fargate", "컨테이너 상시 실행"],
  DDB: ["DynamoDB", "고정비 없는 DB"], RDS_SAZ: ["PostgreSQL", "단일 가용 영역"], RDS_MAZ: ["PostgreSQL", "다중 가용 영역"],
  AUR_PAUSE: ["Aurora Serverless", "쉬면 자동 정지"], AUR_MAZ: ["Aurora Serverless", "다중 가용 영역"],
};
const DIM_KO = { Security: "보안", Reachability: "접속", Resilience: "장애 내성", Observability: "기록", Operability: "운영", Cost: "비용" };
const LAYER_KO = { ARTIFACT: "파일", PLAN: "생성 전", STATE: "생성 후", HTTP: "외부 접속" };

const QUESTIONS = [
  { key: "app_form", title: "어떤 앱을 올리시나요?", desc: "만든 결과물의 형태를 골라 주세요.", options: [
    ["STATIC_SITE", "화면만 있는 사이트", "HTML·CSS·JS 파일 (React·Vue 빌드 결과 포함)"],
    ["SERVER_APP", "서버 앱", "API 서버나 서버에서 화면을 만드는 앱"],
    ["STATIC_PLUS_API", "화면 + API", "화면 파일과 API 서버를 함께 운영"]] },
  { key: "capabilities", title: "서버 앱에 이런 기능이 있나요?", desc: "해당하는 것을 모두 골라 주세요. 없으면 그냥 넘어가도 됩니다.", multi: true,
    when: (s) => s.app_form !== "STATIC_SITE", options: [
    ["realtime_connection", "실시간 연결", "채팅, 실시간 알림처럼 연결을 계속 유지"],
    ["large_payload", "큰 파일 업로드·다운로드", "한 번에 6MB가 넘는 요청이나 응답"],
    ["in_memory_state", "서버 메모리·로컬 디스크에 상태 보관", "로그인 상태나 업로드 파일을 서버 안에 두고 다음 요청에서 다시 씀"]] },
  { key: "request_duration", title: "가장 오래 걸리는 요청은 얼마나 걸리나요?",
    desc: "버튼을 누르고 결과가 나올 때까지 걸리는 시간 중 가장 긴 것 하나를 골라 주세요.", when: (s) => s.app_form !== "STATIC_SITE", options: [
    ["SECONDS", "대부분 몇 초 안에 끝나요", ""], ["ABOUT_30S", "30초 정도 걸리는 작업도 있어요", ""],
    ["S30_TO_60S", "30초~1분 걸리는 작업도 있어요", ""], ["S60_TO_120S", "1~2분 걸리는 작업도 있어요", ""],
    ["OVER_120S", "2분 넘게 걸리는 작업도 있어요", ""]] },
  { key: "traffic_scale", title: "하루에 몇 명 정도 쓰나요?", desc: "정확하지 않아도 괜찮아요. 가장 가까운 것을 골라 주세요.", options: [
    ["T1", "100명 이하", "개인·소규모"], ["T2", "1,000명 이하", "팀·동아리·작은 서비스"],
    ["T3", "1만 명 이하", "많이 쓰는 서비스"], ["T4", "1만 명 넘게", "대규모"]] },
  { key: "traffic_pattern", title: "주로 언제 쓰나요?", desc: "사용 패턴에 따라 비용 구조가 달라집니다.", options: [
    ["IDLE_MOSTLY", "거의 안 써요", "가끔 들어오는 정도"], ["BUSINESS_HOURS", "업무시간에만", "평일 낮에 집중"],
    ["STEADY", "하루 종일 꾸준히", ""], ["SPIKY", "갑자기 몰려요", "이벤트·공지 직후 급증"]] },
  { key: "data", title: "데이터를 저장해야 하나요?", desc: "회원 정보, 게시글, 주문처럼 서버에 남겨야 하는 데이터가 있는지 알려 주세요.", options: [
    ["NONE", "저장할 데이터 없음", ""], ["POSTGRES", "저장해야 함 · PostgreSQL을 써요", ""],
    ["DYNAMODB", "저장해야 함 · DynamoDB를 써요", ""], ["UNKNOWN", "저장해야 함 · 어떤 DB인지 모르겠어요", "다음 화면에서 몇 가지만 더 여쭤볼게요"]] },
  { key: "traits", title: "데이터는 어떤 모양인가요?", desc: "해당하는 것을 모두 골라 주세요.", multi: true, when: (s) => s.data === "UNKNOWN", options: [
    ["relational", "데이터끼리 연결이 많아요", "회원-주문-상품처럼 서로 연결"],
    ["flexible_query", "여러 조건으로 검색·집계해요", "기간별 매출, 조건 검색 등"],
    ["key_lookup_only", "ID로만 꺼내 써요", "세션, 설정값처럼 키로 바로 조회"]] },
  { key: "downtime_tolerance", title: "잠깐 멈추면 어떻게 되나요?", desc: "AWS 데이터센터 하나에 문제가 생겼을 때를 생각해 주세요.", options: [
    ["OK", "잠깐 멈춰도 괜찮아요", "가장 저렴"], ["NOT_IN_BUSINESS_HOURS", "업무시간엔 멈추면 안 돼요", ""],
    ["NEVER", "절대 멈추면 안 돼요", "가장 높은 가용성"]] },
  { key: "latency_sensitivity", title: "첫 요청이 1~2초 느려도 괜찮나요?", desc: "오래 쉬다가 들어온 첫 요청이 조금 느릴 수 있는 구성이 더 저렴합니다.", options: [
    ["TOLERANT", "괜찮아요", ""], ["SENSITIVE", "항상 빨라야 해요", ""]] },
  { key: "exposure", title: "누가 접속하나요?", desc: "", options: [
    ["PUBLIC", "누구나", "인터넷에 공개"], ["APP_AUTH", "로그인한 사용자만", "로그인은 앱이 처리"], ["IP_ALLOWLIST", "특정 IP에서만", "회사·학교 네트워크 등"]] },
  { key: "final", title: "마지막으로 확인할게요", desc: "", custom: true },
];

// Short, app-centric examples (shown only when the user opens "예시 보기").
const EXAMPLES = {
  app_form: { STATIC_SITE: ["포트폴리오", "회사 소개 페이지", "React로 만든 화면(빌드 결과)"], SERVER_APP: ["로그인·회원 API", "게시판 서버"],
    STATIC_PLUS_API: ["화면 + 주문 API가 있는 쇼핑몰", "관리자 화면 + 데이터 API"] },
  capabilities: { realtime_connection: ["채팅", "실시간 알림", "공동 편집"], large_payload: ["동영상·대용량 파일 업로드"],
    in_memory_state: ["로그인 정보를 서버 메모리에 보관"] },
  request_duration: { SECONDS: ["로그인", "상품 조회", "글 저장"], ABOUT_30S: ["외부 결제·API 호출을 기다림"],
    S30_TO_60S: ["엑셀 보고서 만들기"], S60_TO_120S: ["큰 이미지 일괄 변환"], OVER_120S: ["동영상 인코딩", "대량 데이터 일괄 처리"] },
  traffic_scale: { T1: ["개인 블로그", "팀 내부 도구"], T2: ["동아리·학과 서비스", "작은 회사 홈페이지"], T3: ["지역 서비스", "인기 있는 커뮤니티"],
    T4: ["전국 단위 서비스"] },
  traffic_pattern: { IDLE_MOSTLY: ["개인 포트폴리오"], BUSINESS_HOURS: ["사내 결재·관리 도구"], STEADY: ["항상 쓰는 커뮤니티"],
    SPIKY: ["수강 신청", "티켓 오픈", "이벤트 공지"] },
  data: { NONE: ["소개 페이지", "문서 사이트"], POSTGRES: ["회원·주문처럼 서로 연결된 데이터"], DYNAMODB: ["키로 바로 꺼내는 세션·설정값"],
    UNKNOWN: ["잘 모르겠다면 이걸 고르세요"] },
  traits: { relational: ["회원-주문-상품"], flexible_query: ["기간별 매출, 조건 검색"], key_lookup_only: ["세션, 장바구니"] },
  downtime_tolerance: { OK: ["개인 블로그", "연습용 서비스"], NOT_IN_BUSINESS_HOURS: ["사내 업무 도구"], NEVER: ["결제·예약 서비스"] },
  latency_sensitivity: { TOLERANT: ["보고서 생성", "관리용 작업", "가끔 쓰는 서비스"], SENSITIVE: ["로그인 API", "상품 조회", "실시간 화면"] },
  exposure: { PUBLIC: ["회사 홈페이지"], APP_AUTH: ["회원 전용 서비스"], IP_ALLOWLIST: ["회사 안에서만 쓰는 도구"] },
};

// Weak, deterministic hints from earlier answers only. null = no hint (D-161). Never auto-selects.
function suggestion(key, w) {
  if (key === "data" && w.app_form === "STATIC_SITE") return "NONE";
  if (key === "latency_sensitivity" && w.app_form === "STATIC_SITE") return "TOLERANT";
  if (key === "downtime_tolerance" && w.traffic_pattern === "IDLE_MOSTLY") return "OK";
  return null;
}

function examplesBlock(q, w) {
  const ex = EXAMPLES[q.key];
  if (!ex) return "";
  const hint = suggestion(q.key, w);
  const hintLabel = hint && q.options.find(([v]) => v === hint)?.[1];
  const items = q.options.filter(([v]) => ex[v]).map(([v, t]) =>
    `<li><b>“${esc(t)}”</b><span class="muted"> — ${ex[v].map(esc).join(", ")}</span></li>`).join("");
  return h`<details class="examples" id="examples" ${w.examplesOpen[q.key] ? "open" : ""}><summary>예시 보기</summary>
    <ul class="example-list">${items}</ul>
    ${hintLabel ? `<p class="hint" data-hint="${esc(hint)}">이 경우 보통 <b>“${esc(hintLabel)}”</b>을(를) 많이 골라요.</p>` : ""}</details>`;
}

// ------------------------------------------------------------------ state
const S = { me: null, templates: [], baseline: [], checkTitles: {}, envs: [], wizard: null, poll: null };
const LAYER_ORDER = ["ARTIFACT", "PLAN", "STATE", "HTTP"];
// Internal ids (reason codes, SB/PR check ids) are for reviewers; regular users only see sentences.
const isReviewer = () => !!S.me?.admin;
const codeTag = (code, style = "") => (isReviewer() ? `<span class="code-tag"${style ? ` style="${style}"` : ""}>${esc(code)}</span>` : "");
const LAYER_DESC = { ARTIFACT: "올린 파일 검사", PLAN: "만들기 전 설계 검사", STATE: "만든 뒤 설정 검사", HTTP: "실제 주소로 접속해 보는 검사" };
const isLocalSkip = (c) => c.status === "NOT_APPLICABLE" && /^로컬 모드/.test(c.detail || "");
const archLabel = (a) => {
  if (!a) return "-";
  const parts = [];
  if (a.frontend === "STATIC") parts.push("정적 호스팅 (CloudFront + 비공개 S3)");
  if (a.compute && a.compute !== "NONE") parts.push(NODE[a.compute][0]);
  if (a.db && a.db !== "NONE") parts.push(NODE[a.db][0]);
  return parts.join(" + ") || a.candidate_id;
};

function wizardDefaults(preset) {
  const i = preset.input;
  return {
    preset: preset.id, app_form: i.app_form, capabilities: [], traffic_scale: i.traffic_scale,
    traffic_pattern: i.traffic_pattern, data: i.data?.needed ? (i.data.declared || "UNKNOWN") : "NONE", traits: [],
    downtime_tolerance: i.downtime_tolerance, latency_sensitivity: i.latency_sensitivity, exposure: i.exposure,
    cidrs: "", tier: i.tier, budget: i.budget_usd_month, name: "", spa: false, step: 0, request_duration: "SECONDS",
    dirty: false, examplesOpen: {}, budgetOverride: null, lastAdjust: null,
  };
}

// 내 앱에 맞게 만들기 (D-178, D-187): start from no answers. Every single-choice question is answered by the user, never pre-filled.
function blankWizard() {
  return {
    ...wizardDefaults({ id: null, input: { tier: "DEV", budget_usd_month: "" } }),
    app_form: null, request_duration: null, traffic_scale: null, traffic_pattern: null, data: null,
    downtime_tolerance: null, latency_sensitivity: null, exposure: null, mode: "custom",
  };
}

function toInput(w) {
  const caps = new Set(w.capabilities);
  const data = w.data === "NONE" ? { needed: false } : {
    needed: true, declared: w.data === "UNKNOWN" ? "UNKNOWN" : w.data,
    traits: Object.fromEntries(["relational", "flexible_query", "key_lookup_only"].map((t) => [t, w.traits.includes(t)])),
  };
  return {
    app_form: w.app_form, traffic_scale: w.traffic_scale, traffic_pattern: w.traffic_pattern, data,
    capabilities: w.app_form === "STATIC_SITE" ? {} : {
      realtime_connection: caps.has("realtime_connection"), large_payload: caps.has("large_payload"),
      in_memory_state: caps.has("in_memory_state"), request_duration: w.request_duration,
    },
    downtime_tolerance: w.downtime_tolerance, latency_sensitivity: w.latency_sensitivity, exposure: w.exposure,
    allowlist_cidrs: w.exposure === "IP_ALLOWLIST" ? w.cidrs.split(/[\s,]+/).filter(Boolean) : [],
    tier: w.tier, budget_usd_month: Number.isFinite(+w.budget) && w.budget !== "" ? Math.max(0, Math.floor(+w.budget)) : null,
    spa_fallback: w.spa,
  };
}

// Quick Start (D-163): a template only fills the Requirement Model; the server runs the same engine.
function quickTemplate(w) { return w?.mode === "quick" ? S.templates.find((t) => t.template_id === w.template) : null; }

function templateWizard(t) {
  const w = wizardDefaults({ id: t.template_id, input: t.default_requirements });
  return { ...w, mode: "quick", template: t.template_id, fromTemplate: t.title, spa: !!t.default_requirements.spa_fallback };
}

function requirementsBody(w) {
  const t = quickTemplate(w);
  if (!t) return { input: toInput(w) };
  const values = { tier: w.tier, spa_fallback: !!w.spa };
  if (w.budget !== "" && w.budget !== null && w.budget !== undefined) values.budget_usd_month = Math.max(0, Math.floor(+w.budget));
  return { template: { id: t.template_id, values } };
}

// Upload checks in plain words: what failed, what was inside the zip, how to fix it (D-164).
const ARTIFACT_FIX = {
  "ART-ZIP": "zip 형식이 아니에요. 빌드 결과 폴더를 zip으로 압축해서 올려 주세요.",
  "ART-PATH": "zip 안에 ‘..’이나 절대 경로가 들어 있어요. 빌드 결과 폴더에서 다시 압축해 주세요.",
  "ART-SIZE": "파일 크기 합계는 50MB, 개수는 5,000개 이하여야 해요. 원본 이미지·영상이나 소스맵(.map)을 빼 보세요.",
  "ART-INDEX": "zip 맨 위(또는 하나뿐인 폴더 바로 아래)에 index.html이 있어야 해요. React·Vite는 dist/, CRA는 build/ 폴더 안의 내용물을 묶어 주세요.",
  "SB-SEC-04": "Access Key 같은 비밀값으로 보이는 문자열이 있어요. 비밀값은 사이트 파일에 넣지 말고 빼 주세요.",
};
const failedChecks = (artifact) => (artifact?.checks || []).filter((c) => c.status === "FAIL");
const failedTitles = (artifact) => failedChecks(artifact).map((c) => S.checkTitles[c.check_id] || c.check_id).join(", ");

function artifactHelp(artifact) {
  const failed = failedChecks(artifact);
  if (!failed.length) return "";
  const sm = artifact.summary;
  const notWeb = sm && sm.html_files === 0 && sm.not_web_files > 0;
  const found = sm ? h`<h3 style="margin-top:16px">올린 zip 안에서 찾은 것</h3>
    <p class="small" data-found>파일 ${sm.files}개 · ${sm.extensions.map(([e, n]) => `${esc(e)} ${n}개`).join(", ")} · html 파일 ${sm.html_files}개</p>
    ${sm.nested_index.length ? `<p class="small muted">index.html 위치: <span class="mono">${sm.nested_index.map(esc).join(", ")}</span> — 이 파일이 있는 폴더 안의 내용물을 묶어 주세요.</p>` : ""}` : "";
  return h`<div class="card" data-artifact-help><h2>올린 파일이 검사를 통과하지 못했어요</h2>
    ${notWeb ? `<p class="notice warn">웹사이트 빌드 결과가 아닌 것 같아요. 실행 파일(.exe·.dll 등)만 있고 html 파일이 없어요.</p>` : ""}
    <ul class="reasons">${failed.map((c) => `<li class="bad"><span class="dot"></span><span><b>${esc(S.checkTitles[c.check_id] || c.check_id)}</b><br><span class="small">${esc(ARTIFACT_FIX[c.check_id] || c.detail)}</span>${codeTag(c.check_id)}</span></li>`).join("")}</ul>
    ${found}</div>`;
}

// ------------------------------------------------------------------ shell
function shell(content) {
  const route = location.hash;
  const items = S.envs.map((e) => h`
    <a class="side-item ${route === "#/env/" + e.env_id ? "active" : ""}" href="#/env/${esc(e.env_id)}">
      <span class="sdot ${STATUS[e.status]?.[1] || ""}" aria-hidden="true"></span><span class="name">${esc(e.name)}</span><span class="sr-only">${esc(STATUS[e.status]?.[0] || e.status)}</span>
    </a>`).join("");
  return h`
  ${CFG.localMode ? `<div class="local-banner" role="note">로컬 시뮬레이션 — AWS에 아무것도 만들지 않습니다. 생성·검증 단계는 로컬에서 흉내만 냅니다.</div>` : ""}
  <a class="skip" href="#main">본문으로 건너뛰기</a>
  <div class="shell">
    <nav class="sidebar" aria-label="환경 목록">
      <a class="brand" href="#/" aria-label="홈으로"><span class="brand-mark"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M9 21a5 5 0 0 1 4-8 6 6 0 0 1 11 3 4 4 0 0 1-1 8H11a3 3 0 0 1-2-3z"/></svg></span>Self-Service Cloud</a>
      <a class="side-item ${route.startsWith("#/new") ? "active" : ""}" href="#/new">＋ 새 환경 만들기</a>
      <a class="side-item ${route === "#/" || route === "" ? "active" : ""}" href="#/">전체 환경</a>
      <div class="side-label">내 환경</div>
      ${items || '<div class="side-item muted small">아직 환경이 없어요</div>'}
      <div class="side-foot"><span class="foot-email">${esc(S.me?.email || "")}${S.me?.admin ? ' · <b>관리자</b>' : ""}</span><br>
        <a href="#" id="logout">로그아웃</a></div>
    </nav>
    <main class="main" id="main" tabindex="-1">${content}</main>
  </div>`;
}

async function refreshEnvs() {
  try {
    const qs = S.me?.admin ? "?all=1" : "";
    S.envs = (await api("GET", "/environments" + qs)).environments;
  } catch (e) { /* shown by page */ }
}

// ------------------------------------------------------------------ pages
function loginPage(stage = {}) {
  $app.innerHTML = h`
  <div class="login"><div class="box">
    <div class="eyebrow">Self-Service Cloud</div>
    <h1>${stage.challenge ? "새 비밀번호를 정해 주세요" : "로그인"}</h1>
    <p class="lead">${stage.challenge ? "처음 로그인했어요. 앞으로 쓸 비밀번호를 정해 주세요." : CFG.authMode === "local" ? "로컬 개발 모드입니다. 아무 이메일로 들어갈 수 있어요." : "관리자에게 받은 계정으로 로그인해 주세요."}</p>
    ${CFG.authMode === "local" ? '<div class="notice warn" style="margin-bottom:20px">로컬 모드 — Cognito 대신 서명 없는 개발용 토큰을 씁니다. 실제 배포에서는 쓰이지 않습니다.</div>' : ""}
    <form id="f" novalidate>
      ${stage.challenge ? "" : `<div class="field"><label for="email">이메일</label><input class="input" id="email" name="email" type="email" autocomplete="username" required aria-describedby="err"></div>`}
      ${CFG.authMode === "local" && !stage.challenge ? `<label class="check-line"><input type="checkbox" name="admin"> 관리자(코치)로 로그인</label>` : h`<div class="field"><label for="password">${stage.challenge ? "새 비밀번호 (12자 이상, 대·소문자·숫자 포함)" : "비밀번호"}</label>
        <input class="input" id="password" name="password" type="password" autocomplete="${stage.challenge ? "new-password" : "current-password"}" required aria-describedby="err"></div>`}
      <div class="help error" id="err" role="alert"></div>
      <button class="btn primary lg" type="submit">${stage.challenge ? "비밀번호 저장하고 시작하기" : "로그인"}</button>
    </form>
  </div></div>`;
  document.getElementById("f").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const err = document.getElementById("err");
    err.textContent = "";
    const email = (f.get("email") || "").trim();
    if (!stage.challenge && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { err.textContent = "이메일 형식을 확인해 주세요."; document.getElementById("email").classList.add("error"); document.getElementById("email").focus(); return; }
    if (stage.challenge && (f.get("password") || "").length < 12) { err.textContent = "비밀번호는 12자 이상이어야 해요."; return; }
    const btn = ev.target.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      if (stage.challenge) await auth.newPassword(stage.challenge, stage.email, f.get("password"));
      else {
        const r = await auth.login(email, f.get("password"), f.get("admin") === "on");
        if (r.challenge) return loginPage({ challenge: r.challenge, email: f.get("email") });
      }
      location.hash = "#/";
      boot();
    } catch (e) { err.textContent = e.message; btn.disabled = false; }
  };
}

function homePage() {
  const rows = S.envs.map((e) => h`
    <tr><td><a href="#/env/${esc(e.env_id)}"><b>${esc(e.name)}</b></a><div class="muted small mono">${esc(e.env_id)}</div></td>
      <td>${badge(e.status)}</td><td>${esc(e.tier)}</td><td>${usd(e.cost_upper_usd)}/월</td>
      <td class="small">${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.url.replace("https://", ""))}</a>` : "-"}</td>
      ${S.me?.admin ? `<td class="small muted">${esc(e.owner_email || "")}</td>` : ""}</tr>`).join("");
  return shell(h`<div class="page">
    <h1>${S.me?.admin ? "전체 환경" : "내 환경"}</h1>
    <p class="lead">요구사항만 입력하면 플랫폼이 구성을 고르고 실제 AWS 환경을 만들어 검증합니다.</p>
    ${S.envs.length ? `<div class="card"><table><thead><tr><th>이름</th><th>상태</th><th>등급</th><th>최대 예상 비용</th><th>주소</th>${S.me?.admin ? "<th>소유자</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="card result"><div class="icon">☁️</div><h2>첫 환경을 만들어 볼까요?</h2><p class="muted">몇 가지 질문에 답하면 바로 추천해 드려요.</p><a class="btn primary" href="#/new">새 환경 만들기</a></div>`}
  </div>`);
}

function startPage() {
  const quick = S.templates.length > 0;
  $app.innerHTML = shell(h`<div class="page narrow">
    <div class="eyebrow">새 환경</div><h1>어떻게 만들까요?</h1>
    <div class="choices modes">
      <a class="choice mode" href="#/new/custom"><span><span class="c-title">내 앱에 맞게 만들기</span>
        <span class="c-desc">몇 가지 질문에 답하면 비용, 보안, 가용성을 고려해서 알맞은 클라우드 구성을 추천해드려요.</span></span></a>
      ${quick ? `<a class="choice mode" href="#/new/quick"><span><span class="c-title">템플릿으로 시작하기</span>
        <span class="c-desc">만들고 싶은 서비스가 정해져 있다면 준비된 구성으로 바로 시작할 수 있어요.</span></span></a>` : ""}
    </div></div>`);
  bindShell();
}

function templatePage() {
  const list = S.templates.map((t) => h`
    <button class="choice" data-template="${esc(t.template_id)}"><span class="radio" aria-hidden="true"></span>
      <span><span class="c-title">${esc(t.title)}</span><span class="c-desc">${esc(t.short_description)}</span></span></button>`).join("");
  $app.innerHTML = shell(h`<div class="page narrow">
    <div class="eyebrow">템플릿으로 시작하기</div><h1>무엇을 만들까요?</h1>
    <p class="lead">목적만 고르면 나머지 요구사항은 준비된 값으로 채워요. 검토 화면에서 언제든 바꿀 수 있어요.</p>
    <div class="choices">${list}</div>
    <div class="wizard-foot"><a class="btn lg back" href="#/new">이전</a></div></div>`);
  bindShell();
  document.querySelectorAll("[data-template]").forEach((b) => (b.onclick = () => {
    S.wizard = templateWizard(S.templates.find((t) => t.template_id === b.dataset.template));
    location.hash = "#/new/quick/form";
  }));
}

function quickFormPage() {
  const w = S.wizard;
  const t = quickTemplate(w);
  if (!t) { location.hash = "#/new/quick"; return; }
  const tiers = [["DEV", "개발 · 테스트용", "7일 뒤 자동 삭제(연장 가능)"], ["PROD", "실제 운영용", "관리자 승인 후 생성, 자동 삭제 없음"]].filter(([v]) => t.supported_tiers.includes(v));
  $app.innerHTML = shell(h`<div class="page narrow">
    <div class="eyebrow">템플릿으로 시작하기</div><h1 id="q-title">${esc(t.title)}</h1>
    <p class="lead">${esc(t.short_description)}</p>
    <div class="field"><label for="name">환경 이름</label><input class="input" id="name" maxlength="60" placeholder="예: 내 포트폴리오" value="${esc(w.name)}" required>
      <span class="help">목록에서 알아보기 쉬운 이름이면 돼요. (60자 이내)</span></div>
    <div class="field"><span class="label" id="tier-label">환경 용도</span><div class="choices" role="radiogroup" aria-labelledby="tier-label">
      ${tiers.map(([v, tt, d]) => `<button class="choice ${w.tier === v ? "selected" : ""}" role="radio" aria-checked="${w.tier === v}" data-tier="${v}"><span class="radio" aria-hidden="true"></span><span><span class="c-title">${tt}</span><span class="c-desc">${d}</span></span></button>`).join("")}
    </div></div>
    <div class="field"><label for="budget">한 달 최대 예산 (USD, 선택)</label><input class="input" id="budget" type="number" inputmode="numeric" min="0" max="10000" step="1" value="${esc(w.budget ?? "")}" placeholder="${esc(t.default_requirements.budget_usd_month)}">
      <span class="help">비워 두면 기본값 $${esc(t.default_requirements.budget_usd_month)}로 봐요.</span></div>
    <button class="choice ${w.spa ? "selected" : ""}" role="checkbox" aria-checked="${w.spa}" id="spa"><span class="radio check" aria-hidden="true"></span><span>
      <span class="c-title">React·Vue 같은 SPA 라우터를 써요</span><span class="c-desc">주소창에 직접 입력한 하위 경로도 index.html로 연결합니다.</span></span></button>
    <p class="muted small" style="margin-top:16px">사이트 파일(${esc(t.required_artifact.label)})은 다음 검토 화면에서 올려요.</p>
    <div class="wizard-foot"><a class="btn lg back" href="#/new/quick">이전</a><button class="btn primary lg" id="next">검토하기</button></div></div>`);
  bindShell();
  const capture = () => { w.name = document.getElementById("name").value; w.budget = document.getElementById("budget").value; };
  document.querySelectorAll("#name, #budget").forEach((el) => el.addEventListener("input", () => (w.dirty = true)));
  document.querySelectorAll("[data-tier]").forEach((b) => (b.onclick = () => { capture(); w.tier = b.dataset.tier; w.dirty = true; quickFormPage(); }));
  document.getElementById("spa").onclick = () => { capture(); w.spa = !w.spa; w.dirty = true; quickFormPage(); };
  document.getElementById("next").onclick = () => {
    capture();
    const errs = [];
    if (!w.name.trim()) errs.push(["name", "환경 이름을 입력해 주세요."]);
    if (w.budget !== "" && (!/^\d+$/.test(String(w.budget)) || +w.budget > 10000)) errs.push(["budget", "예산은 0~10,000 사이의 정수로 입력해 주세요."]);
    if (showErrors(errs)) return;
    location.hash = "#/new/review";
  };
}

function showErrors(errs) {
  document.querySelectorAll(".input.error").forEach((el) => { el.classList.remove("error"); el.removeAttribute("aria-invalid"); });
  document.querySelectorAll(".help.error.inline").forEach((el) => el.remove());
  errs.forEach(([id, msg]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add("error");
    el.setAttribute("aria-invalid", "true");
    const m = document.createElement("span");
    m.className = "help error inline";
    m.id = `${id}-err`;
    m.setAttribute("role", "alert");
    m.textContent = msg;
    el.insertAdjacentElement("afterend", m);
    el.setAttribute("aria-describedby", `${id}-err`);
  });
  if (errs.length) document.getElementById(errs[0][0])?.focus();
  return errs.length > 0;
}

function visibleQuestions(w) { return QUESTIONS.filter((q) => !q.when || q.when(w)); }

function questionPage() {
  const w = S.wizard;
  if (!w) { location.hash = "#/new"; return; }
  const qs = visibleQuestions(w);
  w.step = Math.min(w.step, qs.length - 1);
  const q = qs[w.step];
  const answered = q.custom || q.multi || w[q.key] != null;
  let body;
  if (q.custom) {
    body = h`
      <div class="field"><label for="name">환경 이름</label><input class="input" id="name" maxlength="60" placeholder="예: 내 포트폴리오" value="${esc(w.name)}" required>
        <span class="help">목록에서 알아보기 쉬운 이름이면 돼요. (60자 이내)</span></div>
      <div class="field"><span class="label" id="tier-label">용도</span><div class="choices" role="radiogroup" aria-labelledby="tier-label">
        ${[["DEV", "개발 · 테스트용", "7일 뒤 자동 삭제(연장 가능)"], ["PROD", "실제 운영용", "관리자 승인 후 생성, 자동 삭제 없음"]].map(([v, t, d]) =>
          `<button class="choice ${w.tier === v ? "selected" : ""}" role="radio" aria-checked="${w.tier === v}" data-tier="${v}"><span class="radio" aria-hidden="true"></span><span><span class="c-title">${t}</span><span class="c-desc">${d}</span></span></button>`).join("")}
      </div></div>
      <div class="field"><label for="budget">한 달 최대 예산 (USD)</label><input class="input" id="budget" type="number" inputmode="numeric" min="0" max="10000" step="1" value="${esc(w.budget ?? "")}">
        <span class="help">예상 비용이 이 금액을 넘으면 조정안을 보여 드려요. 원하면 초과를 확인하고 그대로 진행할 수도 있어요. 비워 두면 $10로 봅니다.</span></div>
      ${w.app_form !== "SERVER_APP" ? `<button class="choice ${w.spa ? "selected" : ""}" role="checkbox" aria-checked="${w.spa}" id="spa"><span class="radio check" aria-hidden="true"></span><span>
        <span class="c-title">React·Vue 같은 SPA 라우터를 써요</span><span class="c-desc">주소창에 직접 입력한 하위 경로도 index.html로 연결합니다.</span></span></button>` : ""}`;
  } else {
    const val = w[q.key];
    body = `<div class="choices" role="${q.multi ? "group" : "radiogroup"}" aria-labelledby="q-title">${q.options.map(([v, t, d]) => {
      const sel = q.multi ? val.includes(v) : val === v;
      return `<button class="choice ${sel ? "selected" : ""}" role="${q.multi ? "checkbox" : "radio"}" aria-checked="${sel}" data-v="${esc(v)}"><span class="radio ${q.multi ? "check" : ""}" aria-hidden="true"></span><span><span class="c-title">${esc(t)}</span>${d ? `<span class="c-desc">${esc(d)}</span>` : ""}</span></button>`;
    }).join("")}</div>`;
    if (q.key === "exposure" && w.exposure === "IP_ALLOWLIST") {
      body += h`<div class="field" style="margin-top:16px"><label for="cidrs">허용할 IP 주소 범위</label>
        <input class="input" id="cidrs" placeholder="예: 203.0.113.0/24, 198.51.100.7/32" value="${esc(w.cidrs)}">
        <span class="help" id="cidrs-help">회사·학교 네트워크 담당자에게 받은 주소를 쉼표로 구분해 적어 주세요. 한 대만 허용하려면 끝에 /32를 붙입니다.</span></div>`;
    }
  }
  $app.innerHTML = shell(h`<div class="page narrow">
    <div class="progress" role="progressbar" aria-label="질문 진행" aria-valuemin="1" aria-valuemax="${qs.length}" aria-valuenow="${w.step + 1}"><i style="width:${Math.round(((w.step + 1) / (qs.length + 1)) * 100)}%"></i></div>
    <div class="eyebrow">${w.step + 1} / ${qs.length}</div>
    ${w.fromTemplate && w.step === 0 ? `<p class="notice small" data-from-template>‘${esc(w.fromTemplate)}’ 기본값으로 답을 채워 두었어요. 필요한 것만 바꾸세요.</p>` : ""}
    <h1 id="q-title">${esc(q.title)}</h1>${q.desc ? `<p class="lead">${esc(q.desc)}</p>` : '<div style="height:20px"></div>'}
    ${body}
    ${q.custom ? "" : examplesBlock(q, w)}
    <div class="wizard-foot">
      <button class="btn lg back" id="back">이전</button>
      <button class="btn primary lg" id="next" ${answered ? "" : "disabled"}>${q.custom ? "추천 받기" : "다음"}</button>
    </div></div>`);
  bindShell();
  document.querySelectorAll("[data-v]").forEach((b) => (b.onclick = () => {
    if (q.multi) {
      const set = new Set(w[q.key]);
      set.has(b.dataset.v) ? set.delete(b.dataset.v) : set.add(b.dataset.v);
      w[q.key] = [...set];
    } else w[q.key] = b.dataset.v;
    w.dirty = true;
    w.lastAdjust = null;
    w.budgetOverride = null;
    questionPage();
  }));
  document.getElementById("examples")?.addEventListener("toggle", (e) => (w.examplesOpen[q.key] = e.target.open));
  document.querySelectorAll("[data-tier]").forEach((b) => (b.onclick = () => { capture(); w.tier = b.dataset.tier; w.dirty = true; questionPage(); }));
  const spa = document.getElementById("spa");
  if (spa) spa.onclick = () => { capture(); w.spa = !w.spa; questionPage(); };
  document.querySelectorAll("#name, #budget, #cidrs").forEach((el) => el.addEventListener("input", () => (w.dirty = true)));
  function capture() {
    const n = document.getElementById("name"); if (n) w.name = n.value;
    const bu = document.getElementById("budget"); if (bu) w.budget = bu.value;
    const c = document.getElementById("cidrs"); if (c) w.cidrs = c.value;
  }
  document.getElementById("cidrs")?.addEventListener("input", (e) => (w.cidrs = e.target.value));
  document.getElementById("back").onclick = () => { capture(); if (w.step === 0) location.hash = "#/new"; else { w.step--; questionPage(); } };
  document.getElementById("next").onclick = () => {
    capture();
    if (q.custom) {
      const errs = [];
      if (!w.name.trim()) errs.push(["name", "환경 이름을 입력해 주세요."]);
      if (w.budget !== "" && w.budget !== null && (!/^\d+$/.test(String(w.budget)) || +w.budget > 10000)) errs.push(["budget", "예산은 0~10,000 사이의 정수로 입력해 주세요."]);
      if (showErrors(errs)) return;
      location.hash = "#/new/review";
      return;
    }
    if (q.key === "exposure" && w.exposure === "IP_ALLOWLIST") {
      const list = w.cidrs.split(/[\s,]+/).filter(Boolean);
      const bad = list.filter((c) => !CIDR_RE.test(c));
      if (showErrors(!list.length ? [["cidrs", "허용할 IP 주소 범위를 하나 이상 입력해 주세요."]] : bad.length ? [["cidrs", `형식이 맞지 않아요: ${bad.join(", ")} (예: 203.0.113.0/24)`]] : [])) return;
    }
    w.step++;
    questionPage();
  };
}

function reasonList(items, cls = "") {
  return `<ul class="reasons">${(items || []).map((r) => `<li class="${cls}"><span class="dot"></span><span>${esc(r.message)}${codeTag(r.code)}</span></li>`).join("")}</ul>`;
}

// The request path each blueprint actually builds: Static = CloudFront → S3 (D-31), Serverless = HTTP API → Lambda
// running the image + Lambda Web Adapter (D-165, no CloudFront), Container = CloudFront → VPC origin → internal ALB → Fargate (D-33).
const PATH_NODE = {
  CF: ["CloudFront", "HTTPS · 보안 헤더"], HTTP_API: ["API Gateway (HTTP API)", "HTTPS 주소"],
  ALB: ["내부 ALB", "CloudFront로만 연결 (VPC Origin)"], IMAGE_LWA: ["컨테이너 이미지", "+ Lambda Web Adapter"],
  LAMBDA: ["Lambda", "요청 시에만 실행"], LAMBDA_PC: ["Lambda (미리 준비)", "첫 요청 지연 없음"],
};

function archPath(a) {
  if (a.frontend === "STATIC") return [PATH_NODE.CF, ["S3 (비공개)", "화면 파일"], ...(a.compute !== "NONE" ? [NODE[a.compute]] : [])];
  if (a.compute === "SLS" || a.compute === "SLS_PC")
    return [PATH_NODE.HTTP_API, a.compute === "SLS" ? PATH_NODE.LAMBDA : PATH_NODE.LAMBDA_PC, PATH_NODE.IMAGE_LWA];
  if (a.compute === "CTR") return [PATH_NODE.CF, PATH_NODE.ALB, NODE.CTR];
  return [];
}

function archDiagram(a) {
  if (!a) return "";
  const nodes = [["사용자", "브라우저"], ...archPath(a)];
  if (a.db !== "NONE") nodes.push(NODE[a.db]);
  return `<div class="arch">${nodes.map((n, i) => `${i ? '<span class="edge">→</span>' : ""}<div class="node"><b>${esc(n[0])}</b><span>${esc(n[1])}</span></div>`).join("")}</div>`;
}

function mainExclusions(rec) {
  // "주요 탈락 후보": the best-placed excluded candidate per compute family
  const seen = new Set();
  const out = [];
  for (const c of rec.candidates || []) {
    if (c.status === "SELECTED") continue;
    const fam = c.candidate_id.split("+")[0];
    if (seen.has(fam) || fam === rec.architecture.compute) continue;
    seen.add(fam);
    out.push(c);
  }
  return out;
}

function reviewerPanel(rec, env) {
  if (!isReviewer() || !rec?.reasons) return "";
  const pr = rec.pricing || {};
  const cand = (rec.candidates || []).map((c) => `<tr><td class="mono small">${esc(c.candidate_id)}</td><td>${esc(c.status)}</td><td>${esc(c.stage || "-")}</td>
    <td class="mono small">${esc((c.reasons || []).map((r) => r.code).join(", "))}</td><td>${c.cost_rep_usd !== undefined ? usd(c.cost_rep_usd) : "-"}</td></tr>`).join("");
  const reasons = [...(rec.reasons || []), ...(rec.warnings || []), ...(rec.policies || [])]
    .map((r) => `<tr><td class="mono small">${esc(r.code)}</td><td class="mono small">${esc(JSON.stringify(r.params || {}))}</td></tr>`).join("");
  return h`<details class="card reviewer"><summary><b>검토자용 상세</b> <span class="muted small">reason code · 후보 비교 · 규칙/가격 버전</span></summary>
    <dl class="kv" style="margin-top:12px">
      <dt>rules_version</dt><dd class="mono small">${esc(rec.rules_version)}</dd>
      <dt>input_hash</dt><dd class="mono small">${esc(rec.input_hash)}</dd>
      <dt>가격표</dt><dd class="mono small">${esc(pr.snapshot_id || rec.pricing_snapshot_id)} · ${esc(pr.region || "")} · ${esc(pr.updated_at || "")} · ${esc(pr.source || "")}</dd>
      ${env ? `<dt>env_id</dt><dd class="mono small">${esc(env.env_id)}</dd>` : ""}
      ${rec.origin ? `<dt>origin</dt><dd class="mono small">${esc(JSON.stringify(rec.origin))}</dd>` : ""}
      ${env?.budget_override ? `<dt>budget_override</dt><dd class="mono small">${esc(JSON.stringify(env.budget_override))}</dd>` : ""}
    </dl>
    <h3 style="margin-top:14px">후보 (${(rec.candidates || []).length})</h3>
    <table class="stack"><thead><tr><th>candidate</th><th>status</th><th>stage</th><th>reason</th><th>대표 비용</th></tr></thead><tbody>${cand}</tbody></table>
    <h3 style="margin-top:14px">reason code</h3>
    <table class="stack"><tbody>${reasons}</tbody></table></details>`;
}

function pricingNote(rec) {
  const pr = rec?.pricing || {};
  return `예상치예요. ${esc(pr.updated_at || "")} 기준 ${pr.source === "manual" ? "수동 입력" : esc(pr.source || "")} 가격표(${esc(pr.region || "ap-northeast-2")})와 사용량 가정으로 계산해서 실제 청구액과 다를 수 있어요.`;
}

function costCard(rec) {
  const c = rec.cost;
  const max = Math.max(c.budget_usd || 1, c.upper_usd || 1);
  const over = c.upper_usd > c.budget_usd;
  return h`<div class="card">
    <h2>예상 비용 <span class="badge">추정치</span></h2>
    <div class="cost-big">${usd(c.rep_usd)} ~ ${usd(c.upper_usd)}<span class="muted small"> / 월</span></div>
    <div class="meter"><b style="left:0;width:${(c.upper_usd / max) * 100}%"></b><i class="${over ? "over" : ""}" style="width:${(c.rep_usd / max) * 100}%"></i></div>
    <div class="small muted">예산 ${usd(c.budget_usd)} · 고정비 ${usd(c.fixed_usd)} · 사용량에 따라 최대 ${usd(c.variable_upper_usd)}</div>
    <p class="notice small" style="margin-top:12px">${pricingNote(rec)}</p>
    <details style="margin-top:12px"><summary>항목별 보기 (최대 기준)</summary>
      <table><tbody>${c.line_items_upper.map((i) => `<tr><td class="mono">${esc(i.resource)}</td><td>${i.kind === "FIXED" ? "고정" : "사용량"}${i.shared ? " · 공유" : ""}</td><td style="text-align:right">$${(i.cents / 100).toFixed(2)}</td></tr>`).join("")}</tbody></table>
    </details></div>`;
}

function checksPlanned(rec) {
  const items = rec.production_ready_checks || [];
  if (!items.length) return "";
  const groups = LAYER_ORDER.map((layer) => [layer, items.filter((c) => c.layer === layer)]).filter(([, g]) => g.length);
  return h`<div class="card"><h2>만든 뒤 자동으로 확인하는 것 <span class="count">${items.length}개</span></h2>
    <p class="muted small">모두 통과해야 ‘사용 가능’이 됩니다. 보안 항목이 하나라도 실패하면 접속을 바로 막습니다.</p>
    <div class="layer-grid">${groups.map(([layer, g]) => h`
      <div class="layer"><h3>${esc(LAYER_DESC[layer])} <span class="count">${g.length}</span></h3>
        <ul class="ticks">${g.map((c) => `<li><span>${esc(c.title)}</span>${codeTag(c.id)}</li>`).join("")}</ul></div>`).join("")}
    </div></div>`;
}

function baselineCard(rec) {
  const plain = Object.fromEntries(S.baseline.map((b) => [b.id, b.plain || b.title]));
  const ids = rec.security_baseline || [];
  return h`<div class="card"><h2>항상 적용되는 보안 기준 <span class="count">${ids.length}개</span></h2>
    <p class="muted small">비용을 줄이려고 빼는 일은 없어요.</p>
    <ul class="ticks two">${ids.map((id) => `<li><span>${esc(plain[id] || id)}</span>${codeTag(id)}</li>`).join("")}</ul></div>`;
}

const CAUSE_QUESTION = { traffic_scale: "traffic_scale", request_duration: "request_duration", data: "data", capabilities: "capabilities" };

function answerLabel(key, value) {
  const q = QUESTIONS.find((x) => x.key === key);
  return q?.options?.find(([v]) => v === value)?.[1] || value;
}

function gateBlock(rec, w) {
  const causes = (rec.causes || []).map((c) => {
    const key = CAUSE_QUESTION[c.field];
    const q = QUESTIONS.find((x) => x.key === key);
    const reason = rec.reasons.find((r) => r.code === c.code);
    return h`<div class="cause"><p><b>“${esc(answerLabel(key, c.value))}”</b>${q ? `<span class="muted"> (${esc(q.title)})</span>` : ""}을(를) 선택했어요.</p>
      <p class="muted small">${esc(reason?.message || "")}${codeTag(c.code)}</p>
      ${key ? `<button class="btn" data-jump="${esc(key)}">이 답변 바꾸기</button>` : ""}</div>`;
  }).join("");
  const alts = (rec.alternatives || []).map((a) => `<li>${esc(a.message)}${codeTag(a.code)}</li>`).join("");
  const near = rec.nearest?.candidate_id ? `<p class="notice small" style="margin-top:12px">${esc(rec.nearest.note || "가까운 대안")}: ${esc(archLabel(candidateArch(rec.nearest.candidate_id)))} · 최대 월 ${usd(rec.nearest.cost_upper_usd)} (추정치)</p>` : "";
  return h`<div class="card result warn"><div class="icon">!</div><h2>${rec.status === "UNSUPPORTED" ? "아직 지원하지 않는 요구사항이에요" : "확인이 필요한 부분이 있어요"}</h2></div>
    <div class="card"><h2>지원하지 않는 이유</h2>${causes || reasonList(rec.reasons, "warn")}
      ${alts ? `<h3 style="margin-top:16px">가능한 대안</h3><ul class="alt-list">${alts}</ul>` : ""}${near}</div>`;
}

function candidateArch(candidateId) {
  const [compute, db] = candidateId.split("+");
  return { frontend: S.wizard?.app_form === "SERVER_APP" ? "NONE" : "STATIC", compute, db, candidate_id: candidateId };
}

function budgetBlock(rec, w) {
  const c = rec.cost;
  const opts = (rec.budget.relax_options || []).map((o, i) => {
    const rows = o.changes.map((ch) => `<tr><td>${esc(ch.label)}</td><td>${esc(ch.from_label)}</td><td><b>${esc(ch.to_label)}</b></td></tr>`).join("");
    const arch = o.candidate_id ? `<tr><td>구성</td><td>${esc(archLabel(rec.architecture))}</td><td><b>${esc(archLabel(candidateArch(o.candidate_id)))}${o.multi_az === "SINGLE" ? " (단일 가용 영역)" : ""}</b></td></tr>` : "";
    return h`<div class="option" data-option="${i}">
      <table class="compare"><thead><tr><th></th><th>현재</th><th>제안</th></tr></thead><tbody>${rows}${arch}
        <tr><td>예상 비용</td><td>월 ${usd(c.rep_usd)}~${usd(c.upper_usd)}</td><td><b>${o.cost_rep_usd !== undefined ? `월 ${usd(o.cost_rep_usd)}~` : "최대 월 "}${usd(o.cost_upper_usd)}</b></td></tr></tbody></table>
      <p class="small ${o.fits_budget ? "" : "muted"}">${esc(o.reason.message)}${codeTag(o.reason.code)}</p>
      <button class="btn ${o.fits_budget ? "primary" : ""}" data-apply="${i}">이 변경 적용</button></div>`;
  }).join("");
  return h`<div class="card" id="budget-card"><h2>예산을 넘어요</h2>${reasonList(rec.budget.reasons, "bad")}
    <p class="muted small">입력한 예산은 목표값이에요. 요구사항을 조정하거나, 확인 후 초과 상태로 진행할 수 있어요.</p>
    <div class="btn-row" style="margin-top:12px"><button class="btn primary" id="show-adjust">예산에 맞게 요구사항 조정</button>
      ${w.budgetOverride ? "" : `<button class="btn" id="override">예산 초과로 진행</button>`}</div>
    <div id="adjust-panel" hidden><h3 style="margin-top:18px">이렇게 바꿀 수 있어요</h3>
      <p class="muted small">아직 아무것도 바뀌지 않았어요. ‘이 변경 적용’을 눌러야 요구사항이 바뀌고 다시 추천해요. 기능과 보안 기준은 낮추지 않아요.</p>${opts}</div></div>`;
}

function adjustedBanner(adj, rec) {
  const rows = adj.changes.map((ch) => `<tr><td>${esc(ch.label)}</td><td>${esc(ch.from_label)}</td><td><b>${esc(ch.to_label)}</b></td></tr>`).join("");
  return h`<div class="card adjusted" data-adjusted><h2>요구사항을 바꿨어요</h2>
    <table class="compare"><thead><tr><th></th><th>이전</th><th>지금</th></tr></thead><tbody>${rows}
      <tr><td>구성</td><td>${esc(adj.beforeArch)}</td><td><b>${esc(archLabel(rec.architecture))}</b></td></tr>
      <tr><td>예상 비용</td><td>월 ${usd(adj.beforeRep)}~${usd(adj.beforeUpper)}</td><td><b>월 ${usd(rec.cost.rep_usd)}~${usd(rec.cost.upper_usd)}</b></td></tr></tbody></table>
    <p class="muted small">${esc(adj.reason)}</p></div>`;
}

function bindReviewActions(rec, w) {
  document.querySelectorAll("[data-jump]").forEach((b) => (b.onclick = () => {
    const idx = visibleQuestions(w).findIndex((q) => q.key === b.dataset.jump);
    if (idx >= 0) w.step = idx;
    location.hash = "#/new/q";
  }));
  const tc = document.getElementById("to-custom");
  if (tc) tc.onclick = () => { w.mode = "custom"; w.step = 0; w.dirty = true; location.hash = "#/new/q"; };
  const show = document.getElementById("show-adjust");
  if (show) show.onclick = () => { document.getElementById("adjust-panel").hidden = false; show.disabled = true; };
  document.querySelectorAll("[data-apply]").forEach((b) => (b.onclick = () => {
    const o = rec.budget.relax_options[Number(b.dataset.apply)];
    w.lastAdjust = { changes: o.changes, beforeArch: archLabel(rec.architecture), beforeRep: rec.cost.rep_usd,
      beforeUpper: rec.cost.upper_usd, reason: o.reason.message };
    o.changes.forEach((ch) => { if (ch.field === "budget_usd_month") w.budget = String(ch.to); else w[ch.field] = ch.to; });
    const t = quickTemplate(w);
    if (t && o.changes.some((ch) => !t.editable_fields.includes(ch.field))) w.mode = "custom"; // no longer the template's requirements
    w.budgetOverride = null;
    w.dirty = true;
    reviewPage();
  }));
  const ov = document.getElementById("override");
  if (ov) ov.onclick = async () => {
    const c = rec.cost;
    const ok = await modal({ title: "예산을 넘는 구성으로 진행할까요?", confirm: "초과 승인하고 계속", cancel: "취소",
      body: h`<dl class="kv"><dt>입력한 예산</dt><dd>월 ${usd(c.budget_usd)}</dd><dt>현재 예상 비용</dt><dd>월 ${usd(c.rep_usd)}~${usd(c.upper_usd)} (추정치)</dd>
        <dt>예상 초과</dt><dd><b>약 ${usd(Math.max(0, c.rep_usd - c.budget_usd))}~${usd(c.upper_usd - c.budget_usd)} / 월</b></dd></dl>
        <p style="margin-top:12px">예상 비용이 입력한 예산보다 높습니다. 그래도 이 구성으로 진행하시겠습니까?</p>
        <p class="muted small">플랫폼 전체 상한, 사용자별 환경 수, 보안 기준은 이 승인으로 바뀌지 않아요.</p>` });
    if (!ok) return;
    w.budgetOverride = { cost_upper_usd: c.upper_usd, at: new Date().toISOString() };
    reviewPage();
  };
}

const TIER_POLICY = [
  ["DEV", "개발 · 테스트용", "승인하면 바로 만들어요", "7일 뒤 자동 삭제 (연장 가능)", "기본 접속·보안 검사"],
  ["PROD", "실제 운영용", "관리자 승인 후 만들어요", "자동 삭제 없음", "기본 검사 + 장애 알림(모니터링) 확인"],
];

function quickReview(t, rec, w, blocked) {
  const ttl = w.tier === "DEV" ? "만든 날로부터 7일 뒤 자동 삭제돼요. 필요하면 7일씩 연장할 수 있어요." : "운영 환경은 자동으로 삭제되지 않아요.";
  return h`
    ${w.lastAdjust ? adjustedBanner(w.lastAdjust, rec) : ""}
    <div class="eyebrow">템플릿으로 시작하기 · 검토</div><h1>${esc(t.title.replace(/ 만들기$/, ""))}</h1>
    <p class="lead">${esc(w.name)} · ${w.tier === "PROD" ? "실제 운영용" : "개발·테스트용"}</p>
    <div class="card" data-quick-review><h2>사용할 구성</h2>
      <ul class="components">${t.components.map((c) => `<li><b>${esc(c.service)}</b><span aria-hidden="true">→</span><span>${esc(c.role)}</span></li>`).join("")}</ul>
      <details style="margin-top:12px"><summary>이렇게 고른 이유</summary>${reasonList(rec.reasons)}
        ${rec.policies?.length ? `<h3 style="margin-top:12px">적용되는 설정</h3>${reasonList(rec.policies)}` : ""}</details>
      <div class="btn-row" style="margin-top:14px"><button class="btn" id="to-custom">세부 요구사항 바꾸기</button></div></div>
    ${costCard(rec)}
    ${blocked ? budgetBlock(rec, w) : ""}
    ${baselineCard(rec)}${checksPlanned(rec)}
    <div class="card"><h2>DEV / PROD 정책</h2>
      <table class="stack"><thead><tr><th>용도</th><th>만들 때</th><th>자동 삭제(TTL)</th><th>검사</th></tr></thead><tbody>
      ${TIER_POLICY.map(([v, n, a, l, c]) => `<tr${v === w.tier ? ' class="current"' : ""}><td data-th="용도"><b>${n}</b>${v === w.tier ? ' <span class="chip ok">이 환경</span>' : ""}</td><td data-th="만들 때">${a}</td><td data-th="자동 삭제">${l}</td><td data-th="검사">${c}</td></tr>`).join("")}
      </tbody></table>
      <p class="small" style="margin-top:10px" data-ttl><b>TTL</b> · ${ttl}</p></div>
    ${reviewerPanel(rec)}`;
}

async function reviewPage() {
  const w = S.wizard;
  if (!w) { location.hash = "#/new"; return; }
  $app.innerHTML = shell(loadingPage("요구사항을 분석하고 있어요"));
  bindShell();
  let rec;
  const route = location.hash;
  try { rec = await api("POST", "/recommendations", requirementsBody(w)); }
  catch (e) { $app.innerHTML = shell(`<div class="page"><div class="notice bad">${esc(e.message)}</div><p><a href="#/new/q">돌아가기</a></p></div>`); bindShell(); return; }
  if (location.hash !== route || !S.wizard) return; // stale response
  S.wizard.rec = rec;
  const ok = rec.status === "RECOMMENDED" || rec.status === "RECOMMENDED_WITH_WARNINGS";
  const tpl = quickTemplate(w);
  let top;
  if (rec.status === "UNSUPPORTED" || rec.status === "NEEDS_INPUT") {
    top = gateBlock(rec, w);
  } else {
    if (rec.status !== "OVER_BUDGET" || (w.budgetOverride && w.budgetOverride.cost_upper_usd !== rec.cost.upper_usd)) w.budgetOverride = null;
    const blocked = rec.status === "OVER_BUDGET";
    if (tpl) top = quickReview(tpl, rec, w, blocked);
    else top = h`
      ${w.lastAdjust ? adjustedBanner(w.lastAdjust, rec) : ""}
      <div class="eyebrow">추천 결과</div><h1>${esc(runModel(rec.architecture) || "이")} 구성을 추천해요</h1>
      <p class="lead">${esc(w.name)} · ${w.tier === "PROD" ? "실제 운영용" : "개발·테스트용"}</p>
      <div class="card"><h2>구성</h2>${archDiagram(rec.architecture)}<h3>이렇게 고른 이유</h3>${reasonList(rec.reasons)}
        ${rec.policies?.length ? `<h3 style="margin-top:16px">적용되는 설정</h3>${reasonList(rec.policies)}` : ""}
        ${rec.warnings?.length ? `<h3 style="margin-top:16px">알아둘 점</h3>${reasonList(rec.warnings, "warn")}` : ""}</div>
      ${mainExclusions(rec).length ? `<div class="card"><h2>고르지 않은 구성</h2><table><tbody>${mainExclusions(rec).map((c) =>
        `<tr><td style="width:200px">${esc((NODE[c.candidate_id.split("+")[0]] || ["정적 사이트만"])[0])}</td><td>${esc(c.reasons[0]?.message || "")}</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${costCard(rec)}
      ${blocked ? budgetBlock(rec, w) : ""}
      ${baselineCard(rec)}${checksPlanned(rec)}${reviewerPanel(rec)}`;
  }
  const overridden = rec.status === "OVER_BUDGET" && !!w.budgetOverride;
  const canProvision = (ok || overridden) && rec.provisionable;
  const isImage = artifactKind(rec) === "IMAGE";
  $app.innerHTML = shell(h`<div class="page">${top}
    <div class="section card">
      ${canProvision ? h`${isImage ? imageForm(rec) : `<h2>사이트 파일 올리기</h2><p class="muted small">빌드 결과 폴더(index.html 포함)를 zip으로 묶어 올려 주세요. 50MB 이하.</p>
        <label class="drop" id="drop" for="file"><input type="file" id="file" class="sr-only" accept=".zip,application/zip"><span id="droptext">여기에 zip 파일을 끌어 놓거나 눌러서 고르세요</span></label>`}
        <div class="help error" id="err" role="alert" style="margin-top:10px"></div>
        ${overridden ? `<p class="notice warn" style="margin-top:12px" data-override>예산 초과를 확인했어요 · 예산 월 ${usd(rec.cost.budget_usd)} → 최대 월 ${usd(rec.cost.upper_usd)}</p>` : ""}
        <div class="action-bar"><span class="action-summary"><b>최대 ${usd(rec.cost.upper_usd)}/월</b><span class="muted small" id="filestate">${isImage ? "이미지 주소를 입력해 주세요" : "파일을 골라 주세요"}</span></span>
          <a class="btn lg back" href="${tpl ? "#/new/quick/form" : "#/new/q"}">수정</a><button class="btn primary lg" id="approve" disabled>${w.tier === "PROD" && !S.me?.admin ? "관리자에게 승인 요청" : tpl ? "이 구성으로 만들기" : "승인하고 만들기"}</button></div>`
      : h`<div class="notice ${ok ? "warn" : "bad"}">${ok ? "이 구성은 설계되어 있지만 아직 플랫폼에서 만들 수 없어요. 지금은 DB 없는 정적 호스팅, 요청 단위 실행(Lambda), 상시 실행 컨테이너(Fargate)만 만들 수 있어요."
        : rec.status === "OVER_BUDGET" ? (rec.provisionable ? "예산에 맞게 조정하거나, 예산 초과를 확인하면 파일을 올릴 수 있어요." : "이 구성은 아직 플랫폼에서 만들 수 없어요. 지금은 DB 없는 정적 호스팅, 요청 단위 실행(Lambda), 상시 실행 컨테이너(Fargate)만 만들 수 있어요.")
        : "위에서 원인이 된 답변을 바꾼 뒤 다시 추천받아 주세요."}</div>
        <div class="wizard-foot"><a class="btn lg" href="#/new/q">요구사항 수정</a></div>`}
    </div></div>`);
  bindShell();
  bindReviewActions(rec, w);
  if (!canProvision) return;
  let file = null;
  let image = null;
  if (isImage) {
    const field = document.getElementById("image");
    const sync = () => {
      document.getElementById("approve").disabled = !field.value.trim();
      document.getElementById("filestate").textContent = field.value.trim() || "이미지 주소를 입력해 주세요";
    };
    field.addEventListener("input", sync);
    sync();
  }
  const drop = document.getElementById("drop") || { classList: { add() {}, remove() {} } };
  const pick = (f) => {
    if (!f) return;
    const err = document.getElementById("err");
    err.textContent = "";
    if (!/\.zip$/i.test(f.name)) { err.textContent = "zip 파일만 올릴 수 있어요."; return; }
    if (f.size > 50 * 1024 * 1024) { err.textContent = "50MB 이하의 파일만 올릴 수 있어요."; return; }
    if (f.size === 0) { err.textContent = "빈 파일이에요."; return; }
    file = f;
    drop.classList.add("done");
    document.getElementById("droptext").textContent = `✓ ${f.name} (${(f.size / 1024).toFixed(0)} KB) · 다른 파일을 고르려면 누르세요`;
    document.getElementById("filestate").textContent = f.name;
    document.getElementById("approve").disabled = false;
  };
  if (!isImage) document.getElementById("file").onchange = (e) => pick(e.target.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
  document.getElementById("approve").onclick = async (ev) => {
    const btn = ev.currentTarget;
    if (isImage) {
      const parsed = readImageForm();
      if (parsed.error) { document.getElementById("err").textContent = parsed.error; return; }
      image = parsed.image;
    }
    const needsAdmin = w.tier === "PROD" && !S.me?.admin;
    const ok = await modal({
      title: needsAdmin ? "관리자에게 승인을 요청할까요?" : "이 구성으로 만들까요?",
      confirm: needsAdmin ? "승인 요청" : tpl ? "이 구성으로 만들기" : "승인하고 만들기",
      body: h`<dl class="kv">
        <dt>구성</dt><dd>${esc(archLabel(rec.architecture))}</dd>
        <dt>예상 비용</dt><dd>월 ${usd(rec.cost.rep_usd)} ~ 최대 ${usd(rec.cost.upper_usd)} (예산 ${usd(rec.cost.budget_usd)}) · 추정치${overridden ? " · <b>예산 초과 승인됨</b>" : ""}</dd>
        <dt>보안 기준</dt><dd>${rec.security_baseline.length}개 항목 항상 적용</dd>
        <dt>자동 검사</dt><dd>${(rec.production_ready_checks || []).length}개 항목 · 보안 실패 시 접속 차단</dd>
        <dt>자동 삭제</dt><dd>${w.tier === "DEV" ? "7일 뒤 (연장 가능)" : "없음 · 관리자 승인 필요"}</dd>
        ${isImage ? `<dt>앱 이미지</dt><dd class="mono small">${esc(image.image)}</dd><dt>그다음</dt><dd>원본은 그대로 두고 Lambda용 이미지를 따로 만들어 배포해요</dd>`
          : `<dt>올릴 파일</dt><dd>${esc(file.name)}</dd>`}</dl>
        <p class="small muted" style="margin-top:14px">승인하면 실제 AWS 리소스가 만들어지고 비용이 발생합니다.${CFG.localMode ? " (로컬 모드에서는 만들지 않습니다)" : ""}</p>`,
    });
    if (!ok) return;
    btn.disabled = true;
    btn.textContent = "만드는 중…";
    const err = document.getElementById("err");
    try {
      const created = await api("POST", "/environments", { name: w.name.trim(), ...requirementsBody(w), budget_override: overridden,
        ...(isImage ? { image } : {}) });
      if (!isImage) await uploadZip(created.upload, file);
      try { await api("POST", `/environments/${created.environment.env_id}/approve`); }
      catch (e) {
        const detail = failedTitles(e.data?.artifact) || e.data?.blocked?.map((b) => b.message).join(" ") || "";
        S.wizard = null;
        await refreshEnvs();
        location.hash = `#/env/${created.environment.env_id}`;
        toast(`${e.message} ${detail}`.trim());
        return;
      }
      S.wizard = null;
      await refreshEnvs();
      location.hash = `#/env/${created.environment.env_id}`;
    } catch (e) {
      const detail = (e.data?.artifact?.checks || []).filter((c) => c.status === "FAIL").map((c) => `${S.checkTitles[c.check_id] || c.check_id}: ${c.detail}`).join(" · ");
      document.getElementById("err").textContent = `${e.message} ${detail}`.trim();
      btn.disabled = false;
      btn.textContent = "다시 시도";
    }
  };
}

// Serverless input (D-166): a public HTTP container image; the platform builds the Lambda image from it.
function artifactKind(rec) {
  if (!rec?.provisionable) return null;
  return rec.artifact_kind || (rec.architecture?.compute === "NONE" ? "STATIC_ZIP" : null);
}

// Container runs all the time behind a shared internal ALB: say what that costs before approval (§9.3 INFO_SHARED_MARGINAL_COST).
function containerCostNote(rec) {
  const items = rec.cost?.line_items_upper || [];
  const alb = items.find((i) => i.resource === "alb");
  const n = (rec.reasons || []).find((r) => r.code === "INFO_SHARED_MARGINAL_COST")?.params?.n || 4;
  const tasks = items.find((i) => i.resource === "fargate-min-tasks");
  return `<div class="notice warn small">컨테이너는 요청이 없어도 항상 실행돼서 고정비가 있어요${tasks ? ` (컨테이너 월 약 $${Math.round(tasks.cents / 100)})` : ""}.
    공유 부하분산기(내부 ALB)는 컨테이너 환경들이 나눠 쓰는 것으로 계산했어요${alb ? `. 지금 컨테이너 환경이 하나도 없다면 실제로는 월 약 $${Math.round((alb.cents * n) / 100)}이 더 들고, 마지막 컨테이너 환경을 지우면 함께 삭제돼요` : ""}.
    처음 만들 때는 공유 인프라 준비로 20분 정도 걸릴 수 있어요.</div>`;
}

function imageForm(rec) {
  const container = rec?.blueprint === "container";
  return h`<h2>앱 컨테이너 이미지</h2>
    <p class="muted small">공개 저장소(docker.io, public.ecr.aws, ghcr.io, quay.io)에 있는 HTTP 서버 이미지를 적어 주세요. 앱은 <span class="mono">PORT</span> 환경변수(8080)로 요청을 받아야 해요.
      ${container ? "플랫폼이 원본 이미지를 그대로 환경 전용 저장소로 복사해 ECS Fargate에서 실행해요."
        : "플랫폼이 원본은 그대로 두고, Lambda에서 실행할 수 있게 Lambda Web Adapter를 얹은 이미지를 따로 만들어요."}</p>
    ${container ? containerCostNote(rec) : ""}
    <div class="field"><label for="image">이미지 주소 (태그 또는 digest 포함)</label>
      <input class="input mono" id="image" placeholder="예: docker.io/traefik/whoami:v1.10.3" autocomplete="off" spellcheck="false"></div>
    <div class="field"><label for="health">상태 확인 경로</label>
      <input class="input mono" id="health" value="/" spellcheck="false"><span class="help">이 경로가 200으로 응답하면 앱이 준비된 것으로 봐요.</span></div>
    <div class="field"><label for="envvars">환경변수 (한 줄에 하나, KEY=VALUE)</label>
      <textarea class="input mono" id="envvars" rows="3" placeholder="WHOAMI_PORT_NUMBER=8080" spellcheck="false"></textarea>
      <span class="help">비밀번호·토큰 같은 비밀값은 넣지 마세요. 아직 비밀값 보관(Secrets Manager)을 지원하지 않아서, 비밀값으로 보이면 만들기 전에 막아요.</span></div>`;
}

function readImageForm() {
  const env = {};
  const lines = document.getElementById("envvars").value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  for (const line of lines) {
    const i = line.indexOf("=");
    if (i < 1) return { error: `환경변수는 KEY=VALUE 형식이어야 해요: ${line}` };
    env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return { image: { image: document.getElementById("image").value.trim(), health_path: document.getElementById("health").value.trim() || "/", env } };
}

function progressSteps(env) {
  const redeploy = (env.revisions || []).length > 1 || env.status === "REDEPLOYING";
  const labels = redeploy ? ["새 버전 업로드", "파일 교체 · 캐시 비우기", "보안·동작 검증", "결과"] : ["승인", "AWS 리소스 생성", "보안·동작 검증", "결과"];
  const st = env.status;
  const stage = st === "PENDING_ADMIN_APPROVAL" || st === "DRAFT" ? 0 : ["PROVISIONING", "REDEPLOYING", "REPAIRING"].includes(st) ? 1 : st === "VALIDATING" ? 2 : 3;
  const failedAt = st === "FAILED" ? (env.failure?.stage === "VALIDATE" || env.verdict ? 2 : 1) : -1;
  return `<div class="steps" aria-label="진행 단계">${labels.map((l, i) => {
    let cls = i < stage ? "done" : i === stage ? "now" : "";
    if (failedAt >= 0) cls = i < failedAt ? "done" : i === failedAt ? "fail" : "";
    if (i === 3 && ["READY"].includes(st)) cls = "done";
    if (st === "DEGRADED" && i === 3) cls = "fail";
    return `<div class="step ${cls}" ${cls === "now" ? 'aria-current="step"' : ""}>${esc(l)}</div>`;
  }).join("")}</div>`;
}

function resultBlock(env) {
  const st = env.status;
  const url = env.outputs?.url;
  const map = {
    READY: ["ok", "✓", "환경이 준비됐어요", "모든 보안·동작 검증을 통과했습니다."],
    DEGRADED: ["warn", "!", "접속에 문제가 있어요", "보안 기준은 통과했지만 사이트가 정상 응답하지 않습니다. 업로드한 파일을 확인해 주세요."],
    FAILED: ["bad", "×", env.failure?.stage === "PLAN" ? "보안 기준 위반으로 생성을 막았어요" : env.current_revision || env.failure?.stage === "REDEPLOY" ? "새 버전을 배포하지 못했어요" : "환경을 만들지 못했어요",
      env.verdict?.security_failed ? "보안 검증에 실패해 접속을 차단했습니다." : (env.failure?.message || env.failure?.cause ? "아래 기록을 확인해 주세요." : "")],
    PROVISIONING: ["run", "⋯", "AWS에 리소스를 만들고 있어요", "보통 5분 안팎 걸립니다. 이 화면을 닫아도 계속 진행돼요."],
    VALIDATING: ["run", "⋯", "보안과 동작을 검증하고 있어요", "실제 주소로 접속해 보고, 설정을 하나씩 확인합니다."],
    DELETING: ["run", "⋯", "삭제하고 있어요", "CDN 정리에 몇 분 걸릴 수 있어요."],
    REDEPLOYING: ["run", "⋯", `새 버전 rev-${env.artifact?.revision ?? ""}을 배포하고 있어요`, "인프라는 그대로 두고 사이트 파일만 바꿉니다. 끝나면 다시 검증해요."],
    DELETED: ["", "–", "삭제된 환경이에요", "AWS 리소스를 모두 제거했습니다."],
    DELETE_FAILED: ["bad", "×", "삭제에 실패했어요", "플랫폼이 자동으로 다시 시도합니다."],
    PENDING_ADMIN_APPROVAL: ["warn", "…", "관리자 승인을 기다리고 있어요", "운영 환경은 관리자가 승인하면 만들어집니다."],
    DRAFT: ["warn", "…", "아직 승인되지 않았어요", ""],
    DRIFTED: ["warn", "!", "승인된 구성과 설정이 달라졌어요", env.drift?.endpoint_disabled
      ? "보안 기준을 벗어난 변경이라 접속을 차단해 두었어요. 수정안을 확인하고 복구를 승인해 주세요."
      : "아래 수정안을 확인하고, 맞으면 원래 구성으로 복구를 승인해 주세요."],
    REPAIRING: ["run", "⋯", "원래 구성으로 되돌리고 있어요", "승인한 수정안만 적용하고, 끝나면 다시 검증해요."],
  }[st] || ["", "", st, ""];
  return h`<div class="card result ${map[0]}"><div class="icon">${map[1]}</div><h2>${esc(map[2])}</h2>
    <p class="muted">${esc(map[3])}</p>
    ${url && ["READY", "DEGRADED"].includes(st) ? `<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener">사이트 열기 ↗</a>` : ""}</div>`;
}

function checkLabel(c) {
  if (isLocalSkip(c)) return ["로컬 미실행", ""];
  return { PASS: ["통과", "ok"], FAIL: ["실패", "bad"], DEFERRED: ["다음 단계에서 확인", ""], NOT_APPLICABLE: ["해당 없음", ""] }[c.status];
}

function checksTable(env) {
  const checks = env.checks || [];
  if (!checks.length) return "";
  const order = { FAIL: 0, PASS: 1, DEFERRED: 2, NOT_APPLICABLE: 3 };
  const title = (c) => S.checkTitles[c.check_id] || c.check_id;
  const failed = checks.filter((c) => c.status === "FAIL");
  const dims = [...new Set(checks.map((c) => c.dimension))];
  const chips = dims.map((d) => {
    const all = checks.filter((c) => c.dimension === d && c.status !== "NOT_APPLICABLE" && c.status !== "DEFERRED");
    const bad = all.filter((c) => c.status === "FAIL").length;
    if (!all.length) return "";
    return `<span class="chip ${bad ? "bad" : "ok"}">${esc(DIM_KO[d] || d)} ${all.length - bad}/${all.length}</span>`;
  }).join("");
  const skipped = checks.filter(isLocalSkip).length;
  const rows = [...checks].sort((a, b) => order[a.status] - order[b.status]).map((c) => {
    const [label, cls] = checkLabel(c);
    return `<tr><td data-th="결과"><span class="badge ${cls}">${label}</span></td><td data-th="항목">${esc(title(c))}${isReviewer() ? `<div class="code-tag" style="margin:0">${esc(c.check_id)}</div>` : ""}</td><td data-th="시점">${esc(LAYER_KO[c.layer] || c.layer)}</td><td data-th="상세" class="small muted">${esc(c.detail)}</td></tr>`;
  }).join("");
  const v = env.verdict;
  return h`<div class="card"><h2>검증 결과 ${v ? badge(env.status === "DRIFTED" ? "DRIFTED" : v.verdict) : ""}</h2>
    <div class="chips">${chips}</div>
    ${skipped ? `<p class="notice small">로컬 모드라 AWS가 필요한 검사 ${skipped}개는 실행하지 않았어요. 실제 배포에서는 모두 실행됩니다.</p>` : ""}
    ${failed.length ? h`<h3 style="margin-top:16px">통과하지 못한 항목</h3><ul class="reasons">${failed.map((c) =>
      `<li class="bad"><span class="dot"></span><span><b>${esc(title(c))}</b><br><span class="small muted">${esc(c.detail)}</span>${codeTag(c.check_id)}</span></li>`).join("")}</ul>` : ""}
    <details style="margin-top:16px"><summary>검사 기록 전체 보기 (${checks.length}개)</summary>
    <table class="stack"><thead><tr><th>결과</th><th>항목</th><th>시점</th><th>상세</th></tr></thead><tbody>${rows}</tbody></table></details></div>`;
}

function canRedeploy(env) {
  if (env.recommendation?.architecture?.candidate_id !== "NONE+NONE") return false;
  if (["READY", "DEGRADED"].includes(env.status)) return true;
  return env.status === "FAILED" && (env.failure?.stage === "REDEPLOY" || (env.revisions || []).some((r) => ["READY", "DEGRADED"].includes(r.status)));
}

function revisionCards(env) {
  const revs = (env.revisions || []).slice().sort((a, b) => b.revision - a.revision);
  const can = canRedeploy(env);
  const current = env.current_revision; // the revision actually served, not the one being deployed
  const rows = revs.map((r) => {
    const st = r.status === "DEPLOYING" ? "REDEPLOYING" : r.status;
    const rollback = can && r.revision !== current && ["READY", "DEGRADED"].includes(r.status);
    return `<tr><td data-th="버전"><b>rev-${esc(r.revision)}</b>${r.revision === current ? ' <span class="chip ok">현재</span>' : ""}</td>
      <td data-th="결과">${badge(st)}</td><td data-th="올린 사람" class="small">${esc(r.uploaded_by || "")}</td>
      <td data-th="올린 시각" class="small muted">${fmtTime(r.uploaded_at)}</td>
      <td data-th="">${rollback ? `<button class="btn sm nowrap" data-rollback="${esc(r.revision)}" aria-label="rev-${esc(r.revision)}으로 되돌리기">이 버전으로 되돌리기</button>` : ""}</td></tr>`;
  }).join("");
  const upload = can ? h`<div class="card"><h2>새 버전 배포</h2>
      <p class="muted small">새 빌드 결과(zip)를 올리면 인프라는 그대로 두고 사이트 파일만 바꾼 뒤 다시 검증해요. 비용은 바뀌지 않아요.</p>
      <label class="drop" id="rdrop" for="rfile"><input type="file" id="rfile" class="sr-only" accept=".zip,application/zip"><span id="rdroptext">zip 파일을 끌어 놓거나 눌러서 고르세요</span></label>
      <div class="help error" id="rerr" role="alert" style="margin-top:10px"></div>
      <div class="btn-row" style="margin-top:12px;justify-content:flex-end"><button class="btn primary" id="redeploy" disabled>새 버전 배포하기</button></div></div>` : "";
  if (!revs.length && !upload) return "";
  return h`<div class="section">${upload}
    ${revs.length ? `<div class="card"><h2>배포 기록</h2><table class="stack"><thead><tr><th>버전</th><th>결과</th><th>올린 사람</th><th>올린 시각</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}</div>`;
}

function pickZip(input, label, errEl, onPick) {
  input.onchange = () => {
    const f = input.files[0];
    errEl.textContent = "";
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) { errEl.textContent = "zip 파일만 올릴 수 있어요."; return; }
    if (f.size > 50 * 1024 * 1024) { errEl.textContent = "50MB 이하의 파일만 올릴 수 있어요."; return; }
    if (f.size === 0) { errEl.textContent = "빈 파일이에요."; return; }
    label.closest(".drop").classList.add("done");
    label.textContent = `✓ ${f.name} (${(f.size / 1024).toFixed(0)} KB)`;
    onPick(f);
  };
}

function draftUpload(env) {
  const failed = env.artifact && !env.artifact.passed;
  return h`<div class="section card" data-draft-upload><h2>${failed ? "파일 다시 올리기" : "사이트 파일 바꾸기"}</h2>
    <p class="muted small">아직 만들기 전이라 올린 파일을 바꿀 수 있어요. 새 zip을 다시 검사하고, 통과하면 바로 승인으로 이어져요.</p>
    <label class="drop" id="ddrop" for="dfile"><input type="file" id="dfile" class="sr-only" accept=".zip,application/zip"><span id="ddroptext">zip 파일을 끌어 놓거나 눌러서 고르세요</span></label>
    <div class="help error" id="derr" role="alert" style="margin-top:10px"></div>
    <div class="btn-row" style="margin-top:12px;justify-content:flex-end"><button class="btn primary" id="dupload" disabled>이 파일로 다시 검사하고 승인하기</button></div></div>`;
}

function bindDraftUpload(id) {
  const input = document.getElementById("dfile");
  if (!input) return;
  let file = null;
  const btn = document.getElementById("dupload");
  pickZip(input, document.getElementById("ddroptext"), document.getElementById("derr"), (f) => { file = f; btn.disabled = false; });
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "올리는 중…";
    try {
      const u = await api("POST", `/environments/${id}/upload-url`);
      await uploadZip(u.upload, file);
      await api("POST", `/environments/${id}/approve`);
      toast("새 파일이 검사를 통과했어요.");
    } catch (e) { toast(`${e.message} ${failedTitles(e.data?.artifact)}`.trim()); }
    await refreshEnvs();
    envPage(id, { quiet: true });
  };
}

function bindRedeploy(env, id) {
  const input = document.getElementById("rfile");
  if (input) {
    let file = null;
    const btn = document.getElementById("redeploy");
    pickZip(input, document.getElementById("rdroptext"), document.getElementById("rerr"), (f) => { file = f; btn.disabled = false; });
    btn.onclick = async () => {
      const next = Math.max(0, ...(env.revisions || []).map((r) => r.revision)) + 1;
      if (!(await modal({ title: `새 버전 rev-${next}을 배포할까요?`, confirm: "배포하기",
        body: h`<dl class="kv"><dt>올릴 파일</dt><dd>${esc(file.name)}</dd><dt>바뀌는 것</dt><dd>사이트 파일만 (인프라·비용 변화 없음)</dd>
          <dt>그다음</dt><dd>보안·동작을 다시 검사해요. 실패하면 이전 버전으로 되돌릴 수 있어요.</dd></dl>` }))) return;
      btn.disabled = true;
      btn.textContent = "올리는 중…";
      try {
        const u = await api("POST", `/environments/${id}/upload-url`);
        await uploadZip(u.upload, file);
        await api("POST", `/environments/${id}/redeploy`, {});
        toast(`rev-${u.revision} 배포를 시작했어요.`);
      } catch (e) {
        const detail = failedTitles(e.data?.artifact);
        document.getElementById("rerr").textContent = `${e.message} ${detail}`.trim();
        btn.disabled = false;
        btn.textContent = "새 버전 배포하기";
        return;
      }
      await refreshEnvs();
      envPage(id, { quiet: true });
    };
  }
  document.querySelectorAll("[data-rollback]").forEach((b) => (b.onclick = async () => {
    const rev = Number(b.dataset.rollback);
    if (!(await modal({ title: `rev-${rev}으로 되돌릴까요?`, confirm: "되돌리기",
      body: `<p>그때 올렸던 파일로 사이트를 바꾸고 다시 검증해요. 인프라는 바뀌지 않아요.</p>` }))) return;
    b.disabled = true;
    try { await api("POST", `/environments/${id}/redeploy`, { revision: rev }); toast(`rev-${rev}으로 되돌리는 중이에요.`); }
    catch (e) { toast(e.message); }
    await refreshEnvs();
    envPage(id, { quiet: true });
  }));
}

async function envPage(id, { quiet = false } = {}) {
  clearTimeout(S.poll);
  const seq = (S.envSeq = (S.envSeq || 0) + 1); // only the newest request may render (no stale overwrite)
  if (!quiet) { $app.innerHTML = shell(loadingPage("환경 정보를 불러오고 있어요")); bindShell(); }
  let env;
  const route = location.hash;
  try { env = (await api("GET", `/environments/${id}`)).environment; }
  catch (e) { $app.innerHTML = shell(`<div class="page"><div class="notice bad">${esc(e.message)}</div></div>`); bindShell(); return; }
  if (location.hash !== route || seq !== S.envSeq) return; // stale response
  const rec = env.recommendation || {};
  const st = env.status;
  const actions = [];
  if (st === "PENDING_ADMIN_APPROVAL" && S.me?.admin) actions.push(`<button class="btn primary" data-act="approve">관리자 승인</button>`);
  if (st === "DRAFT") actions.push(`<button class="btn primary" data-act="approve">승인하고 만들기</button>`);
  if (st === "FAILED" && ((env.failure?.stage !== "PLAN" && !env.verdict?.security_failed) || env.failure?.stage === "REDEPLOY"))
    actions.push(`<button class="btn" data-act="retry">다시 시도</button>`);
  if (st === "DRIFTED") actions.push(`<button class="btn primary" data-act="repair" ${repairReady(env) ? "" : "disabled"}>원래 구성으로 복구</button>`);
  if (["READY", "DEGRADED", "DRIFTED"].includes(st)) actions.push(`<button class="btn" data-act="recheck">다시 검사</button>`);
  if (env.tier === "DEV" && ["READY", "DEGRADED", "DRIFTED"].includes(st)) actions.push(`<button class="btn" data-act="extend">7일 연장</button>`);
  if (["DRAFT", "PENDING_ADMIN_APPROVAL", "READY", "DEGRADED", "DRIFTED", "FAILED", "DELETE_FAILED"].includes(st)) actions.push(`<button class="btn danger" data-act="delete">삭제</button>`);
  const events = (env.event_log || []).slice().reverse().map((e) => `<li><span class="muted">${fmtTime(e.at)}</span><span>${e.status ? badge(e.status) + " " : ""}${esc(e.message)}</span></li>`).join("");
  $app.innerHTML = shell(h`<div class="page">
    <div style="display:flex;align-items:flex-start;gap:12px;justify-content:space-between;flex-wrap:wrap">
      <div><div class="eyebrow mono">${esc(env.env_id)}</div><h1>${esc(env.name)} ${badge(st)}</h1></div>
      <div class="btn-row">${actions.join("")}</div></div>
    ${progressSteps(env)}
    <div role="status" aria-live="polite">${resultBlock(env)}</div>
    <div class="section grid2">
      <div class="card"><h2>환경 정보</h2><dl class="kv">
        <dt>주소</dt><dd>${env.outputs?.url ? `<a href="${esc(env.outputs.url)}" target="_blank" rel="noopener">${esc(env.outputs.url)}</a>` : "-"}</dd>
        <dt>구성</dt><dd>${esc(archLabel(rec.architecture))}</dd>
        <dt>등급</dt><dd>${env.tier === "PROD" ? "실제 운영용" : "개발·테스트용"}</dd>
        <dt>최대 예상 비용</dt><dd>${usd(env.cost_upper_usd)}/월 <span class="muted small">(추정치)</span></dd>
        <dt>자동 삭제</dt><dd>${env.expires_at ? fmtTime(env.expires_at) : "없음"}</dd>
        <dt>만든 사람</dt><dd>${esc(env.owner_email || "")}${env.created_via === "agent" ? ` <span class="badge" data-agent>Claude Code agent</span>` : ""}</dd>
        <dt>승인</dt><dd>${esc(env.approved_by || "-")} ${env.approved_at ? "· " + fmtTime(env.approved_at) : ""}</dd>
        ${env.artifact?.kind === "IMAGE" ? `<dt>앱 이미지</dt><dd class="mono small">${esc(env.artifact.image)}</dd>` : ""}
        <dt>현재 버전</dt><dd>${env.current_revision ? `rev-${esc(env.current_revision)}` : "-"}</dd>
        ${env.budget_override ? `<dt>예산 초과 승인</dt><dd data-override-detail>예산 월 ${usd(env.budget_override.budget_usd)} → 최대 월 ${usd(env.budget_override.cost_upper_usd)} · ${esc(env.budget_override.confirmed_by)} · ${fmtTime(env.budget_override.at)}</dd>` : ""}
      </dl></div>
      <div class="card"><h2>추천 이유</h2>${archDiagram(rec.architecture)}${reasonList((rec.reasons || []).slice(0, 6))}</div>
    </div>
    ${env.failure ? `<div class="section card"><h2>실패 기록</h2><dl class="kv"><dt>단계</dt><dd>${esc(env.failure.stage)}</dd>${env.failure.message ? `<dt>내용</dt><dd>${esc(env.failure.message)}</dd>` : ""}${env.failure.error ? `<dt>오류 (관리자)</dt><dd class="mono small">${esc(env.failure.error)}</dd>` : ""}${env.failure.cause ? `<dt>상세 (관리자)</dt><dd class="mono small">${esc(env.failure.cause).slice(0, 1200)}</dd>` : ""}</dl></div>` : ""}
    ${st === "DRAFT" && env.created_via === "agent" ? `<p class="section notice warn" data-agent-draft>Claude Code agent가 저장소를 분석해 만든 초안이에요. 아직 아무것도 만들어지지 않았어요. 위 구성·비용·추천 이유를 확인하고, 맞으면 직접 <b>승인하고 만들기</b>를 눌러 주세요.</p>` : ""}
    ${driftCard(env)}${lastRepairCard(env)}
    ${env.admission?.blocked?.length ? `<div class="section card"><h2>승인이 막힌 이유</h2>${reasonList(env.admission.blocked, "bad")}</div>` : ""}
    ${["DRAFT", "PENDING_ADMIN_APPROVAL"].includes(st) && env.artifact && !env.artifact.passed ? `<div class="section">${artifactHelp(env.artifact)}</div>` : ""}
    ${st === "DRAFT" && artifactKind(rec) === "STATIC_ZIP" ? draftUpload(env) : ""}
    ${revisionCards(env)}
    <div class="section">${checksTable(env)}</div>
    ${reviewerPanel(rec, env) ? `<div class="section">${reviewerPanel(rec, env)}</div>` : ""}
    <div class="section card"><h2>진행 기록</h2><ul class="timeline">${events}</ul></div>
  </div>`);
  bindShell();
  bindRedeploy(env, id);
  bindDraftUpload(id);
  document.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const act = b.dataset.act;
    if (act === "delete" && !(await modal({ title: "이 환경을 삭제할까요?", confirm: "삭제", danger: true,
      body: `<p><b>${esc(env.name)}</b>의 사이트와 AWS 리소스를 모두 삭제합니다. 되돌릴 수 없어요.</p>` }))) return;
    if (act === "repair" && !(await modal({ title: "원래 구성으로 되돌릴까요?", confirm: "복구",
      body: `<p>승인된 구성과 달라진 리소스 ${env.repair_plan.changes}개를 아래처럼 되돌립니다. 콘솔에서 일부러 바꾼 설정이라면 함께 되돌아가요.</p>
        <ul class="mono small">${repairResources(env).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` }))) return;
    b.disabled = true;
    try {
      if (act === "delete") await api("DELETE", `/environments/${id}`);
      else await api("POST", `/environments/${id}/${act}`);
      toast({ delete: "삭제를 시작했어요.", approve: "승인했어요.", retry: "다시 시도합니다.", extend: "7일 연장했어요.",
        repair: "복구를 시작했어요.", recheck: "다시 검사하고 있어요. 잠시 뒤 결과가 보여요." }[act]);
    } catch (e) { toast(`${e.message} ${failedTitles(e.data?.artifact)}`.trim()); }
    await refreshEnvs();
    envPage(id, { quiet: true });
  }));
  clearTimeout(S.poll);
  const waitingPlan = st === "DRIFTED" && !repairReady(env) && !env.repair_plan?.error;  // the plan arrives in a minute
  if ((IN_PROGRESS.has(st) || waitingPlan) && location.hash === `#/env/${id}`) {
    S.poll = setTimeout(async () => { await refreshEnvs(); if (location.hash === `#/env/${id}`) envPage(id, { quiet: true }); }, CFG.localMode ? 1500 : 5000);
  }
}

// ------------------------------------------------------------------ Day-2 drift (D-186)
const repairReady = (env) => !!(env.repair_plan?.stacks?.length && env.repair_plan.passed && env.repair_plan.changes);
const repairResources = (env) => (env.repair_plan?.stacks || []).flatMap((s) => (s.changes || []).map((c) => `${c.address} (${c.actions.join("/")})`));
const seconds = (n) => (n == null ? "-" : n < 120 ? `${n}초` : `${Math.round(n / 60)}분`);

function driftCard(env) {
  const d = env.drift;
  if (!d || env.status !== "DRIFTED") return "";
  const plan = env.repair_plan;
  const rows = (d.failed || []).map((f) => `<li><b>${esc(S.checkTitles[f.check_id] || f.check_id)}</b>${codeTag(f.check_id)}<span class="muted small"> — ${esc(f.detail || "")}</span></li>`).join("");
  const planBody = repairReady(env)
    ? `<p>리소스 ${plan.changes}개를 승인된 구성으로 되돌립니다. 보안 검사(PLAN)를 통과한 수정안이에요.</p><ul class="mono small">${repairResources(env).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>${plan.replaces?.length ? `<p class="notice warn small">다시 만들어지는 리소스가 있어요: ${esc(plan.replaces.join(", "))}</p>` : ""}`
    : plan?.error ? `<p class="notice bad small">${esc(plan.error)}</p>`
    : plan && !plan.changes ? `<p class="notice warn small">Terraform으로 되돌릴 차이가 없어요. Terraform이 관리하지 않는 부분이 바뀌었을 수 있어 관리자 확인이 필요해요.</p>`
    : plan && !plan.passed ? `<p class="notice bad small">수정안이 보안 검사를 통과하지 못했어요: ${esc((plan.stacks || []).flatMap((s) => s.failed_checks || []).join(", "))}</p>`
    : `<p class="muted">원래 구성으로 되돌리는 수정안을 만들고 있어요. 보통 1~2분 걸립니다.</p>`;
  return h`<div class="section grid2" data-drift>
    <div class="card"><h2>무엇이 달라졌나요</h2><ul class="reasons">${rows}</ul>
      <dl class="kv"><dt>감지 방법</dt><dd>${esc({ EVENT: "변경 이벤트(CloudTrail)", SCHEDULE: "주기 검사", MANUAL: "다시 검사" }[d.trigger] || d.trigger)}</dd>
        ${d.event_name ? `<dt>변경</dt><dd class="mono small">${esc(d.event_name)}</dd>` : ""}
        ${d.actor ? `<dt>바꾼 사람</dt><dd class="mono small">${esc(d.actor)}</dd>` : ""}
        <dt>바뀐 시각</dt><dd>${fmtTime(d.event_time || d.detected_at)}</dd>
        <dt>감지까지</dt><dd>${seconds(d.detect_seconds)}</dd></dl></div>
    <div class="card" data-repair-plan><h2>수정안</h2>${planBody}</div></div>`;
}

function lastRepairCard(env) {
  const r = env.last_repair;
  if (!r || !["READY", "DEGRADED"].includes(env.status)) return "";
  return h`<p class="section notice small" data-last-repair>마지막 복구: ${fmtTime(r.repaired_at)} · 감지까지 ${seconds(r.detect_seconds)} · 승인 대기 ${seconds(r.wait_seconds)} · 승인 후 복구 완료까지 ${seconds(r.apply_seconds)} · 승인 ${esc(r.approved_by || "-")}</p>`;
}

// ------------------------------------------------------------------ router
function bindShell() {
  const l = document.getElementById("logout");
  if (l) l.onclick = (e) => { e.preventDefault(); auth.logout(); };
}

async function render() {
  clearTimeout(S.poll);
  const route = location.hash || "#/";
  if (!CFG.apiUrl) { $app.innerHTML = `<div class="page"><div class="notice bad">config.js가 없습니다. 플랫폼을 배포한 뒤 접속해 주세요.</div></div>`; return; }
  if (route === "#/login" || !auth.tokens) return loginPage();
  if (route === "#/new") return startPage();
  if (route === "#/new/custom") { S.wizard = blankWizard(); location.replace("#/new/q"); return; }
  if (route === "#/new/quick") return templatePage();
  if (route === "#/new/quick/form") return quickFormPage();
  if (route === "#/new/q") return questionPage();
  if (route === "#/new/review") return reviewPage();
  if (route.startsWith("#/env/")) return envPage(route.slice(6));
  $app.innerHTML = shell(loadingPage("불러오고 있어요"));
  bindShell();
  await refreshEnvs();
  if ((location.hash || "#/") !== route) return; // user navigated away while loading
  $app.innerHTML = homePage();
  bindShell();
}

async function boot() {
  if (!auth.tokens) return render();
  try {
    const [me, cat] = await Promise.all([api("GET", "/me"), api("GET", "/presets")]);
    S.me = me;
    S.templates = cat.templates || [];
    S.baseline = cat.baseline;
    S.checkTitles = cat.check_titles || {};
    await refreshEnvs();
  } catch (e) { if (!auth.tokens) return render(); toast(e.message); }
  render();
}

// Leaving the wizard with unsaved answers asks once (D-162). Covers the logo, sidebar links and in-page links.
document.addEventListener("click", async (e) => {
  const a = e.target.closest?.('a[href^="#/"]');
  if (!a || !location.hash.startsWith("#/new") || a.getAttribute("href").startsWith("#/new") || !S.wizard?.dirty) return;
  e.preventDefault();
  if (await modal({ title: "작성 중인 내용이 사라질 수 있어요.", confirm: "나가기", cancel: "계속 작성",
    body: "<p>지금까지 고른 답변이 저장되지 않아요. 그래도 이동할까요?</p>" })) {
    S.wizard = null;
    location.hash = a.getAttribute("href");
  }
});

window.addEventListener("hashchange", render);
boot();
