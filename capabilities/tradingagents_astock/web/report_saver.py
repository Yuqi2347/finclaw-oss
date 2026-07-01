"""Persist completed web analysis reports to local markdown files."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingagents.dataflows.utils import safe_ticker_component
from web.html_report_export import generate_html_report


_SECTIONS = [
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


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def save_markdown_report(final_state: dict[str, Any], ticker: str, trade_date: str, results_dir: str) -> Path:
    safe_ticker = safe_ticker_component(ticker)
    root = Path(results_dir) / "reports" / safe_ticker / trade_date
    root.mkdir(parents=True, exist_ok=True)

    parts = [
        f"# TradingAgents A股投研报告：{ticker}",
        "",
        f"- 分析日期：{trade_date}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "> 本报告由 AI 多 Agent 系统自动生成，仅供学习研究，不构成投资建议。",
        "",
    ]

    for key, title in _SECTIONS:
        content = final_state.get(key)
        if content:
            parts.extend([f"## {title}", "", _strip_think(str(content)), ""])

    debate = final_state.get("investment_debate_state")
    if isinstance(debate, dict):
        parts.extend(["## 多空辩论", ""])
        for key, title in (("bull_history", "多方观点"), ("bear_history", "空方观点"), ("judge_decision", "研究经理决策")):
            if debate.get(key):
                parts.extend([f"### {title}", "", _strip_think(str(debate[key])), ""])

    risk = final_state.get("risk_debate_state")
    if isinstance(risk, dict):
        parts.extend(["## 风控评估", ""])
        for key, title in (
            ("aggressive_history", "激进观点"),
            ("conservative_history", "保守观点"),
            ("neutral_history", "中性观点"),
            ("judge_decision", "风控决策"),
        ):
            if risk.get(key):
                parts.extend([f"### {title}", "", _strip_think(str(risk[key])), ""])

    report_path = root / "complete_report.md"
    report_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")
    _save_json(root / "final_state.json", final_state)
    metadata = {
        "source": "TradingAgents",
        "report_type": "stock_research",
        "category": "个股层",
        "subject": ticker,
        "date": trade_date,
        "title": f"{ticker} 个股深度研究报告",
        "tags": [ticker, "个股研究"],
        "research_scope": "deep_stock_research",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "artifacts": {
            "md": str(report_path),
            "json": str(root / "final_state.json"),
        },
    }
    try:
        html_files = generate_html_report(final_state, ticker, trade_date, root)
        metadata["html_report"] = {"ok": True, "files": html_files}
        public_html = html_files.get("public_report_filepath")
        if public_html:
            metadata["artifacts"]["html"] = public_html
    except Exception as exc:
        metadata["html_report"] = {"ok": False, "error": str(exc)}
    _save_json(root / "metadata.json", metadata)
    return report_path


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
