"""Build the 10-minute proposal presentation (docs/proposal/proposal-presentation.pptx) from the final proposal.

Usage: python scripts/build_presentation.py [output.pptx]
Screenshot slots on the demo slide are dashed boxes; replace them with real screenshots in PowerPoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "proposal" / "proposal-presentation.pptx"

FONT = "Malgun Gothic"
DARK, TEAL, AMBER, RED = "1E2B32", "1F9E8F", "E8A33D", "C8553D"
TINT, TEXT, MUTED, LINE, WHITE = "EEF4F3", "1B2329", "5E6B71", "D5DFDD", "FFFFFF"
TOTAL = 10


def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h)


def box(slide, x, y, w, h, fill=None, line=None, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08, dash=False):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = rgb(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(1.25)
        if dash:
            s.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    else:
        s.line.fill.background()
    s.shadow.inherit = False
    return s


def text(slide, x, y, w, h, paras, size=16, color=TEXT, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, margin=0.0, space_after=0, shape=None):
    """paras: str | list of str | list of list of (text, {opts}) runs."""
    if shape is None:
        shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for side in ("left", "right", "top", "bottom"):
        setattr(tf, f"margin_{side}", Inches(margin))
    if isinstance(paras, str):
        paras = [paras]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        runs = [(para, {})] if isinstance(para, str) else para
        for t, o in runs:
            r = p.add_run()
            r.text = t
            f = r.font
            f.name = FONT
            f.size = Pt(o.get("size", size))
            f.bold = o.get("bold", bold)
            f.color.rgb = rgb(o.get("color", color))
    return shape


def circle_num(slide, x, y, n, d=0.5, fill=TEAL, size=16):
    c = box(slide, x, y, d, d, fill=fill, shape=MSO_SHAPE.OVAL)
    text(slide, 0, 0, 0, 0, str(n), size=size, color=WHITE, bold=True, align=PP_ALIGN.CENTER,
         anchor=MSO_ANCHOR.MIDDLE, shape=c)
    return c


def content_slide(prs, idx, kicker, title, notes):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = rgb(WHITE)
    text(s, 0.6, 0.35, 12, 0.35, kicker, size=14, color=TEAL, bold=True)
    text(s, 0.6, 0.68, 12.1, 0.7, title, size=30, color=TEXT, bold=True)
    text(s, 0.6, 7.0, 8, 0.3, "Self-Service Cloud Platform · 이진서", size=10, color=MUTED)
    text(s, 11.7, 7.0, 1.0, 0.3, f"{idx} / {TOTAL}", size=10, color=MUTED, align=PP_ALIGN.RIGHT)
    s.notes_slide.notes_text_frame.text = notes
    return s


def dark_slide(prs, notes):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = rgb(DARK)
    s.notes_slide.notes_text_frame.text = notes
    return s


def table(slide, x, y, w, col_w, rows, size=13, row_h=0.5, header_fill=DARK, first_bold=True, highlight=None):
    shp = slide.shapes.add_table(len(rows), len(rows[0]), Inches(x), Inches(y), Inches(w), Inches(row_h * len(rows)))
    tbl = shp.table
    tbl.first_row = True
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = Inches(cw)
    for i, row in enumerate(rows):
        tbl.rows[i].height = Inches(row_h)
        for j, val in enumerate(row):
            cell = tbl.cell(i, j)
            cell.fill.solid()
            if i == 0:
                cell.fill.fore_color.rgb = rgb(header_fill)
            elif highlight and i in highlight:
                cell.fill.fore_color.rgb = rgb("FDF3E3")
            else:
                cell.fill.fore_color.rgb = rgb(WHITE if i % 2 else TINT)
            cell.margin_left = cell.margin_right = Inches(0.1)
            cell.margin_top = cell.margin_bottom = Inches(0.04)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            r = p.add_run()
            r.text = val
            r.font.name = FONT
            r.font.size = Pt(size)
            r.font.bold = i == 0 or (first_bold and j == 0)
            r.font.color.rgb = rgb(WHITE if i == 0 else TEXT)
    return shp


def keep_korean_words(prs) -> None:
    """Wrap Korean at word boundaries (eaLnBrk=0), like CSS word-break: keep-all; PowerPoint splits mid-word otherwise."""
    for slide in prs.slides:
        for shp in slide.shapes:
            frames = [shp.text_frame] if shp.has_text_frame else []
            if shp.has_table:
                frames += [c.text_frame for row in shp.table.rows for c in row.cells]
            for tf in frames:
                for p in tf.paragraphs:
                    p._p.get_or_add_pPr().set("eaLnBrk", "0")
                    for r in p.runs:
                        r._r.get_or_add_rPr().set("lang", "ko-KR")


def build(out: Path = OUT) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    # 1. Title ---------------------------------------------------------------------------------------------------
    s = dark_slide(prs, (
        "[0:20] 안녕하세요, 트랙 D Platform Engineering의 이진서입니다.\n"
        "제 프로젝트는 개발자가 AWS 서비스를 고르는 대신 앱의 요구사항에 답하면, 플랫폼이 구조를 추천하고 "
        "실제로 만들고 검증해서 안전한 환경만 넘겨주는 Self-Service Cloud Platform입니다.\n"
        "오늘은 제안서를 바탕으로 문제, 요구사항 형식, 지표, 리스크 순서로 말씀드리고 마지막에 피드백 받고 싶은 점을 여쭙겠습니다."))
    text(s, 0.8, 0.9, 8, 0.4, "WorldSkills Cloud Computing 프로젝트 트랙 · D. Platform Engineering", size=14, color="7FC8BE", bold=True)
    text(s, 0.8, 1.8, 7.6, 2.4, ["앱 요구사항에 답하면", "검증된 AWS 환경을 받는 플랫폼"], size=36, color=WHITE, bold=True, space_after=4)
    text(s, 0.8, 3.55, 7.4, 0.5, "Self-Service Cloud Platform — 제안서 1차 발표", size=18, color="C9D6D3")
    text(s, 0.8, 6.2, 7.4, 0.4, "이진서  ·  Reviewer 선수 2번, 3번  ·  2026-10", size=14, color="9FB1AD")
    for i, (name, desc) in enumerate([("Static", "S3 + CloudFront"), ("Serverless", "Lambda + API Gateway"),
                                      ("Container", "ECS Fargate + 내부 ALB")]):
        y = 1.9 + i * 1.35
        box(s, 8.9, y, 3.7, 1.1, fill="2A3B44")
        circle_num(s, 9.15, y + 0.3, i + 1, d=0.5)
        text(s, 9.85, y + 0.17, 2.7, 0.4, name, size=18, color=WHITE, bold=True)
        text(s, 9.85, y + 0.57, 2.7, 0.4, desc, size=14, color="B5C4C1")

    # 2. Problem -------------------------------------------------------------------------------------------------
    s = content_slide(prs, 2, "01 · 문제 정의", "같은 웹 서비스라도 맞는 AWS 구조는 다르다", (
        "[1:00] AWS에 앱 하나를 올리려면 compute, 네트워크, IAM, 로그, 비용 구조를 개발자가 다 정해야 합니다.\n"
        "같은 웹 서비스라도 정적 사이트, 짧은 API, 연결을 오래 유지하는 서버는 맞는 구조가 다릅니다(왼쪽).\n"
        "이걸 사람마다 따로 판단하면 환경마다 보안 수준이 달라지고 고정비나 과도한 권한이 남습니다.\n"
        "더 큰 문제는 '만들었다'와 '안전하게 쓸 수 있다'가 다르다는 점입니다. 저도 이 플랫폼을 만들면서 로컬 테스트는 다 통과했는데 "
        "실제 AWS에서만 드러난 문제로 배포가 7번 멈췄습니다.\n"
        "범위는 지난번 말씀드린 대로 HTTP로 요청을 받는 웹 애플리케이션으로 한정합니다. 배치나 worker, HTTP가 아닌 서버는 엔진이 '지원 불가'로 안내합니다."))
    text(s, 0.6, 1.6, 5.4, 0.4, "앱 특성에 따라 맞는 구조", size=16, color=MUTED, bold=True)
    for i, (app, arch) in enumerate([("정적 사이트 · SPA", "S3 + CloudFront"),
                                     ("짧고 불규칙한 API", "Lambda"),
                                     ("연결을 오래 유지하는 서버", "Container (Fargate)")]):
        y = 2.1 + i * 1.05
        box(s, 0.6, y, 2.75, 0.85, fill=TINT)
        text(s, 0.6, y, 2.75, 0.85, app, size=15, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        text(s, 3.4, y, 0.45, 0.85, "→", size=20, color=MUTED, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        box(s, 3.9, y, 2.2, 0.85, fill=TEAL)
        text(s, 3.9, y, 2.2, 0.85, arch, size=15, color=WHITE, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    problems = [("판단이 사람마다 다름", "환경마다 구조와 보안 수준이 제각각"),
                ("만들었다 ≠ 안전하게 쓸 수 있다", "로컬은 통과했는데 실제 AWS에서만 7번 실패"),
                ("쓰지 않는 비용·권한이 남음", "고정비, 과도한 IAM 권한, 지우지 않은 리소스")]
    for i, (h, d) in enumerate(problems):
        y = 1.6 + i * 1.22
        box(s, 6.75, y, 5.95, 1.05, fill=WHITE, line=LINE)
        circle_num(s, 6.95, y + 0.27, i + 1, fill=RED if i == 1 else DARK)
        text(s, 7.65, y + 0.13, 4.9, 0.4, h, size=17, bold=True)
        text(s, 7.65, y + 0.53, 4.9, 0.4, d, size=14, color=MUTED)
    box(s, 0.6, 5.5, 12.1, 1.2, fill="FDF3E3")
    text(s, 0.85, 5.6, 11.6, 1.0, [
        [("범위  ", {"bold": True, "color": "9A6412"}), ("HTTP(S)로 요청을 받는 웹 애플리케이션 — 정적 사이트 · API · 실시간 웹", {"bold": True})],
        [("배치 · worker · HTTP가 아닌 서버(게임, MQTT 등)는 범위 밖이며, 엔진이 UNSUPPORTED와 이유를 안내", {"color": MUTED})]],
        size=16, anchor=MSO_ANCHOR.MIDDLE, space_after=4)

    # 3. Flow ----------------------------------------------------------------------------------------------------
    s = content_slide(prs, 3, "02 · 해결 방법", "요구사항 → 추천 → 승인 → 검증된 환경 → 회수", (
        "[1:00] 전체 흐름입니다. 개발자는 요구사항 질문에 답하고, 추천 엔진이 구조와 예상 비용을 보여줍니다.\n"
        "사람이 승인해야만 실제 AWS가 바뀝니다. 승인하면 Step Functions가 CodeBuild에서 Terraform을 실행합니다.\n"
        "핵심은 아래 줄입니다. 만들기 전 plan을 검사하고, 만든 뒤 실제 AWS 설정을 검사하고, 마지막으로 외부에서 HTTPS로 접속해 봅니다.\n"
        "세 검사를 모두 통과해야 READY가 되고, 하나라도 어기면 FAILED와 실패 단계, 원인이 남습니다. 보안 위반이면 바로 차단합니다.\n"
        "다 쓰면 삭제하고, 남은 리소스가 0인지 다시 조회해서 확인합니다."))
    rows = [("요청과 승인", [("개발자", "요구사항 답변"), ("추천 엔진", "규칙 기반 + 비용"), ("사람 승인", "예산 · quota 확인"),
                         ("Terraform 실행", "Step Functions · CodeBuild")], 4),
            ("검사와 판정", [("PLAN 검사", "만들기 전"), ("Blueprint 생성", "3종 중 하나"), ("STATE 검사", "실제 AWS 설정"),
                         ("HTTP 검사", "외부 접속"), ("READY", "사용 가능")], 5)]
    for r, (label, nodes, n) in enumerate(rows):
        y = 1.65 + r * 1.75
        text(s, 0.6, y, 4, 0.35, label, size=14, color=MUTED, bold=True)
        gap = 0.45
        w = (12.1 - gap * (n - 1)) / n
        for k, (h, d) in enumerate(nodes):
            x = 0.6 + k * (w + gap)
            ready = h == "READY"
            box(s, x, y + 0.4, w, 1.05, fill=TEAL if ready else TINT)
            text(s, x, y + 0.48, w, 0.45, h, size=17, bold=True, color=WHITE if ready else TEXT, align=PP_ALIGN.CENTER)
            text(s, x, y + 0.9, w, 0.4, d, size=14, color=WHITE if ready else MUTED, align=PP_ALIGN.CENTER)
            if k < n - 1:
                text(s, x + w, y + 0.4, gap, 1.05, "→", size=20, color=MUTED, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    for i, (h, d, c) in enumerate([("하나라도 기준을 어기면", "READY 대신 FAILED + 실패 단계와 원인 기록, 보안 위반은 즉시 차단", RED),
                                   ("다 쓰면 회수", "삭제 후 state · IAM · ECR · log group을 다시 조회해 남은 리소스 0 확인", DARK)]):
        x = 0.6 + i * 6.15
        box(s, x, 5.35, 5.95, 1.3, fill=WHITE, line=LINE)
        text(s, x + 0.3, 5.47, 5.4, 0.4, h, size=17, bold=True, color=c)
        text(s, x + 0.3, 5.9, 5.4, 0.7, d, size=14, color=MUTED)

    # 4. Requirement form ------------------------------------------------------------------------------------------
    s = content_slide(prs, 4, "03 · 요구사항 형식", "질문마다 바뀌는 결정이 있다", (
        "[1:30] 지난번에 요구사항 형식이 가장 중요하다고 말씀해 주셔서, 질문마다 무엇을 결정하는지로 정리했습니다.\n"
        "개발자는 AWS 용어가 아니라 자기 앱의 특징으로 답합니다. 예를 들어 '가장 오래 걸리는 요청'이 30초를 넘으면 "
        "API Gateway의 30초 한도 때문에 Lambda는 후보에서 빠집니다.\n"
        "실시간 연결, 서버 메모리 상태, 큰 파일 중 하나라도 있으면 Container로 갑니다.\n"
        "특정 IP만 허용해야 하면, API Gateway HTTP API에는 WAF를 붙일 수 없어서 Serverless를 제외합니다.\n"
        "이 질문들이 실제 앱을 충분히 표현하는지는 아직 증명하지 않았고, 그래서 다음 단계로 공개 웹 앱 조사를 하려고 합니다."))
    table(s, 0.6, 1.55, 12.1, [3.55, 4.25, 4.3], [
        ("질문 (개발자가 보는 말)", "선택지", "결정되는 것"),
        ("앱 형태", "화면만 / 서버 앱 / 화면 + API", "Static 경로 여부, 구성요소 수"),
        ("가장 오래 걸리는 요청", "몇 초 / 30초 정도 / 30초~1분 / 그 이상", "30초 이상이면 Lambda 제외 (API 30초 한도)"),
        ("실시간 연결 · 메모리 상태 · 큰 파일", "예 / 아니요", "하나라도 예이면 Container"),
        ("하루 사용자 · 트래픽 패턴", "100명 이하 … 1만 명 초과 / 꾸준히 · 몰림", "예상 비용, 최소 실행 개수"),
        ("멈춤 허용도 · 첫 요청 지연", "잠깐 멈춰도 됨 … 절대 안 됨 / 민감도", "Multi-AZ, cold start 허용 여부"),
        ("공개 범위", "누구나 / 로그인 사용자 / 특정 IP만", "특정 IP만이면 Serverless 제외 (WAF 불가)"),
        ("월 예산 · 등급", "금액 / 개발용 · 운영용", "예산 초과 경고, 자동 삭제(TTL) · quota"),
    ], size=14, row_h=0.58, highlight={2, 3})
    text(s, 0.6, 6.35, 12.1, 0.4, "HTTP가 아닌 통신을 고르면 UNSUPPORTED — 같은 입력이면 질문으로 넣든 템플릿으로 넣든 결과가 같음",
         size=12, color=MUTED)

    # 5. Engine + blueprints ---------------------------------------------------------------------------------------
    s = content_slide(prs, 5, "04 · 추천 엔진과 Blueprint", "추천은 규칙 기반 엔진, 결정은 사람", (
        "[1:00] 'AI가 Solution Architect를 대신하는 것 아니냐'는 말씀을 듣고 이 부분을 분명히 하려고 합니다.\n"
        "추천은 LLM이 아니라 규칙 기반 엔진이 합니다. SA가 쓰는 판단 기준, 예를 들어 서비스 한도나 비용 구조를 규칙으로 옮겼습니다.\n"
        "그래서 같은 입력이면 항상 같은 결과가 나오고, 추천마다 이유와 탈락한 후보의 이유가 코드로 남습니다.\n"
        "AWS를 바꾸는 건 항상 사람의 승인 뒤입니다.\n"
        "오른쪽이 실제로 만든 Blueprint 세 가지입니다. Container는 ALB를 인터넷에 노출하지 않고 NAT 고정비도 피하는 구조로 만들었습니다."))
    for i, (h, d) in enumerate([("결정적", "같은 입력 → 항상 같은 결과"),
                                ("설명 가능", "추천마다 이유와 탈락 후보의 이유(reason code)"),
                                ("사람 승인", "AI Agent는 실행 경로에 없음, AWS 변경은 승인 후")]):
        y = 1.6 + i * 0.95
        circle_num(s, 0.6, y + 0.1, i + 1)
        text(s, 1.3, y, 4.5, 0.4, h, size=17, bold=True)
        text(s, 1.3, y + 0.4, 4.6, 0.4, d, size=14, color=MUTED)
    box(s, 0.6, 4.6, 5.3, 1.75, fill=TINT)
    text(s, 0.85, 4.72, 4.9, 1.55, [
        [("예시", {"bold": True, "color": TEAL})],
        [("가장 오래 걸리는 요청 = 30초~1분", {"bold": True})],
        "→ Serverless 탈락 (EXC_SLS_TIMEOUT)",
        [("→ Container 추천, 예상 비용 표시", {"bold": True})]], size=15, space_after=3)
    bps = [("Static", "비공개 S3 + CloudFront(OAC)", "고정비 0, S3 직접 접근 차단"),
           ("Serverless", "HTTP API → Lambda (Web Adapter image)", "요청이 없으면 비용 0, 원본 image는 그대로"),
           ("Container", "CloudFront → VPC origin → 내부 ALB → Fargate", "ALB 비공개, NAT 없음, ALB는 환경끼리 공유")]
    for i, (name, path, why) in enumerate(bps):
        y = 1.6 + i * 1.7
        box(s, 6.4, y, 6.3, 1.5, fill=WHITE, line=LINE)
        box(s, 6.6, y + 0.25, 1.7, 1.0, fill=TEAL if i == 2 else DARK)
        text(s, 6.6, y + 0.25, 1.7, 1.0, name, size=15, color=WHITE, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        text(s, 8.5, y + 0.22, 4.1, 0.6, path, size=14, bold=True)
        text(s, 8.5, y + 0.88, 4.1, 0.4, why, size=13, color=MUTED)

    # 6. Demo screenshots ------------------------------------------------------------------------------------------
    s = content_slide(prs, 6, "05 · Demo", "실제 AWS에서 동작한 화면", (
        "[1:00] 실제 AWS(서울 리전)에서 동작한 화면입니다.\n"
        "① 요구사항에 답하면 추천 구조와 이유, 승인 전 예상 비용이 나옵니다.\n"
        "② 승인 후 PLAN, STATE, HTTP 검사를 모두 통과해서 READY가 된 환경입니다.\n"
        "③ 받은 HTTPS 주소로 접속하면 실제 앱이 응답합니다.\n"
        "Static은 승인부터 READY까지 3분 58초, Serverless는 1분 43초가 걸렸습니다."))
    slots = [("스크린샷 ①", "요구사항 입력 → 추천 결과", "추천 구조 · 이유 · 예상 비용이 보이게"),
             ("스크린샷 ②", "READY 환경과 검사 결과", "PLAN · STATE · HTTP PASS 목록이 보이게"),
             ("스크린샷 ③", "실제 앱 응답", "브라우저 주소창의 HTTPS URL이 보이게")]
    for i, (tag, cap, hint) in enumerate(slots):
        x = 0.6 + i * 4.1
        box(s, x, 1.6, 3.9, 3.6, fill="F7FAF9", line="9FB1AD", dash=True, radius=0.04)
        text(s, x, 2.75, 3.9, 0.5, tag, size=20, color="9FB1AD", bold=True, align=PP_ALIGN.CENTER)
        text(s, x + 0.2, 3.3, 3.5, 0.8, hint, size=12, color="9FB1AD", align=PP_ALIGN.CENTER)
        circle_num(s, x, 5.45, i + 1, d=0.45, size=14)
        text(s, x + 0.6, 5.45, 3.3, 0.45, cap, size=16, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    box(s, 0.6, 6.15, 12.1, 0.6, fill=TINT)
    text(s, 0.85, 6.15, 11.6, 0.6, [[("승인 → READY 실측   ", {"bold": True, "color": TEAL}),
                                     ("Static 3분 58초  ·  Serverless 1분 43초  ·  삭제 후 남은 리소스 0", {"bold": True})]],
         size=15, anchor=MSO_ANCHOR.MIDDLE)

    # 7. Metrics ---------------------------------------------------------------------------------------------------
    s = content_slide(prs, 7, "06 · 최종 지표", "무엇을, 어떻게 잴 것인가", (
        "[1:30] CFP에서 가장 중요하다고 한 지표와 측정 방법입니다. 실측, 예상, 목표를 구분해서 말씀드리겠습니다.\n"
        "위의 숫자 세 개는 실제 AWS에서 잰 값입니다.\n"
        "Container는 공유 인프라를 처음 만드는 시간 때문에 17분 40초가 걸렸고, 자동 검사 46개 중 45개를 통과했습니다. "
        "남은 1개는 정상 설정을 위반으로 판정한 검증기 오탐이라 지금 고치고 있습니다.\n"
        "목표는 Static, Serverless, 공유 인프라가 있는 Container는 10분, 처음 만드는 Container는 20분입니다.\n"
        "마지막 줄은 지난번 피드백을 받고 새로 넣은 지표입니다. 공개 웹 앱을 조사해서, 제가 SA로서 정답을 먼저 적고 엔진 결과와 비교합니다.\n"
        "정성 지표로는 다른 선수 두 명이 직접 써 보고 걸린 시간, 추천 이유를 이해했는지, 실패 메시지만 보고 다음 행동을 정할 수 있었는지를 묻습니다."))
    for i, (big, lab) in enumerate([("3분 58초", "Static 승인 → READY"), ("1분 43초", "Serverless 승인 → READY"),
                                    ("0개", "삭제 후 남은 리소스")]):
        x = 0.6 + i * 4.1
        box(s, x, 1.55, 3.9, 1.25, fill=TINT)
        text(s, x + 0.3, 1.6, 3.4, 0.7, big, size=32, color=TEAL, bold=True)
        text(s, x + 0.3, 2.28, 3.4, 0.4, [[(lab, {}), ("  실측", {"bold": True, "color": TEAL, "size": 12})]], size=14, color=MUTED)
    table(s, 0.6, 3.0, 12.1, [3.0, 4.4, 4.7], [
        ("지표", "목표", "측정 방법"),
        ("승인 → READY", "Static · Serverless · warm Container 10분, cold Container 20분", "환경 기록의 승인 · READY 시각 차이, Blueprint별 3회"),
        ("보안 기준 위반 차단", "READY 환경 FAIL 0, 위반 주입 10종 100% 차단", "PLAN · STATE · HTTP 검사 결과, Negative E2E"),
        ("삭제 후 남는 리소스", "0", "삭제 후 read-only 조회"),
        ("신규  공개 웹 앱 추천 일치율", "공개 앱 10개 이상 조사 후 목표치 확정", "SA 정답을 먼저 적고 엔진 결과와 비교"),
    ], size=14, row_h=0.55, highlight={4})
    text(s, 0.6, 5.95, 12.1, 0.8, [
        [("Container 현재  ", {"bold": True, "color": "9A6412"}),
         ("17분 40초(공유 인프라 최초 생성 포함) · 자동 검사 45/46 통과, 1건은 검증기 오탐으로 수정 중", {})],
        [("정성  ", {"bold": True, "color": TEAL}),
         ("선수 2명 사용 평가 — 걸린 시간, 추천 이유 이해, 실패 메시지로 다음 행동 결정 가능 여부", {})]],
        size=14, color=MUTED, space_after=4)

    # 8. Cost & security -------------------------------------------------------------------------------------------
    s = content_slide(prs, 8, "07 · 비용과 보안", "사용자가 우회할 수 없는 안전장치", (
        "[0:50] 비용입니다. 플랫폼 자체는 사용량 과금 서비스로만 만들어서 월 1~3달러 정도입니다.\n"
        "Static과 Serverless 환경은 각각 1달러 미만이고, Container는 공유 ALB 때문에 혼자 있을 때 약 23.5달러, 여러 환경이 나누면 최대 12달러입니다.\n"
        "오른쪽 안전장치는 모두 구현돼 있습니다. 플랫폼 전체 상한 30달러를 넘으면 승인 단계에서 막히고, 사용자가 우회할 수 없습니다.\n"
        "보안은 비용과 무관하게 항상 적용하고, 세 단계 검사로 확인합니다. 단가는 제출 전에 Pricing Calculator로 다시 확인하겠습니다."))
    text(s, 0.6, 1.6, 5.6, 0.4, "월 예상 비용 (USD, 예상)", size=16, color=MUTED, bold=True)
    for i, (v, lab) in enumerate([("$1–3", "Control Plane + CodeBuild"), ("< $1", "Static · Serverless DEV 1개"),
                                  ("최대 $12", "Container DEV (ALB 4분할)"), ("$23.5", "Container가 1개뿐일 때")]):
        x, y = 0.6 + (i % 2) * 2.85, 2.1 + (i // 2) * 1.6
        box(s, x, y, 2.7, 1.4, fill=TINT)
        text(s, x + 0.2, y + 0.15, 2.4, 0.6, v, size=26, color=TEAL if i < 2 else "9A6412", bold=True)
        text(s, x + 0.2, y + 0.78, 2.4, 0.55, lab, size=13, color=MUTED)
    text(s, 0.6, 5.4, 5.6, 0.7, "단가는 2026-10-04 기준 근사값, 제출 전 AWS Pricing Calculator로 재확인", size=12, color=MUTED)
    guards = [("플랫폼 전체 상한 $30", "넘으면 승인 단계에서 차단, 우회 불가"),
              ("사용자 quota · 자동 삭제", "DEV 2개 · PROD 1개, DEV는 7일 뒤 삭제"),
              ("고정비 리소스 사전 차단", "NAT Gateway · EIP · EC2는 PLAN 검사에서 차단"),
              ("권한 경계", "runner와 환경 role 모두 permissions boundary"),
              ("보안 3단계 검사", "PLAN → STATE → HTTP, 하나라도 실패하면 READY 불가")]
    for i, (h, d) in enumerate(guards):
        y = 1.6 + i * 0.98
        box(s, 6.6, y, 6.1, 0.85, fill=WHITE, line=LINE)
        circle_num(s, 6.78, y + 0.19, i + 1, d=0.47, fill=DARK, size=14)
        text(s, 7.45, y + 0.08, 5.1, 0.4, h, size=16, bold=True)
        text(s, 7.45, y + 0.45, 5.1, 0.4, d, size=13, color=MUTED)

    # 9. Risks & next ----------------------------------------------------------------------------------------------
    s = content_slide(prs, 9, "08 · 리스크와 다음 단계", "실제 AWS에서만 드러나는 문제를 테스트로 고정", (
        "[1:10] 가장 큰 리스크는 로컬 테스트를 다 통과해도 실제 AWS에서만 드러나는 문제입니다. 구현 중에 7번 겪었습니다.\n"
        "예를 들어 ECS ListTasks는 task가 아니라 container-instance라는 resource type으로 권한을 검사해서, 검증 단계에서 막혔습니다.\n"
        "그래서 실패할 때마다 원인을 먼저 분석하고, 최소한으로 고치고, 같은 실패가 다시 나지 않게 회귀 테스트를 추가한 뒤 다음 E2E를 했습니다. "
        "지금 자동 테스트는 382개입니다.\n"
        "다음 할 일은 오른쪽 순서입니다. 특히 두 번째, 베이스라인으로 쓸 공개 웹 앱 조사는 지난번 피드백을 반영한 것입니다.\n"
        "필수 강좌 4개는 아직 수강 전이라, 수강한 뒤 AI 활용 계획을 확정하겠습니다."))
    loop = ["실패", "원인 분석", "최소 수정", "회귀 테스트", "다음 E2E"]
    for i, step in enumerate(loop):
        y = 1.6 + i * 0.72
        box(s, 0.6, y, 2.6, 0.58, fill=RED if i == 0 else (TEAL if i == 4 else TINT))
        text(s, 0.6, y, 2.6, 0.58, step, size=15, bold=True, color=WHITE if i in (0, 4) else TEXT,
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    box(s, 3.45, 1.6, 2.75, 3.46, fill=WHITE, line=LINE)
    text(s, 3.65, 1.72, 2.4, 3.3, [
        [("실제로 겪은 7건", {"bold": True, "color": TEXT, "size": 14})],
        "API Gateway 태그 권한", "log delivery 권한", "ECS 배포 추적 권한", "ListTasks resource type",
        "IAM inline 10,240자 한도", "Linux provider checksum", "검증기 오탐 (route table)"], size=13, color=MUTED, space_after=7)
    box(s, 0.6, 5.3, 5.6, 1.2, fill=TINT)
    text(s, 0.85, 5.4, 5.2, 1.05, [
        [("대응", {"bold": True, "color": TEAL})],
        "실패마다 회귀 테스트 추가 (현재 382개)", "AWS 호출별 필요 권한을 공식 레퍼런스로 테스트"],
        size=14, space_after=3)
    nexts = [("Container 검증기 오탐 수정", "실제 응답 형태로 테스트 → E2E 재실행(첫 환경 → 두 번째 환경)", False),
             ("베이스라인 공개 웹 앱 조사", "형태별 공개 앱 10개 이상, 요구사항 형식과 추천 엔진 검증", True),
             ("Negative E2E", "실제 AWS에 위반 10종을 주입해 차단 확인", False),
             ("필수 강좌 4개 수강", "수강 후 AI 활용 계획 확정", False),
             ("사용성 평가", "선수 2명이 직접 환경 생성", False)]
    for i, (h, d, hl) in enumerate(nexts):
        y = 1.6 + i * 1.0
        circle_num(s, 6.6, y + 0.12, i + 1, fill=AMBER if hl else DARK)
        text(s, 7.3, y, 5.4, 0.42, [[(h, {}), ("  피드백 반영", {"color": "9A6412", "size": 12})] if hl else [(h, {})]],
             size=16, bold=True)
        text(s, 7.3, y + 0.42, 5.4, 0.45, d, size=13, color=MUTED)

    # 10. Feedback -------------------------------------------------------------------------------------------------
    s = dark_slide(prs, (
        "[0:40] 마지막으로 피드백을 받고 싶은 점 세 가지입니다.\n"
        "첫째, 범위를 HTTP 웹 앱으로 한정하는 방향이 맞는지.\n"
        "둘째, 베이스라인 앱을 형태별로 여러 개 조사하는 것과 대표 앱 하나를 정하는 것 중 어느 쪽이 좋을지.\n"
        "셋째, 추천 일치율의 정답을 제가 SA로서 먼저 정하는 방식이 공정하다고 볼 수 있을지입니다.\n"
        "감사합니다."))
    text(s, 0.8, 0.8, 11, 0.4, "09 · 피드백 요청", size=14, color="7FC8BE", bold=True)
    text(s, 0.8, 1.2, 11.5, 0.9, "피드백 받고 싶은 점", size=36, color=WHITE, bold=True)
    qs = ["범위를 HTTP 웹 애플리케이션으로 한정하는 방향이 맞을까요?",
          "베이스라인 앱은 형태별로 여러 개 조사할까요, 대표 앱 하나로 정할까요?",
          "추천 일치율의 정답(SA 판단)을 제가 먼저 정하는 방식이 공정할까요?"]
    for i, q in enumerate(qs):
        y = 2.5 + i * 1.15
        box(s, 0.8, y, 11.7, 0.95, fill="2A3B44")
        circle_num(s, 1.05, y + 0.22, i + 1)
        text(s, 1.8, y, 10.5, 0.95, q, size=19, color=WHITE, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 0.8, 6.3, 11.7, 0.5, "감사합니다", size=20, color="7FC8BE", bold=True, align=PP_ALIGN.RIGHT)

    keep_korean_words(prs)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    return out


if __name__ == "__main__":
    print(build(*[Path(a).resolve() for a in sys.argv[1:]]))
