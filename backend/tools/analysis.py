from __future__ import annotations

import re
from datetime import date
from typing import Any

from backend.services.resource_guard import resource_guard
from backend.services.analysis_jobs import analysis_job_store
from backend.tools.datahub import datahub_client
from backend.tools.reports import report_library


_A_SHARE_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)


def run_market_discovery(no_resume: bool = False, session_id: str = "default", run_id: str | None = None) -> dict[str, Any]:
    job = analysis_job_store.create_market_discovery(no_resume=no_resume, session_id=session_id, origin_run_id=run_id)
    return {
        "status": "analysis_job_created",
        "job": job.model_dump(),
        "message": "市场主线发现已作为后台任务启动，你可以继续对话。",
    }


def run_stock_research(
    ticker: str,
    trade_date: str | None = None,
    force: bool = False,
    session_id: str = "default",
    run_id: str | None = None,
) -> dict[str, Any]:
    normalized = _resolve_stock_ticker(ticker)
    selected_date = trade_date or date.today().isoformat()
    existing = analysis_job_store.find_latest_stock_research(
        ticker=normalized,
        trade_date=selected_date,
        session_id=session_id,
    )
    if existing is not None and existing.status in {"running", "cancelling"}:
        existing_payload = existing.model_dump()
        return {
            "status": "analysis_job_reused",
            "job": existing_payload,
            "message": (
                f"{normalized} 的个股深度研究已经在运行中，"
                "已返回现有任务，不会重复启动。请用 get_analysis_jobs 查看进度。"
            ),
        }
    if existing is not None and not force:
        existing_payload = existing.model_dump()
        if existing.status == "completed":
            return {
                "status": "analysis_job_completed_existing",
                "job": existing_payload,
                "report_id": existing.output_report_id,
                "message": (
                    f"{normalized} 已有完成的个股深度研究，"
                    "已返回现有任务和报告编号，不会重复启动。"
                ),
            }
        if existing.status in {"failed", "cancelled"}:
            return {
                "status": "analysis_job_restart_required",
                "job": existing_payload,
                "message": (
                    f"{normalized} 最近一次个股深度研究状态为 {existing.status}。"
                    "如需重新运行，请明确要求重跑，系统会以 force=true 启动新任务。"
                ),
            }

    resource_guard.check_and_record_stock_research_start(session_id)
    job = analysis_job_store.create_stock_research(
        ticker=normalized,
        trade_date=trade_date,
        session_id=session_id,
        origin_run_id=run_id,
    )
    return {
        "status": "analysis_job_created",
        "job": job.model_dump(),
        "message": "个股深度研究已作为后台任务启动，你可以继续对话。",
    }


def refresh_and_run_stock_research(
    ticker: str,
    trade_date: str | None = None,
    force: bool = True,
    include_daily: bool = True,
    include_news: bool = True,
    include_fundamentals: bool = True,
    news_limit: int = 20,
    session_id: str = "default",
    run_id: str | None = None,
) -> dict[str, Any]:
    normalized = _resolve_stock_ticker(ticker)
    refresh_result = _refresh_stock_inputs(
        normalized,
        include_daily=include_daily,
        include_news=include_news,
        include_fundamentals=include_fundamentals,
        news_limit=news_limit,
    )
    research_result = run_stock_research(
        normalized,
        trade_date=trade_date,
        force=force,
        session_id=session_id,
        run_id=run_id,
    )
    return {
        "status": "refresh_completed_research_started",
        "ticker": normalized,
        "refresh": refresh_result,
        "research": research_result,
        "job": research_result.get("job"),
        "message": "个股研究输入数据刷新流程已执行，个股深度研究已作为后台任务启动。",
    }


def _refresh_stock_inputs(
    ticker: str,
    *,
    include_daily: bool,
    include_news: bool,
    include_fundamentals: bool,
    news_limit: int,
) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    def run_step(name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            steps[name] = fn(*args, **kwargs)
        except Exception as exc:  # Keep research launch available when one data source is unstable.
            errors.append({"step": name, "error": str(exc)})

    run_step("snapshot", datahub_client.refresh_stock_snapshot, ticker)
    if include_daily:
        run_step("daily", datahub_client.refresh_stock_daily, ticker)
    if include_news:
        run_step("news", datahub_client.refresh_stock_news, ticker, limit=news_limit)
    if include_fundamentals:
        run_step("fundamentals", datahub_client.refresh_stock_fundamentals, ticker)

    return {"ticker": ticker, "steps": steps, "errors": errors}


def get_analysis_jobs(
    job_type: str | None = None,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    normalized_ticker = _resolve_stock_ticker(ticker) if ticker else None
    jobs = analysis_job_store.query_jobs(
        job_type=job_type,
        ticker=normalized_ticker,
        status=status,
        limit=limit,
    )
    return {"jobs": jobs, "count": len(jobs)}


def _resolve_stock_ticker(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("股票代码不能为空")
    if _A_SHARE_CODE_RE.fullmatch(raw):
        return _normalize_a_share_ticker(raw)

    resolved = _resolve_from_datahub_name(raw)
    if resolved:
        return resolved

    raise ValueError(
        f"无法将 {raw!r} 解析为标准 A 股代码。请先调用 search_stock_symbol 按股票名称/代码搜索，确认后再使用标准代码。"
    )


def _normalize_a_share_ticker(value: str) -> str:
    upper = value.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith("6"):
        return f"{upper}.SH"
    if upper.startswith(("0", "3")):
        return f"{upper}.SZ"
    if upper.startswith(("4", "8")):
        return f"{upper}.BJ"
    raise ValueError(f"不支持的 A 股代码：{value}")


def _resolve_from_datahub_name(name: str) -> str | None:
    target = name.strip().lower()
    candidates: list[dict[str, Any]] = []
    for getter in (datahub_client.get_positions, datahub_client.get_watchlist):
        try:
            rows = getter()
        except Exception:
            rows = []
        if isinstance(rows, list):
            candidates.extend(item for item in rows if isinstance(item, dict))

    for record in report_library.list_report_catalog(limit=200):
        if isinstance(record, dict):
            candidates.append({"ticker": record.get("subject"), "name": record.get("title")})

    for item in candidates:
        ticker = str(item.get("ticker") or "").strip()
        item_name = str(item.get("name") or item.get("title") or "").strip().lower()
        if item_name and target in item_name and _A_SHARE_CODE_RE.fullmatch(ticker):
            return _normalize_a_share_ticker(ticker)
    return None
