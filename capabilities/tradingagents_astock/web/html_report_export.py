"""Self-contained HTML export for TradingAgents AStock reports."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any


_REPORT_KEYS: list[tuple[str, str]] = [
    ("investment_plan", "最终投资建议"),
    ("market_report", "技术分析"),
    ("sentiment_report", "市场情绪"),
    ("news_report", "新闻舆情"),
    ("fundamentals_report", "基本面"),
    ("policy_report", "政策分析"),
    ("hot_money_report", "游资追踪"),
    ("lockup_report", "解禁/减持"),
    ("trader_investment_plan", "交易员计划"),
    ("trader_investment_decision", "交易员决策"),
    ("final_trade_decision", "最终决策"),
]


_DEBATE_KEYS: list[tuple[str, str, tuple[tuple[str, str], ...]]] = [
    (
        "investment_debate_state",
        "多空辩论",
        (
            ("bull_history", "多方观点"),
            ("bear_history", "空方观点"),
            ("judge_decision", "研究经理裁决"),
        ),
    ),
    (
        "risk_debate_state",
        "风控评估",
        (
            ("aggressive_history", "激进观点"),
            ("conservative_history", "保守观点"),
            ("neutral_history", "中性观点"),
            ("judge_decision", "风控裁决"),
        ),
    ),
]


def generate_html_report(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    report_root: Path,
) -> dict[str, Any]:
    """Render a local HTML report beside ``complete_report.md``.

    The renderer intentionally has no external engine dependency. It converts
    TradingAgents final_state sections into a readable static HTML artifact.
    """
    report_root = Path(report_root)
    report_root.mkdir(parents=True, exist_ok=True)
    safe_ticker = _safe_filename(ticker)
    safe_date = _safe_filename(trade_date)
    filename = f"final_report_{safe_date}_{safe_ticker}_A股个股深度研究报告.html"
    path = report_root / filename
    path.write_text(_build_html(final_state, ticker, trade_date), encoding="utf-8")
    resolved = str(path.resolve())
    return {
        "report_filepath": resolved,
        "public_report_filepath": resolved,
        "public_report_filename": path.name,
    }


def _build_html(final_state: dict[str, Any], ticker: str, trade_date: str) -> str:
    sections = _build_sections(final_state)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nav = "\n".join(
        f'<a href="#{section_id}">{html.escape(title)}</a>'
        for section_id, title, _ in sections
    )
    body = "\n".join(
        f"""
        <section class="section" id="{section_id}">
          <div class="section-kicker">TradingAgents</div>
          <h2>{html.escape(title)}</h2>
          <div class="prose">{content}</div>
        </section>
        """
        for section_id, title, content in sections
    )
    if not body:
        body = """
        <section class="section" id="empty-report">
          <div class="section-kicker">TradingAgents</div>
          <h2>报告内容为空</h2>
          <div class="prose"><p>本次研究未返回可展示章节。请查看 final_state.json 或后台日志定位原因。</p></div>
        </section>
        """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(ticker)} A股个股深度研究报告</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1ea;
      --paper: #fffaf1;
      --paper-strong: #fff6e2;
      --ink: #1f2420;
      --muted: #687066;
      --line: rgba(52, 61, 51, 0.14);
      --accent: #9b5b32;
      --accent-2: #315f56;
      --shadow: 0 24px 80px rgba(48, 38, 26, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 12% 8%, rgba(155, 91, 50, 0.16), transparent 28rem),
        radial-gradient(circle at 88% 0%, rgba(49, 95, 86, 0.16), transparent 30rem),
        linear-gradient(135deg, #f8f3ea 0%, var(--bg) 100%);
      color: var(--ink);
      font: 15px/1.72 "LXGW WenKai", "Noto Serif SC", "Source Han Serif SC", "Microsoft YaHei", serif;
    }}
    .shell {{
      width: min(1180px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 42px 0 56px;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 30px;
      padding: 34px;
      background: linear-gradient(145deg, rgba(255, 250, 241, 0.94), rgba(255, 246, 226, 0.78));
      box-shadow: var(--shadow);
    }}
    .hero::after {{
      content: "";
      position: absolute;
      right: -70px;
      top: -90px;
      width: 260px;
      height: 260px;
      border-radius: 999px;
      background: rgba(155, 91, 50, 0.10);
      border: 1px solid rgba(155, 91, 50, 0.16);
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid rgba(49, 95, 86, 0.22);
      border-radius: 999px;
      padding: 7px 12px;
      color: var(--accent-2);
      background: rgba(49, 95, 86, 0.07);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    h1 {{
      position: relative;
      z-index: 1;
      margin: 18px 0 12px;
      font-size: clamp(30px, 5vw, 58px);
      line-height: 1.04;
      letter-spacing: -0.04em;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
      color: var(--muted);
    }}
    .meta span {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 7px 10px;
      background: rgba(255, 255, 255, 0.48);
    }}
    .layout {{
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 22px;
      margin-top: 24px;
    }}
    .toc {{
      position: sticky;
      top: 18px;
      align-self: start;
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 14px;
      background: rgba(255, 250, 241, 0.74);
      backdrop-filter: blur(14px);
    }}
    .toc-title {{
      margin: 3px 4px 9px;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.12em;
    }}
    .toc a {{
      display: block;
      padding: 8px 9px;
      border-radius: 12px;
      color: var(--ink);
      text-decoration: none;
      font-size: 13px;
    }}
    .toc a:hover {{ background: rgba(155, 91, 50, 0.10); color: var(--accent); }}
    .stack {{ display: grid; gap: 18px; min-width: 0; }}
    .section {{
      border: 1px solid var(--line);
      border-radius: 26px;
      padding: 26px;
      background: rgba(255, 250, 241, 0.88);
      box-shadow: 0 16px 54px rgba(48, 38, 26, 0.08);
    }}
    .section-kicker {{
      color: var(--accent);
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    h2 {{
      margin: 7px 0 18px;
      font-size: 24px;
      line-height: 1.25;
      letter-spacing: -0.02em;
    }}
    h3 {{
      margin: 20px 0 10px;
      font-size: 18px;
      color: var(--accent-2);
    }}
    .prose {{
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .prose p {{ margin: 0 0 12px; }}
    .prose ul {{ margin: 0 0 14px; padding-left: 1.35em; }}
    .prose li {{ margin: 5px 0; }}
    .prose pre {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255, 246, 226, 0.76);
      white-space: pre-wrap;
    }}
    .prose table {{
      width: 100%;
      border-collapse: collapse;
      margin: 14px 0;
      overflow: hidden;
      border-radius: 14px;
    }}
    .prose th, .prose td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      vertical-align: top;
    }}
    .prose th {{ background: rgba(49, 95, 86, 0.08); }}
    .disclaimer {{
      margin-top: 22px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 860px) {{
      .shell {{ width: min(100vw - 24px, 720px); padding-top: 18px; }}
      .hero {{ padding: 24px; border-radius: 24px; }}
      .layout {{ grid-template-columns: 1fr; }}
      .toc {{ position: static; }}
      .section {{ padding: 20px; border-radius: 22px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div class="eyebrow">A-Share Research Report</div>
      <h1>{html.escape(ticker)} 个股深度研究报告</h1>
      <div class="meta">
        <span>分析日期：{html.escape(trade_date)}</span>
        <span>生成时间：{html.escape(generated_at)}</span>
        <span>来源：TradingAgents AStock</span>
      </div>
      <p class="disclaimer">本报告由 AI 多 Agent 系统自动生成，仅供学习研究，不构成投资建议。</p>
    </header>
    <div class="layout">
      <nav class="toc" aria-label="目录">
        <div class="toc-title">目录</div>
        {nav}
      </nav>
      <div class="stack">
        {body}
      </div>
    </div>
  </main>
</body>
</html>
"""


def _build_sections(final_state: dict[str, Any]) -> list[tuple[str, str, str]]:
    sections: list[tuple[str, str, str]] = []
    for key, title in _REPORT_KEYS:
        content = final_state.get(key)
        if _has_content(content):
            sections.append((_safe_anchor(key), title, _render_value(content)))

    for state_key, title, fields in _DEBATE_KEYS:
        state = final_state.get(state_key)
        if not isinstance(state, dict):
            continue
        blocks = []
        for key, subtitle in fields:
            content = state.get(key)
            if _has_content(content):
                blocks.append(f"<h3>{html.escape(subtitle)}</h3>{_render_value(content)}")
        if blocks:
            sections.append((_safe_anchor(state_key), title, "\n".join(blocks)))
    return sections


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return _markdownish_to_html(_strip_think(value))
    if isinstance(value, (list, tuple)):
        items = "".join(f"<li>{_render_inline(item)}</li>" for item in value if _has_content(item))
        return f"<ul>{items}</ul>" if items else ""
    if isinstance(value, dict):
        rows = "".join(
            f"<tr><th>{html.escape(str(key))}</th><td>{_render_inline(item)}</td></tr>"
            for key, item in value.items()
            if _has_content(item)
        )
        return f"<table>{rows}</table>" if rows else ""
    return f"<p>{html.escape(str(value))}</p>"


def _render_inline(value: Any) -> str:
    if isinstance(value, str):
        return _markdownish_to_html(_strip_think(value))
    if isinstance(value, (dict, list, tuple)):
        return _render_value(value)
    return html.escape(str(value))


def _markdownish_to_html(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    table: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline_markup(' '.join(paragraph).strip())}</p>")
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    def flush_table() -> None:
        if table:
            blocks.append(_pipe_table_to_html(table))
            table.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_table()
            flush_paragraph()
            flush_bullets()
            continue
        if _is_pipe_table_line(line):
            flush_paragraph()
            flush_bullets()
            table.append(line)
            continue
        flush_table()
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            flush_bullets()
            level = min(3, len(heading.group(1)) + 1)
            blocks.append(f"<h{level}>{_inline_markup(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            bullets.append(_inline_markup(bullet.group(1)))
            continue
        flush_bullets()
        paragraph.append(line)

    flush_table()
    flush_paragraph()
    flush_bullets()
    return "\n".join(blocks) if blocks else "<p></p>"


def _is_pipe_table_line(line: str) -> bool:
    if "|" not in line:
        return False
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(_split_pipe_row(stripped)) >= 2


def _is_pipe_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_pipe_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _pipe_table_to_html(lines: list[str]) -> str:
    rows = [_split_pipe_row(line) for line in lines if _is_pipe_table_line(line)]
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:]
    if body and _is_pipe_separator(body[0]):
        body = body[1:]

    width = max(len(header), *(len(row) for row in body)) if body else len(header)
    header = _normalize_row(header, width)
    body = [_normalize_row(row, width) for row in body]
    thead = "<thead><tr>" + "".join(f"<th>{_inline_markup(cell)}</th>" for cell in header) + "</tr></thead>"
    tbody_rows = []
    for row in body:
        tbody_rows.append("<tr>" + "".join(f"<td>{_inline_markup(cell)}</td>" for cell in row) + "</tr>")
    tbody = "<tbody>" + "".join(tbody_rows) + "</tbody>" if tbody_rows else ""
    return f"<table>{thead}{tbody}</table>"


def _normalize_row(row: list[str], width: int) -> list[str]:
    if len(row) >= width:
        return row[:width]
    return row + [""] * (width - len(row))


def _inline_markup(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", str(text), flags=re.DOTALL).strip()


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_strip_think(value))
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _safe_anchor(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-") or "section"


def _safe_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", str(value)).strip("._") or "report"
