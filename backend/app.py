from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import math
from datetime import datetime
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from requests import HTTPError

from backend.core.env import settings
from backend.core.agent_loop import agent_loop
from backend.core.models import ChatRequest, ConfirmRequest, InterruptRequest, SessionCreateRequest, SessionSecuritySettings
from backend.core.streaming import sse_event
from backend.core.tool_gateway import tool_gateway
from backend.services.approval import approval_store
from backend.services.analysis_jobs import analysis_job_store
from backend.services.cancellation import cancellation_store
from backend.services.continuation import continuation_service
from backend.services.audit import log_event
from backend.services.dashboard_refresh_scheduler import dashboard_refresh_scheduler
from backend.services.interruptions import interruption_service
from backend.services.resource_guard import ResourceLimitExceeded
from backend.services.observability import trace_store
from backend.services.sessions import chat_session_store
from backend.services.dashboard_state import dashboard_state
from backend.services.portfolio_ledger import portfolio_ledger_service
from backend.services.tradinggraph_service import tradinggraph_service
from backend.services.research_threads import research_thread_service
from backend.services.capabilities import capability_service
from backend.services.runtime_maintenance import runtime_maintenance_service
from backend.adapters.tradinggraph_client import TradingGraphError
from backend.tools.datahub import datahub_client
from backend.tools.reports import report_library
from backend.api.memory_api import router as memory_router

logger = logging.getLogger(__name__)


class ResearchThreadCreateRequest(BaseModel):
    subject: str
    subject_type: str = "unknown"
    depth: str = "standard"
    user_goal: str = ""
    research_goal: str = ""
    subject_hint: str = ""
    scope_hint: str = ""
    budget_profile: str = ""
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] | None = None
    constraints: str = ""
    session_id: str = "default"
    force_new: bool = False


class ResearchThreadControlRequest(BaseModel):
    action: str


class CapabilityUpdateRequest(BaseModel):
    enabled: bool | None = None
    timeout_seconds: int | None = None
    permissions: list[str] | None = None


class SessionRenameRequest(BaseModel):
    title: str


app = FastAPI(title="FinClaw")


def _mount_embedded_datahub() -> None:
    if settings.datahub_mode != "embedded":
        logger.info("FinDataHub mode is http; using DATAHUB_BASE_URL=%s", settings.datahub_base_url)
        return
    try:
        from services.findatahub.backend.app import app as datahub_app
        from services.findatahub.backend.app import startup_refresh_instrument_index
        from services.findatahub.backend.app import startup_market_news_scheduler
    except Exception as exc:
        logger.exception("Failed to load embedded FinDataHub: %s", exc)
        return
    app.mount(settings.datahub_mount_path, datahub_app)

    @app.on_event("startup")
    def _startup_embedded_datahub() -> None:
        startup_refresh_instrument_index()
        startup_market_news_scheduler()

    logger.info("Embedded FinDataHub mounted at %s", settings.datahub_mount_path)


_mount_embedded_datahub()


@app.on_event("startup")
def _startup_dashboard_refresh_scheduler() -> None:
    runtime_maintenance_service.run_once()
    dashboard_refresh_scheduler.start_background_scheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5170",
        "http://localhost:5170",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册记忆系统 API 路由
app.include_router(memory_router)


def sanitize_json_value(value):
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    return value


_A_SHARE_TICKER_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_DASHBOARD_READ_TIMEOUT_SECONDS = 5
_DASHBOARD_MARKET_OVERVIEW_TIMEOUT_SECONDS = 6
_DASHBOARD_REFRESH_SNAPSHOT_TIMEOUT_SECONDS = 12
_DASHBOARD_REFRESH_MARKET_TIMEOUT_SECONDS = 15


def _normalize_dashboard_ticker(value: object) -> str | None:
    ticker = str(value or "").strip().upper()
    if not ticker:
        return None
    if _A_SHARE_TICKER_RE.fullmatch(ticker):
        if "." in ticker:
            return ticker
        if ticker.startswith("6"):
            return f"{ticker}.SH"
        if ticker.startswith(("0", "3")):
            return f"{ticker}.SZ"
        if ticker.startswith(("4", "8")):
            return f"{ticker}.BJ"
    return None


def _is_placeholder_stock_name(value: object, ticker: str | None = None) -> bool:
    name = str(value or "").strip()
    if not name:
        return True
    normalized_ticker = _normalize_dashboard_ticker(ticker) if ticker else None
    normalized_name = _normalize_dashboard_ticker(name)
    return bool(normalized_name and (normalized_name == normalized_ticker or normalized_ticker is None))


def _unwrap_stock_snapshot_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("snapshot")
    if isinstance(nested, dict):
        return nested
    return value


def _snapshot_time_context_for_sidebar(snapshot: dict[str, object]) -> dict[str, object]:
    updated_at = snapshot.get("updated_at")
    return {
        "snapshot_updated_at": updated_at,
        "snapshot_source": snapshot.get("source"),
        "data_semantics": "该行价格来自 DataHub 快照；updated_at 为快照写入/更新时间。",
    }


def _lookup_stock_display_name(ticker: str) -> str | None:
    try:
        rows = datahub_client.search_stock_symbol(ticker, limit=1)
    except Exception:
        return None
    if isinstance(rows, dict):
        for key in ("items", "results", "data"):
            value = rows.get(key)
            if isinstance(value, list):
                rows = value
                break
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_ticker = _normalize_dashboard_ticker(row.get("ticker") or row.get("ts_code") or row.get("code"))
        if candidate_ticker and candidate_ticker != ticker:
            continue
        name = row.get("name") or row.get("display_name") or row.get("short_name")
        if not _is_placeholder_stock_name(name, ticker):
            return str(name).strip()
    return None


def _first_present(mapping: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _build_dashboard_sidebar_payload() -> dict[str, object]:
    """构建看板数据（优化版：使用缓存和优先级加载）"""
    errors: list[str] = []
    current_state = dashboard_state.get_state()
    payload: dict[str, object] = {
        "updated_at": current_state.get("updated_at"),
        "portfolio_summary": current_state.get("portfolio_summary") or {},
        "market_overview": _filter_sidebar_market_overview(current_state.get("market_overview")),
        "watchlist": current_state.get("watchlist") or [],
        "positions": current_state.get("positions") or [],
        "portfolio_performance": current_state.get("portfolio_performance") or {},
        "news": current_state.get("news") or [],
        "news_meta": current_state.get("news_meta") or {},
        "data_source_status": current_state.get("data_source_status") or {},
        "errors": errors,
    }

    # 阶段 1：关键数据（快速返回）
    critical_loaders = [
        (
            "market_overview",
            lambda: datahub_client.get_market_overview(
                8,
                timeout=_DASHBOARD_MARKET_OVERVIEW_TIMEOUT_SECONDS,
                use_cache=True,
                include_breadth=False,
            ),
        ),
        ("watchlist", lambda: datahub_client.get_watchlist(timeout=_DASHBOARD_READ_TIMEOUT_SECONDS, use_cache=True)),
    ]

    # 阶段 2：次要数据
    secondary_loaders = [
        (
            "portfolio_summary",
            lambda: datahub_client.get_portfolio_summary(timeout=_DASHBOARD_READ_TIMEOUT_SECONDS, use_cache=True),
        ),
        ("portfolio_performance", lambda: portfolio_ledger_service.get_performance(recent_limit=5)),
        ("positions", lambda: datahub_client.get_positions(timeout=_DASHBOARD_READ_TIMEOUT_SECONDS, use_cache=True)),
        ("news_bundle", lambda: datahub_client.get_market_news_snapshot(limit=9, timeout=_DASHBOARD_READ_TIMEOUT_SECONDS, use_cache=True)),
    ]

    # 先加载关键数据
    with ThreadPoolExecutor(max_workers=len(critical_loaders)) as executor:
        future_map = {executor.submit(loader): key for key, loader in critical_loaders}
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                payload[key] = future.result()
            except Exception as exc:
                logger.error(f"Failed to load {key}: {exc}")
                errors.append(f"{key}: {exc}")

    # 再加载次要数据
    with ThreadPoolExecutor(max_workers=len(secondary_loaders)) as executor:
        future_map = {executor.submit(loader): key for key, loader in secondary_loaders}
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                result = future.result()
                if key == "news_bundle" and isinstance(result, dict):
                    payload["news"] = result.get("items") or []
                    payload["news_meta"] = result.get("meta") or {}
                else:
                    payload[key] = result
            except Exception as exc:
                logger.error(f"Failed to load {key}: {exc}")
                errors.append(f"{key}: {exc}")

    payload["updated_at"] = _derive_dashboard_snapshot_timestamp(payload)
    payload["watchlist_cards"] = _build_watchlist_cards(payload)
    payload["data_source_status"] = _build_dashboard_data_source_status(payload)

    # 更新全局状态
    dashboard_state.update_state(payload, notify=False)

    return sanitize_json_value(payload)


def _derive_dashboard_snapshot_timestamp(payload: dict[str, object]) -> str | None:
    candidates: list[str] = []

    market_overview = payload.get("market_overview")
    if isinstance(market_overview, dict):
        generated_at = market_overview.get("generated_at")
        if isinstance(generated_at, str) and generated_at.strip():
            candidates.append(generated_at)

        for key in ("indices",):
            rows = market_overview.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                stamp = row.get("updated_at") or row.get("event_time")
                if isinstance(stamp, str) and stamp.strip():
                    candidates.append(stamp)

    portfolio_summary = payload.get("portfolio_summary")
    if isinstance(portfolio_summary, dict):
        last_updated = portfolio_summary.get("last_updated")
        if isinstance(last_updated, str) and last_updated.strip():
            candidates.append(last_updated)
    elif isinstance(portfolio_summary, list):
        for row in portfolio_summary:
            if not isinstance(row, dict):
                continue
            stamp = row.get("updated_at")
            if isinstance(stamp, str) and stamp.strip():
                candidates.append(stamp)

    if not candidates:
        return None
    return max(candidates)


def _build_dashboard_data_source_status(payload: dict[str, object]) -> dict[str, object]:
    errors = payload.get("errors")
    error_rows = [str(item) for item in errors] if isinstance(errors, list) else []
    stamps: list[str] = []
    for key in ("portfolio_summary", "positions", "watchlist_cards"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            stamp = row.get("updated_at")
            if isinstance(stamp, str) and stamp.strip():
                stamps.append(stamp)
    latest_snapshot_at = max(stamps) if stamps else payload.get("updated_at")
    is_stale = not _dashboard_iso_is_today(latest_snapshot_at)
    status = "degraded" if error_rows else ("stale_cache" if is_stale else "ok")
    return {
        "status": status,
        "latest_snapshot_at": latest_snapshot_at,
        "is_stale": is_stale,
        "error_count": len(error_rows),
        "errors": error_rows[:6],
        "note": (
            "部分数据源不可用，当前看板可能包含本地缓存。"
            if error_rows
            else (
                "当前看板快照不是今天的数据，可能是本地缓存。"
                if is_stale
                else "看板读取成功；具体标的新鲜度以各行 updated_at/time_context 为准。"
            )
        ),
    }


def _dashboard_iso_is_today(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return datetime.fromisoformat(value).date() == datetime.utcnow().date()
    except Exception:
        return False


def _filter_sidebar_market_overview(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    filtered: dict[str, object] = {}
    for key in ("generated_at", "indices", "errors"):
        item = value.get(key)
        if item is not None:
            filtered[key] = item
    return filtered


def _build_watchlist_cards(payload: dict[str, object]) -> list[dict[str, object]]:
    watchlist = payload.get("watchlist")
    positions = payload.get("positions")
    if not isinstance(watchlist, list):
        return []

    position_tickers: set[str] = set()
    if isinstance(positions, list):
        for item in positions:
            if not isinstance(item, dict):
                continue
            ticker = _normalize_dashboard_ticker(item.get("ticker"))
            if ticker:
                position_tickers.add(ticker)

    cards: list[dict[str, object]] = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        ticker = _normalize_dashboard_ticker(item.get("ticker"))
        if not ticker or ticker in position_tickers:
            continue
        cards.append(
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "status": item.get("status"),
                "list_name": item.get("list_name"),
                "current_price": None,
                "change_pct": None,
                "change_amount": None,
                "updated_at": item.get("updated_at"),
                "five_day_series": [],
                "five_day_return_pct": None,
            }
        )

    if not cards:
        return []

    tickers = [str(card.get("ticker") or "") for card in cards if card.get("ticker")]
    snapshot_by_ticker: dict[str, dict[str, object]] = {}
    daily_by_ticker: dict[str, list[dict[str, object]]] = {}

    try:
        batch = datahub_client.get_stock_snapshots_batch(tickers, timeout=10)
        if isinstance(batch, dict):
            for row in batch.get("items") or []:
                if not isinstance(row, dict):
                    continue
                ticker = _normalize_dashboard_ticker(row.get("ticker"))
                if ticker:
                    snapshot_by_ticker[ticker] = row
            for row in batch.get("errors") or []:
                if isinstance(row, dict):
                    errors = payload.setdefault("errors", [])
                    if isinstance(errors, list):
                        errors.append(f"watchlist_snapshot_batch:{row.get('ticker')}: {row.get('error')}")
    except Exception as exc:
        errors = payload.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(f"watchlist_snapshot_batch: {exc}")

    try:
        batch = datahub_client.get_stock_daily_batch(tickers, limit=10, adjustment="qfq", timeout=12)
        if isinstance(batch, dict):
            for item in batch.get("items") or []:
                if not isinstance(item, dict):
                    continue
                ticker = _normalize_dashboard_ticker(item.get("ticker"))
                rows = item.get("rows")
                if ticker and isinstance(rows, list):
                    daily_by_ticker[ticker] = [row for row in rows if isinstance(row, dict)]
            for row in batch.get("errors") or []:
                if isinstance(row, dict):
                    errors = payload.setdefault("errors", [])
                    if isinstance(errors, list):
                        errors.append(f"watchlist_daily_batch:{row.get('ticker')}: {row.get('error')}")
    except Exception as exc:
        errors = payload.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(f"watchlist_daily_batch: {exc}")

    def load_card_data(card: dict[str, object]) -> dict[str, object]:
        ticker = str(card.get("ticker") or "")
        result = dict(card)
        card_errors: list[str] = []
        try:
            snapshot_payload: object = snapshot_by_ticker.get(ticker)
            if snapshot_payload is None:
                snapshot_payload = datahub_client.get_stock_snapshot(
                    ticker,
                    timeout=8,
                    use_cache=True,
                    auto_refresh_on_404=True,
                )
            snapshot = _unwrap_stock_snapshot_payload(snapshot_payload)
            if isinstance(snapshot, dict):
                snapshot_name = snapshot.get("name") or snapshot.get("display_name") or snapshot.get("short_name")
                if not _is_placeholder_stock_name(snapshot_name, ticker):
                    result["name"] = str(snapshot_name).strip()
                result["current_price"] = _first_present(snapshot, "price", "current_price", "latest_price", "last_price")
                result["change_pct"] = _first_present(snapshot, "change_pct", "pct_chg", "pct_change")
                result["change_amount"] = _first_present(snapshot, "change_amount", "change", "price_change")
                result["updated_at"] = snapshot.get("updated_at") or snapshot.get("trade_time") or result.get("updated_at")
                if isinstance(snapshot_payload, dict):
                    result["time_context"] = snapshot_payload.get("time_context") or _snapshot_time_context_for_sidebar(snapshot)
            if _is_placeholder_stock_name(result.get("name"), ticker):
                resolved_name = _lookup_stock_display_name(ticker)
                if resolved_name:
                    result["name"] = resolved_name
        except Exception as exc:
            card_errors.append(f"watchlist_snapshot:{ticker}: {exc}")

        try:
            daily_rows: object = daily_by_ticker.get(ticker)
            if daily_rows is None:
                daily_rows = datahub_client.get_stock_daily(
                    ticker,
                    limit=10,
                    adjustment="qfq",
                    timeout=8,
                    use_cache=True,
                )
            if isinstance(daily_rows, list):
                series = []
                for row in daily_rows[-5:]:
                    if not isinstance(row, dict):
                        continue
                    trade_date = row.get("trade_date")
                    open_price = row.get("open")
                    high_price = row.get("high")
                    low_price = row.get("low")
                    close_price = row.get("close")
                    if trade_date is None:
                        continue
                    if any(value is None for value in (open_price, high_price, low_price, close_price)):
                        continue
                    series.append(
                        {
                            "date": trade_date,
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "close": close_price,
                        }
                    )
                result["five_day_series"] = series
                if len(series) >= 2:
                    start = series[0].get("close")
                    end = series[-1].get("close")
                    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start:
                        result["five_day_return_pct"] = ((end - start) / start) * 100
        except Exception as exc:
            card_errors.append(f"watchlist_daily:{ticker}: {exc}")
        if card_errors:
            errors = payload.setdefault("errors", [])
            if isinstance(errors, list):
                errors.extend(card_errors)
        return result

    with ThreadPoolExecutor(max_workers=min(5, len(cards))) as executor:
        future_map = {executor.submit(load_card_data, card): card.get("ticker") for card in cards}
        resolved_cards: list[dict[str, object]] = []
        for future in as_completed(future_map):
            try:
                resolved_cards.append(future.result())
            except Exception as exc:
                ticker = future_map[future]
                errors = payload.setdefault("errors", [])
                if isinstance(errors, list):
                    errors.append(f"watchlist_card:{ticker}: {exc}")

    card_order = {str(card.get("ticker")): idx for idx, card in enumerate(cards)}
    resolved_cards.sort(key=lambda row: card_order.get(str(row.get("ticker")), 9999))
    return resolved_cards[:5]


def _collect_dashboard_refresh_targets() -> tuple[list[str], list[str]]:
    """收集刷新目标（优化版：复用已加载的数据）"""
    tickers: set[str] = set()
    errors: list[str] = []

    # 从全局状态读取（避免重复请求）
    state = dashboard_state.get_state()

    for label in ["positions", "watchlist"]:
        rows = state.get(label, [])
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            ticker = _normalize_dashboard_ticker(item.get("ticker"))
            if ticker:
                tickers.add(ticker)

    try:
        for rows in (
            datahub_client.get_positions(timeout=8, use_cache=False),
            datahub_client.get_watchlist(timeout=8, use_cache=False),
        ):
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                ticker = _normalize_dashboard_ticker(item.get("ticker"))
                if ticker:
                    tickers.add(ticker)
    except Exception as exc:
        errors.append(f"refresh_targets: {exc}")

    return sorted(tickers), errors


def _refresh_dashboard_ticker_snapshot(ticker: str) -> Any:
    return datahub_client.refresh_stock_snapshot(ticker, timeout=_DASHBOARD_REFRESH_SNAPSHOT_TIMEOUT_SECONDS)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "name": "FinClaw"}


@app.get("/api/sessions")
def list_sessions(limit: int = 50):
    return {"sessions": chat_session_store.list_sessions(limit=limit)}


@app.post("/api/sessions")
def create_session(payload: SessionCreateRequest):
    return chat_session_store.create_session(title=payload.title)


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, payload: SessionRenameRequest):
    try:
        return chat_session_store.rename_session(session_id, payload.title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    try:
        result = chat_session_store.delete_session(session_id)
        approval_store.delete_by_session(session_id)
        trace_store.delete_by_session(session_id)
        return {"status": "deleted", **result}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/chat/stream")
def chat_stream(payload: ChatRequest):
    return StreamingResponse(
        agent_loop.stream(payload.message, payload.session_id, mode=payload.mode),
        media_type="text/event-stream",
    )


@app.get("/api/sessions/{session_id}/messages")
def list_session_messages(session_id: str, after_id: int = 0, limit: int = 100):
    continuation_service.kick()
    return chat_session_store.list_messages(session_id, after_id=after_id, limit=limit)


@app.post("/api/sessions/{session_id}/cancel")
def cancel_session_run(session_id: str, run_id: str | None = None):
    cancellation_store.request_cancel(session_id, run_id)
    return {"status": "cancel_requested", "session_id": session_id, "run_id": run_id}


@app.post("/api/sessions/{session_id}/interrupt")
def interrupt_session_run(session_id: str, payload: InterruptRequest):
    try:
        return interruption_service.interrupt(session_id, payload.message, payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/tools")
def list_tools():
    return [
        {
            "name": item.name,
            "description": item.description,
            "permission": item.permission.value,
            "parameters": item.parameters,
        }
        for item in tool_gateway.registry.list_tools()
    ]


@app.get("/api/capabilities")
def list_capabilities(visibility: str = "external"):
    return {"capabilities": capability_service.list_modules(visibility=visibility)}


@app.get("/api/capabilities/{module_id}")
def get_capability(module_id: str):
    try:
        return capability_service.get_module(module_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/capabilities/{module_id}")
def update_capability(module_id: str, payload: CapabilityUpdateRequest):
    try:
        return capability_service.update_module(module_id, payload.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/capabilities/{module_id}/health")
def check_capability_health(module_id: str):
    try:
        return capability_service.health(module_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/reports/{report_id}/view")
def view_report(report_id: str, format: str | None = None):
    try:
        record = report_library.resolve_record(report_id)
        artifact = report_library._select_artifact(record, format or record.preferred_view) or report_library._first_artifact(record)
        if artifact is None:
            raise FileNotFoundError(f"report artifact not found: {report_id}")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(artifact.internal_path)
    if artifact.format.lower() == "html":
        return HTMLResponse(path.read_text(encoding="utf-8", errors="ignore"))
    if artifact.format.lower() in {"md", "txt"}:
        return PlainTextResponse(path.read_text(encoding="utf-8", errors="ignore"))
    return FileResponse(path, filename=path.name)


@app.get("/api/reports/{report_id}/download")
def download_report(report_id: str, format: str | None = None):
    try:
        record = report_library.resolve_record(report_id)
        artifact = report_library._select_artifact(record, format or record.preferred_view) or report_library._first_artifact(record)
        if artifact is None:
            raise FileNotFoundError(f"report artifact not found: {report_id}")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = Path(artifact.internal_path)
    return FileResponse(path, filename=path.name)


def _raise_tradinggraph_error(exc: TradingGraphError) -> None:
    raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


@app.get("/api/tradinggraph/health")
def tradinggraph_health():
    try:
        return tradinggraph_service.health()
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.get("/api/tradinggraph/mainlines")
def tradinggraph_mainlines():
    try:
        return tradinggraph_service.read_industry_graph(action="list_mainlines")
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.get("/api/tradinggraph/graph")
def tradinggraph_graph(mainline: str | None = None, include_osint: bool = True, full: bool = False):
    try:
        return tradinggraph_service.read_industry_graph(
            action="get_graph_summary",
            mainline=mainline or "",
            include_osint=include_osint,
        )
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.get("/api/tradinggraph/nodes/{node_id}")
def tradinggraph_node(
    node_id: str,
    include_neighbors: bool = False,
    mainline: str | None = None,
    include_osint: bool = True,
    mode: str = "overview",
    field: str = "",
    offset: int = 0,
    limit: int = 0,
    max_chars: int = 11000,
):
    try:
        return tradinggraph_service.read_industry_graph_node(
            node_id,
            include_neighbors=include_neighbors,
            mainline=mainline or "",
            include_osint=include_osint,
            mode=mode,
            field=field,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.get("/api/tradinggraph/runs/{run_id}")
def tradinggraph_run(run_id: str):
    try:
        return tradinggraph_service.read_industry_graph(action="get_run_status", run_id=run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.post("/api/tradinggraph/control")
def tradinggraph_control(payload: dict):
    try:
        return tradinggraph_service.control_industry_graph(
            action=str(payload.get("action") or ""),
            mode=str(payload.get("mode") or "mainline"),
            query=str(payload.get("query") or ""),
            run_id=str(payload.get("run_id") or ""),
            node_ids=payload.get("node_ids") if isinstance(payload.get("node_ids"), list) else None,
            markets=payload.get("markets") if isinstance(payload.get("markets"), list) else None,
            budget=payload.get("budget") if isinstance(payload.get("budget"), dict) else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TradingGraphError as exc:
        _raise_tradinggraph_error(exc)


@app.get("/api/tradinggraph/view")
def tradinggraph_view(mainline: str | None = None, node_id: str | None = None, run_id: str | None = None):
    mainline_label = html.escape(mainline or "全部主线")
    node_label = html.escape(node_id or "")
    run_label = html.escape(run_id or "")
    graph_query = f"?mainline={html.escape(mainline or '')}" if mainline else ""
    external_url = tradinggraph_service.external_view_url(mainline=mainline, node_id=node_id, run_id=run_id)
    external_link = (
        f'<a class="button primary" href="{html.escape(external_url)}" target="_blank" rel="noreferrer">打开产业链透视前端</a>'
        if external_url
        else '<span class="muted">未配置 TRADINGGRAPH_WEB_BASE，当前仅提供 FinClaw 代理 API。</span>'
    )
    node_link = (
        f'<a class="button" href="/api/tradinggraph/nodes/{html.escape(node_id)}" target="_blank" rel="noreferrer">查看节点 JSON</a>'
        if node_id
        else ""
    )
    run_link = (
        f'<a class="button" href="/api/tradinggraph/runs/{html.escape(run_id)}" target="_blank" rel="noreferrer">查看任务状态</a>'
        if run_id
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>产业链透视</title>
  <style>
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f2ea; color: #171717; }}
    main {{ max-width: 860px; margin: 48px auto; padding: 28px; background: #fffaf0; border: 1px solid #ddd1bd; border-radius: 22px; box-shadow: 0 18px 45px rgba(52, 45, 30, .12); }}
    h1 {{ margin: 0 0 10px; font-size: 26px; }}
    p {{ line-height: 1.7; }}
    code {{ background: #efe5d3; padding: 2px 6px; border-radius: 7px; }}
    .meta {{ display: grid; gap: 8px; margin: 18px 0 22px; }}
    .button {{ display: inline-block; margin: 6px 8px 6px 0; padding: 10px 14px; border-radius: 999px; border: 1px solid #cab99d; color: #211b12; text-decoration: none; background: #fff; }}
    .primary {{ background: #1d1b16; color: #fff; border-color: #1d1b16; }}
    .muted {{ color: #746a5b; }}
  </style>
</head>
<body>
  <main>
    <h1>产业链透视：产业链瓶颈图谱</h1>
    <p class="muted">这是 FinClaw 安全代理页。Agent 和前端应使用本页或下方 API 链接，不要自行拼接 localhost 端口。</p>
    <section class="meta">
      <div>主线：<code>{mainline_label}</code></div>
      <div>节点：<code>{node_label or "-"}</code></div>
      <div>任务：<code>{run_label or "-"}</code></div>
    </section>
    <div>
      {external_link}
      <a class="button" href="/api/tradinggraph/mainlines" target="_blank" rel="noreferrer">查看主线列表</a>
      <a class="button" href="/api/tradinggraph/graph{graph_query}" target="_blank" rel="noreferrer">查看图谱摘要</a>
      {node_link}
      {run_link}
    </div>
  </main>
</body>
</html>"""
    )


@app.get("/api/dashboard/sidebar")
def dashboard_sidebar():
    return _build_dashboard_sidebar_payload()


@app.get("/api/portfolio/performance")
def portfolio_performance():
    try:
        return portfolio_ledger_service.get_performance(recent_limit=5)
    except HTTPError as exc:
        detail = str(exc)
        if exc.response is not None:
            detail = f"{exc.response.status_code} {exc.response.reason}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc


@app.get("/api/portfolio/transactions")
def portfolio_transactions(limit: int = 50):
    return portfolio_ledger_service.list_transactions(limit=limit)


@app.get("/api/portfolio/decisions")
def portfolio_decisions(status: str | None = None, limit: int = 50):
    return portfolio_ledger_service.list_decisions(status=status, limit=limit)


@app.post("/api/portfolio/transactions/draft")
def draft_portfolio_transaction(payload: dict[str, Any]):
    try:
        return portfolio_ledger_service.draft_transaction(payload, session_id=str(payload.get("session_id") or "api"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPError as exc:
        detail = str(exc)
        if exc.response is not None:
            detail = f"{exc.response.status_code} {exc.response.reason}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc


@app.post("/api/portfolio/transactions/{transaction_id}/confirm")
def confirm_portfolio_transaction(transaction_id: str, payload: dict[str, Any] | None = None):
    try:
        return portfolio_ledger_service.confirm_transaction(transaction_id, updates=payload or {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPError as exc:
        detail = str(exc)
        if exc.response is not None:
            detail = f"{exc.response.status_code} {exc.response.reason}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc


@app.post("/api/portfolio/decisions/{decision_id}/review")
def review_portfolio_decision(decision_id: str, payload: dict[str, Any] | None = None):
    try:
        return portfolio_ledger_service.review_decision(decision_id, payload=payload or {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/dashboard/sidebar/refresh")
def refresh_dashboard_sidebar():
    """流式刷新看板数据（优化版：同步刷新 + SSE 流式返回）"""
    def stream_refresh():
        # 1. 发送当前数据
        payload = _build_dashboard_sidebar_payload()
        yield sse_event("sidebar_data", payload)

        # 2. 收集刷新目标
        tickers, ticker_errors = _collect_dashboard_refresh_targets()
        if ticker_errors:
            for error in ticker_errors:
                yield sse_event("refresh_warning", {"message": error})

        yield sse_event("refresh_started", {
            "targets": tickers,
            "total": len(tickers),
            "started_at": datetime.utcnow().isoformat()
        })

        # 标记刷新开始
        dashboard_state.set_refresh_in_progress(True)
        log_event("dashboard_sidebar_refresh_started", {"targets": tickers})

        refresh_errors: list[str] = []
        refreshed_targets: list[str] = []

        try:
            news_refresh = datahub_client.refresh_market_news(limit=9, timeout=45, force=True)
            yield sse_event("refresh_progress", {
                "stage": "news",
                "status": news_refresh.get("status", "ok"),
                "message": news_refresh.get("message", "新闻快照刷新已处理"),
            })
        except Exception as exc:
            error_msg = f"news_refresh: {exc}"
            refresh_errors.append(error_msg)
            logger.error(f"Failed to trigger news refresh: {exc}")
            yield sse_event("refresh_progress", {
                "stage": "news",
                "status": "error",
                "error": str(exc),
            })

        try:
            # 3. 同步刷新轻量市场快照：核心指数
            try:
                yield sse_event("refresh_progress", {
                    "stage": "market_indices",
                    "status": "running",
                    "message": "正在刷新核心指数...",
                })
                market_result = datahub_client.refresh_market_indices(timeout=_DASHBOARD_REFRESH_MARKET_TIMEOUT_SECONDS)
                market_status = market_result.get("status") if isinstance(market_result, dict) else "success"
                if market_status == "stale_but_available":
                    yield sse_event("refresh_progress", {
                        "stage": "market_indices",
                        "status": "stale",
                        "message": "核心指数实时源暂不可用，已使用缓存指数",
                    })
                else:
                    yield sse_event("refresh_progress", {
                        "stage": "market_indices",
                        "status": "ok",
                        "message": "核心指数已刷新",
                    })
            except Exception as exc:
                error_msg = f"market_indices: {exc}"
                refresh_errors.append(error_msg)
                logger.error(f"Failed to refresh market indices: {exc}")
                yield sse_event("refresh_progress", {
                    "stage": "market_indices",
                    "status": "error",
                    "error": str(exc)
                })

            # 4. 并发刷新股票实时快照
            if tickers:
                max_workers = min(3, len(tickers))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(_refresh_dashboard_ticker_snapshot, ticker): ticker
                        for ticker in tickers
                    }
                    completed_count = 0
                    for future in as_completed(future_map):
                        ticker = future_map[future]
                        completed_count += 1
                        try:
                            result = future.result()
                            status = result.get("status") if isinstance(result, dict) else "success"
                            if status == "stale_but_available":
                                yield sse_event("refresh_progress", {
                                    "stage": "snapshot",
                                    "ticker": ticker,
                                    "progress": f"{completed_count}/{len(tickers)}",
                                    "percentage": int(completed_count / len(tickers) * 100),
                                    "status": "stale",
                                    "message": "实时行情源暂不可用，已使用缓存快照",
                                })
                            else:
                                refreshed_targets.append(ticker)
                                yield sse_event("refresh_progress", {
                                    "stage": "snapshot",
                                    "ticker": ticker,
                                    "progress": f"{completed_count}/{len(tickers)}",
                                    "percentage": int(completed_count / len(tickers) * 100),
                                    "status": "ok"
                                })
                        except Exception as exc:
                            error_msg = f"snapshot:{ticker}: {exc}"
                            refresh_errors.append(error_msg)
                            logger.error(f"Failed to refresh {ticker}: {exc}")
                            yield sse_event("refresh_progress", {
                                "stage": "snapshot",
                                "ticker": ticker,
                                "progress": f"{completed_count}/{len(tickers)}",
                                "percentage": int(completed_count / len(tickers) * 100),
                                "status": "error",
                                "error": str(exc)
                            })

            # 5. 重新加载最新数据
            final_payload = _build_dashboard_sidebar_payload()
            yield sse_event("sidebar_data", final_payload)

            # 6. 发送完成事件
            yield sse_event("refresh_completed", {
                "refreshed": refreshed_targets,
                "errors": refresh_errors,
                "completed_at": datetime.utcnow().isoformat()
            })

            log_event("dashboard_sidebar_refresh_completed", {
                "targets": refreshed_targets,
                "errors": refresh_errors,
            })

        except Exception as exc:
            logger.error(f"Dashboard refresh failed: {exc}")
            yield sse_event("refresh_failed", {
                "error": str(exc),
                "errors": refresh_errors
            })
            log_event("dashboard_sidebar_refresh_failed", {
                "targets": tickers,
                "error": str(exc),
                "errors": refresh_errors,
            })

        finally:
            dashboard_state.set_refresh_in_progress(False)

    return StreamingResponse(stream_refresh(), media_type="text/event-stream")


@app.get("/api/actions/pending")
def list_pending_actions():
    return [item.model_dump() for item in approval_store.list_pending()]


@app.get("/api/sessions/{session_id}/approvals")
def get_session_approvals(session_id: str):
    _reconcile_approval_queue(session_id)
    queue = chat_session_store.get_approval_queue(session_id)
    active_id = queue.get("active_action_id")
    queued_ids = queue.get("queued_action_ids") or []
    active = approval_store.get(active_id) if active_id else None
    queued = [approval_store.get(action_id) for action_id in queued_ids]
    return {
        "session_id": session_id,
        "active_action": active.model_dump() if active else None,
        "queued_actions": [item.model_dump() for item in queued if item is not None],
        "queue_size": len(queued_ids),
    }


def _reconcile_approval_queue(session_id: str) -> None:
    queue = chat_session_store.get_approval_queue(session_id)
    active_id = queue.get("active_action_id")
    if active_id:
        active = approval_store.get(active_id)
        if active and active.status == "pending":
            return
        chat_session_store.advance_approval_queue(session_id, active_id)
        queue = chat_session_store.get_approval_queue(session_id)
        if queue.get("active_action_id"):
            return
    pending = [
        item
        for item in approval_store.list_pending()
        if item.session_id == session_id and item.action_id not in (queue.get("queued_action_ids") or [])
    ]
    if pending:
        chat_session_store.enqueue_approval_action(session_id, pending[0].action_id)


@app.get("/api/sessions/{session_id}/security")
def get_session_security(session_id: str):
    return chat_session_store.get_security_settings(session_id).model_dump()


@app.patch("/api/sessions/{session_id}/security")
def update_session_security(session_id: str, payload: SessionSecuritySettings):
    return chat_session_store.update_security_settings(
        session_id,
        approval_policy=payload.approval_policy.value,
        allowed_tool_groups=payload.allowed_tool_groups,
    ).model_dump()


@app.get("/api/traces/recent")
def list_recent_traces(limit: int = 50):
    return trace_store.recent_traces(limit)


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str):
    trace = trace_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


@app.get("/api/llm-logs")
def list_llm_logs(limit: int = 100, session_id: str | None = None):
    return {"logs": trace_store.recent_llm_logs(limit=limit, session_id=session_id)}


@app.get("/api/llm-logs/{log_id}")
def get_llm_log(log_id: int):
    log = trace_store.get_llm_log(log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="llm log not found")
    return log


@app.delete("/api/llm-logs")
def clear_llm_logs():
    return trace_store.clear_llm_logs()


@app.get("/api/metrics/summary")
def metrics_summary(limit: int = 500):
    return trace_store.metrics_summary(limit)


@app.get("/api/security/events")
def security_events(limit: int = 100):
    traces = trace_store.recent_traces(limit)
    return traces


@app.get("/api/analysis/jobs")
def list_analysis_jobs(limit: int = 20):
    return [job.model_dump() for job in analysis_job_store.list_jobs(limit)]


@app.get("/api/analysis/jobs/{job_id}")
def get_analysis_job(job_id: str):
    job = analysis_job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="analysis job not found")
    return job.model_dump()


@app.post("/api/analysis/jobs/{job_id}/cancel")
def cancel_analysis_job(job_id: str):
    try:
        return analysis_job_store.cancel(job_id).model_dump()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="analysis job not found") from exc


@app.post("/api/research/threads")
def create_research_thread(request: ResearchThreadCreateRequest):
    try:
        thread = research_thread_service.start_thread(
            subject=request.subject,
            subject_type=request.subject_type,
            depth=request.depth,
            session_id=request.session_id,
            user_goal=request.user_goal,
            research_goal=request.research_goal,
            subject_hint=request.subject_hint,
            scope_hint=request.scope_hint,
            budget_profile=request.budget_profile,
            allowed_tools=request.allowed_tools,
            blocked_tools=request.blocked_tools,
            constraints=request.constraints,
            auto_start=True,
            force_new=request.force_new,
        )
        return research_thread_service.compact_thread(thread)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/research/threads")
def list_research_threads(
    session_id: str | None = None,
    status: str | None = None,
    subject: str | None = None,
    limit: int = 20,
    detail: str = "summary",
):
    return research_thread_service.list_threads(
        session_id=session_id,
        status=status,
        subject=subject,
        limit=limit,
        detail="summary",
    )


@app.get("/api/research/threads/{thread_id}")
def get_research_thread(thread_id: str, detail: str = "summary"):
    try:
        thread = research_thread_service.get_thread(thread_id)
        return research_thread_service.compact_thread(thread)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="research thread not found") from exc


@app.post("/api/research/threads/{thread_id}/control")
def control_research_thread(thread_id: str, request: ResearchThreadControlRequest):
    try:
        payload = research_thread_service.control_thread(thread_id, request.action)
        if isinstance(payload.get("thread"), dict):
            payload["thread"] = research_thread_service.compact_thread(payload["thread"])
        return payload
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="research thread not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/research/records")
def list_research_records(subject_type: str | None = None, query: str | None = None, limit: int = 50):
    return research_thread_service.list_records(subject_type=subject_type, query=query, limit=limit)


@app.get("/api/research/records/{record_id:path}")
def get_research_record(record_id: str, section: str | None = None, max_chars: int = 12000, offset: int = 0):
    try:
        return research_thread_service.get_record(record_id=record_id, section=section, max_chars=max_chars, offset=offset)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="research record not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/actions/{action_id}/confirm")
def confirm_action(action_id: str, payload: ConfirmRequest):
    action = approval_store.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    queue = chat_session_store.get_approval_queue(action.session_id)
    if queue.get("active_action_id") != action_id:
        raise HTTPException(status_code=409, detail="action is not the active approval")
    trace_id = None
    if not payload.approved:
        approval_store.mark(action_id, "denied")
        trace_id = trace_store.start_trace(action.session_id, action.run_id or action.action_id, "approval_denied", {"action_id": action_id})
        trace_store.event(trace_id, "approval.denied", {"action": action.model_dump()}, level="info")
        trace_store.finish_trace(trace_id)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        return {"status": "rejected", "action": action.model_dump()}
    if payload.arguments:
        approval_store.update(action_id, arguments=payload.arguments, status="pending")
    try:
        trace_id = trace_store.start_trace(action.session_id, action.run_id or action.action_id, "approval_confirm", {"action_id": action_id})
        result = tool_gateway.execute_confirmed(action_id, trace_id=trace_id)
        trace_store.finish_trace(trace_id)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        return {"status": "executed", "action": action.model_dump(), "result": result}
    except ResourceLimitExceeded as exc:
        detail = str(exc)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=detail)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        raise HTTPException(status_code=429, detail=detail) from exc
    except HTTPError as exc:
        approval_store.mark(action_id, "failed")
        approval_store.update(action_id, error=str(exc))
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
        detail = str(exc)
        if exc.response is not None:
            detail = f"{exc.response.status_code} {exc.response.reason}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        approval_store.mark(action_id, "failed")
        approval_store.update(action_id, error=str(exc))
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/actions/{action_id}/confirm/stream")
def confirm_action_stream(action_id: str, payload: ConfirmRequest):
    action = approval_store.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    queue = chat_session_store.get_approval_queue(action.session_id)
    if queue.get("active_action_id") != action_id:
        raise HTTPException(status_code=409, detail="action is not the active approval")
    trace_id = None
    if not payload.approved:
        approval_store.mark(action_id, "denied")
        trace_id = trace_store.start_trace(action.session_id, action.run_id or action.action_id, "approval_denied", {"action_id": action_id})
        trace_store.event(trace_id, "approval.denied", {"action": action.model_dump()}, level="info")
        trace_store.finish_trace(trace_id)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        return StreamingResponse(
            agent_loop.stream_after_denial(
                action.tool_name,
                action.arguments,
                session_id=action.session_id,
            ),
            media_type="text/event-stream",
        )
    if payload.arguments:
        updated = approval_store.update(action_id, arguments=payload.arguments, status="pending")
        if updated:
            action = updated
    try:
        trace_id = trace_store.start_trace(action.session_id, action.run_id or action.action_id, "approval_confirm_stream", {"action_id": action_id})
        executed = tool_gateway.execute_confirmed(action_id, trace_id=trace_id)
        trace_store.finish_trace(trace_id)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
    except ResourceLimitExceeded as exc:
        detail = str(exc)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=detail)
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        raise HTTPException(status_code=429, detail=detail) from exc
    except HTTPError as exc:
        approval_store.mark(action_id, "failed")
        approval_store.update(action_id, error=str(exc))
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
        detail = str(exc)
        if exc.response is not None:
            detail = f"{exc.response.status_code} {exc.response.reason}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        approval_store.mark(action_id, "failed")
        approval_store.update(action_id, error=str(exc))
        chat_session_store.advance_approval_queue(action.session_id, action_id)
        if trace_id:
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return StreamingResponse(
        agent_loop.stream_after_approval(
            action.tool_name,
            action.arguments,
            executed,
            session_id=action.session_id,
        ),
        media_type="text/event-stream",
    )
