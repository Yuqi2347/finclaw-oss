from __future__ import annotations

import re
from datetime import datetime
from typing import Any


class SafetyViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


TICKER_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


MAX_LIMITS = {
    "limit": 100,
    "news_limit": 50,
    "max_chars": 30000,
    "stale_days": 365,
}


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    _validate_web_research(tool_name, normalized)
    _validate_ticker(normalized)
    _validate_limits(tool_name, normalized)
    _validate_dates(normalized)
    _validate_enums(tool_name, normalized)
    _validate_force(tool_name, normalized)
    return normalized


def _validate_ticker(arguments: dict[str, Any]) -> None:
    ticker = arguments.get("ticker")
    if ticker is None:
        return
    if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker.strip()):
        raise SafetyViolation("invalid_ticker", "ticker 必须是标准 A 股代码，例如 000988.SZ、688820.SH 或 430047.BJ")
    arguments["ticker"] = ticker.strip().upper()


def _validate_limits(tool_name: str, arguments: dict[str, Any]) -> None:
    for key, max_value in MAX_LIMITS.items():
        if key not in arguments or arguments[key] is None:
            continue
        value = arguments[key]
        if not isinstance(value, int):
            raise SafetyViolation("invalid_limit", f"{key} 必须是整数")
        if value < 1 or value > max_value:
            raise SafetyViolation("limit_exceeded", f"{key} 必须在 1 到 {max_value} 之间")
    if tool_name == "search_stock_symbol" and len(str(arguments.get("query") or "").strip()) > 32:
        raise SafetyViolation("query_too_long", "股票搜索 query 过长")


def _validate_web_research(tool_name: str, arguments: dict[str, Any]) -> None:
    if tool_name != "web_research":
        return
    query = str(arguments.get("query") or "").strip()
    raw_queries = arguments.get("queries")
    has_query = bool(query)
    has_queries = isinstance(raw_queries, list) and any(
        isinstance(item, dict) and str(item.get("query") or "").strip()
        for item in raw_queries
    )
    if not has_query and not has_queries:
        raise SafetyViolation("invalid_query", "web_research 必须提供 query 或非空 queries")
    if has_query:
        arguments["query"] = query
    if raw_queries is not None:
        if not isinstance(raw_queries, list):
            raise SafetyViolation("invalid_queries", "queries 必须是数组")
        if len(raw_queries) > 4:
            raise SafetyViolation("query_limit_exceeded", "web_research 单次最多并行 4 个 query")
        cleaned = []
        for item in raw_queries:
            if not isinstance(item, dict):
                raise SafetyViolation("invalid_queries", "queries 每一项必须是对象")
            item_query = str(item.get("query") or "").strip()
            if not item_query:
                raise SafetyViolation("invalid_query", "queries 每一项都必须提供非空 query")
            cleaned_item = dict(item)
            cleaned_item["query"] = item_query
            cleaned.append(cleaned_item)
        arguments["queries"] = cleaned


def _validate_dates(arguments: dict[str, Any]) -> None:
    for key in ("date", "trade_date", "start_date", "end_date"):
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not DATE_RE.fullmatch(value):
            raise SafetyViolation("invalid_date", f"{key} 必须是 YYYY-MM-DD")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise SafetyViolation("invalid_date", f"{key} 不是有效日期") from exc


def _validate_enums(tool_name: str, arguments: dict[str, Any]) -> None:
    allowed_formats = {"html", "md", "json"}
    if "format" in arguments and arguments["format"] is not None:
        value = str(arguments["format"]).lower()
        if value not in allowed_formats:
            raise SafetyViolation("invalid_format", "format 只能是 html、md 或 json")
        arguments["format"] = value
    if "job_type" in arguments and arguments["job_type"] is not None:
        if arguments["job_type"] not in {"stock_research", "market_discovery"}:
            raise SafetyViolation("invalid_job_type", "job_type 只能是 stock_research 或 market_discovery")
    if "status" in arguments and tool_name == "get_analysis_jobs" and arguments["status"] is not None:
        if arguments["status"] not in {"running", "failed", "completed"}:
            raise SafetyViolation("invalid_status", "status 只能是 running、failed 或 completed")


def _validate_force(tool_name: str, arguments: dict[str, Any]) -> None:
    if tool_name == "run_stock_research" and arguments.get("force") is True:
        # 允许，但留给风险评估提高风险；这里不拒绝。
        return
