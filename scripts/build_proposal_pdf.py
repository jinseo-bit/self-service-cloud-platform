"""Render docs/proposal/proposal-final.md as an A4 PDF in the style of the CFP example proposal.

Markdown -> HTML (tables, fenced code), mermaid blocks drawn by mermaid.js, printed by Chromium (Playwright).
Usage: python scripts/build_proposal_pdf.py [input.md] [output.pdf]
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "proposal" / "proposal-final.md"
OUT = ROOT / "docs" / "proposal" / "proposal-final.pdf"

CSS = """
@page { size: A4; margin: 19mm 19mm 17mm 19mm; }
:root { --ink:#1f1f1f; --muted:#6b6b6b; --line:#e4e1db; --head:#f4f2ee; --chip:#efefef; --code:#f3f3f1; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif; color: var(--ink); font-size: 9.9pt; line-height: 1.56;
       margin: 0; word-break: keep-all; overflow-wrap: break-word; }
h1 { font-family: 'Noto Serif KR', 'Batang', serif; font-weight: 500; font-size: 25pt; line-height: 1.25;
     margin: 0 0 10px; letter-spacing: -0.3px; }
h2 { font-family: 'Noto Serif KR', 'Batang', serif; font-weight: 500; font-size: 16pt; margin: 20px 0 8px; break-after: avoid; }
h3 { font-size: 11pt; margin: 18px 0 6px; break-after: avoid; }
p { margin: 0 0 9px; }
.meta { margin: 0 0 16px; }
.chip { display: inline-block; background: var(--chip); border-radius: 6px; padding: 1px 9px; font-size: 9.5pt; color: #333; }
.meta .dot { color: var(--muted); margin: 0 4px; }
p.label { margin: 12px 0 4px; break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; font-size: 9pt; line-height: 1.45; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th { background: var(--head); font-weight: 500; text-align: left; color: #444; }
th, td { border: 1px solid var(--line); padding: 6px 8px; vertical-align: top; }
th:first-child, td:first-child { min-width: 6.2em; }
code { word-break: break-all; }
ul { margin: 2px 0 10px; padding-left: 20px; }
li { margin: 2px 0; }
code { font-family: 'D2Coding', Consolas, monospace; font-size: 8.8pt; background: var(--code); border-radius: 4px; padding: 0 4px; }
.diagram { margin: 4px 0 4px; text-align: center; break-inside: avoid; }
.diagram svg { max-width: 100%; max-height: 41mm; height: auto; }
.diagram pre.mermaid { margin: 0; }
.diagram .cluster rect { fill: #fafaf8 !important; stroke: #e4e1db !important; }
.note { color: #333; }
body > :last-child { margin-bottom: 0 !important; }
script, .mermaidTooltip { display: none !important; }  /* mermaid's tooltip div would print as an extra page */
"""

FONTS = "https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=Noto+Serif+KR:wght@500&display=block"

MERMAID_INIT = """
mermaid.initialize({ startOnLoad: false, theme: 'base', securityLevel: 'strict',
  flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 22, rankSpacing: 26, padding: 8, useMaxWidth: true },
  themeVariables: { fontFamily: "'Noto Sans KR', 'Malgun Gothic', sans-serif", fontSize: '12px',
    primaryColor: '#f3f3f1', primaryBorderColor: '#b9b6b0', primaryTextColor: '#1f1f1f', lineColor: '#8a8a8a',
    edgeLabelBackground: '#ffffff' } });
// measure labels with the web font, not the fallback, or node text gets clipped
document.fonts.ready.then(() => mermaid.run({ querySelector: '.mermaid' })).then(() => { document.body.dataset.ready = '1'; });
"""


def to_html(md_text: str) -> tuple[str, str]:
    lines = md_text.splitlines()
    title = re.sub(r"^#\s+", "", lines[0]).strip()
    meta = lines[2].strip() if len(lines) > 2 else ""
    body_md = "\n".join(lines[3:])
    body = markdown.markdown(body_md, extensions=["tables", "fenced_code"])
    # mermaid fences -> diagram containers
    body = re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>',
                  lambda m: f'<div class="diagram"><pre class="mermaid">{m.group(1)}</pre></div>', body, flags=re.S)
    # a paragraph that is only a bold label ("**정량 지표**") acts as a small heading; bold inside text stays inline
    body = re.sub(r"<p>(<strong>[^<]*</strong>)</p>", lambda m: f'<p class="label">{m.group(1)}</p>', body)
    date, _, author = meta.partition("·")
    head = (f"<h1>{html.escape(title)}</h1><p class='meta'><span class='chip'>{html.escape(date.strip())}</span>"
            f"<span class='dot'>·</span><span class='chip'>{html.escape(author.strip())}</span></p>")
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<style>{CSS}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script></head>
<body>{head}{body}<script>{MERMAID_INIT}</script></body></html>"""
    return title, page


def build(src: Path = SRC, out: Path = OUT) -> Path:
    title, page = to_html(src.read_text(encoding="utf-8"))
    html_path = out.with_suffix(".html")
    html_path.write_text(page, encoding="utf-8")
    header = (f"<div style='font-family:Malgun Gothic,sans-serif;font-size:7px;color:#8a8a8a;width:100%;"
              f"padding:0 19mm;'>{html.escape(title)}</div>")
    footer = ("<div style='font-family:Malgun Gothic,sans-serif;font-size:7px;color:#8a8a8a;width:100%;padding:0 19mm;"
              "text-align:right;'>Page <span class='pageNumber'></span> of <span class='totalPages'></span></div>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.goto(html_path.as_uri(), wait_until="networkidle")
        pg.wait_for_function("document.body.dataset.ready === '1'", timeout=30_000)
        pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
               header_template=header, footer_template=footer,
               margin={"top": "19mm", "bottom": "17mm", "left": "19mm", "right": "19mm"})
        browser.close()
    return out


if __name__ == "__main__":
    args = [Path(a).resolve() for a in sys.argv[1:]]
    print(build(*args))
