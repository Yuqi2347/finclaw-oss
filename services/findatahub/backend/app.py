from datetime import datetime
import logging
import threading
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from . import models
from .config import apply_network_settings, settings
from .db import Base, SessionLocal, engine, get_db
from .json_utils import sanitize_json_dict
from .schema_upgrade import run_schema_upgrades
from .providers.symbol import normalize_a_share_ticker
from .schemas import DailyPlanUpsert, InstrumentIndexRefreshRequest, MarketPackageRefreshRequest, PositionPatch, PositionUpsert, RefreshRequest, TriggerEventStatusUpdate, TriggerRuleCreate, WatchlistCreate
from .services.fundamental_service import FundamentalService
from .services.market_context_service import MarketContextService
from .services.market_news_service import MarketNewsService
from .services.market_package_service import MarketPackageService
from .services.market_service import MarketService
from .services.news_provider import NewsProvider
from .services.plan_service import PlanService
from .services.portfolio_service import PortfolioService
from .services.refresh_log_service import RefreshLogService
from .services.stock_package_service import StockPackageService
from .services.trigger_service import TriggerService
from .services.watchlist_service import WatchlistService


apply_network_settings()
run_schema_upgrades(engine)
Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.api_title)
logger = logging.getLogger(__name__)

market_service = MarketService()
watchlist_service = WatchlistService()
portfolio_service = PortfolioService()
trigger_service = TriggerService()
plan_service = PlanService()
fundamental_service = FundamentalService()
market_context_service = MarketContextService()
market_package_service = MarketPackageService()
refresh_log_service = RefreshLogService()
stock_package_service = StockPackageService()
news_provider = NewsProvider()
market_news_service = MarketNewsService()
_instrument_index_startup_started = False
_market_news_scheduler_started = False


def _bootstrap_instrument_index_if_needed() -> None:
    db = SessionLocal()
    try:
        total = db.query(models.Instrument).count()
        if total >= market_service.INSTRUMENT_BOOTSTRAP_THRESHOLD:
            return
        logger.info("Instrument index too small (%s), refreshing in background", total)
        result = market_service.refresh_instrument_index(db)
        logger.info("Instrument index background refresh completed: %s", result)
    except Exception as exc:
        logger.warning("Instrument index background refresh failed: %s", exc)
    finally:
        db.close()


def _market_news_scheduler_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            result = market_news_service.refresh_snapshot(db, force=False, limit=6)
            logger.info("Market news refresh tick: %s", result.get("status") if isinstance(result, dict) else result)
        except Exception as exc:
            logger.warning("Market news refresh tick failed: %s", exc)
        finally:
            db.close()
        threading.Event().wait(30 * 60)


def startup_market_news_scheduler() -> None:
    global _market_news_scheduler_started
    if _market_news_scheduler_started:
        return
    _market_news_scheduler_started = True
    thread = threading.Thread(target=_market_news_scheduler_loop, name="market-news-refresh", daemon=True)
    thread.start()


@app.on_event("startup")
def startup_refresh_instrument_index() -> None:
    global _instrument_index_startup_started
    if _instrument_index_startup_started:
        return
    _instrument_index_startup_started = True
    thread = threading.Thread(target=_bootstrap_instrument_index_if_needed, name="instrument-index-bootstrap", daemon=True)
    thread.start()


@app.on_event("startup")
def startup_refresh_market_news() -> None:
    startup_market_news_scheduler()


def serialize(obj):
    if obj is None:
        return None
    data = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        data[col.name] = value.isoformat() if hasattr(value, "isoformat") else value
    return sanitize_json_dict(data)


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _trusted_stock_name(db: Session, ticker: str | None) -> str | None:
    if not ticker:
        return None
    normalized = normalize_a_share_ticker(ticker)
    profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
    profile_name = _clean_text(profile.name if profile else None)
    if profile_name:
        return profile_name
    instrument = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
    return _clean_text(instrument.name if instrument else None)


def _serialize_snapshot(db: Session, snapshot: models.PriceRealtimeSnapshot | None) -> dict[str, Any] | None:
    data = serialize(snapshot)
    if not isinstance(data, dict):
        return data
    trusted_name = _trusted_stock_name(db, str(data.get("ticker") or ""))
    if trusted_name:
        data["name"] = trusted_name
    return data


def _ms_since(started_at: datetime) -> int:
    return int((datetime.utcnow() - started_at).total_seconds() * 1000)


def _daily_adjustment_from_sources(sources: set[str]) -> str:
    if not sources:
        return "unknown"
    if sources.issubset({"baostock", "akshare"}):
        return "qfq"
    return "unknown"


def _summarize_daily_series(
    rows: list[models.PriceDaily],
    snapshot: models.PriceRealtimeSnapshot | None = None,
) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda row: row.trade_date)
    sources = sorted({str(row.source or "").strip() for row in ordered_rows if str(row.source or "").strip()})
    warnings: list[str] = []
    blocking_issues: list[str] = []

    if not ordered_rows:
        blocking_issues.append("no_daily_rows")
        return {
            "status": "blocked",
            "row_count": 0,
            "sources": sources,
            "adjustment": "unknown",
            "warnings": warnings,
            "blocking_issues": blocking_issues,
            "earliest_trade_date": None,
            "latest_trade_date": None,
            "latest_close": None,
            "last_refreshed_at": None,
        }

    for row in ordered_rows:
        prices = [row.open, row.high, row.low, row.close]
        if any(value is None for value in prices):
            blocking_issues.append("missing_ohlc")
            break
        if any(value is not None and value <= 0 for value in prices):
            blocking_issues.append("non_positive_price")
            break
        if row.low is not None and row.high is not None and row.low > row.high:
            blocking_issues.append("invalid_ohlc")
            break
        if row.open is not None and row.close is not None and row.high is not None and row.low is not None:
            if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
                blocking_issues.append("invalid_ohlc")
                break

    if len(sources) > 1:
        warnings.append(f"mixed_daily_sources:{','.join(sources)}")

    adjustment = _daily_adjustment_from_sources(set(sources))
    if adjustment == "unknown":
        warnings.append(f"daily_adjustment_unknown:{','.join(sources) or 'none'}")

    latest_row = ordered_rows[-1]
    last_refreshed_at = max((row.created_at for row in ordered_rows if row.created_at is not None), default=None)
    if latest_row.trade_date:
        age_days = (datetime.utcnow().date() - latest_row.trade_date).days
        if age_days > 10:
            warnings.append(f"daily_series_stale:{age_days}d")

    if snapshot is None:
        warnings.append("missing_snapshot")

    status = "blocked" if blocking_issues else ("warn" if warnings else "ready")
    return {
        "status": status,
        "row_count": len(ordered_rows),
        "sources": sources,
        "adjustment": adjustment,
        "warnings": warnings,
        "blocking_issues": blocking_issues,
        "earliest_trade_date": ordered_rows[0].trade_date.isoformat() if ordered_rows[0].trade_date else None,
        "latest_trade_date": latest_row.trade_date.isoformat() if latest_row.trade_date else None,
        "latest_close": latest_row.close,
        "last_refreshed_at": last_refreshed_at.isoformat() if last_refreshed_at else None,
    }


def _run_logged(db: Session, job_type: str, ticker: str | None, fn):
    started_at = datetime.utcnow()
    try:
        result = fn()
        status = "success"
        if isinstance(result, dict) and result.get("status") in {"success", "stale_but_available"}:
            status = str(result["status"])
        refresh_log_service.log(
            db,
            job_type=job_type,
            status=status,
            ticker=ticker,
            duration_ms=_ms_since(started_at),
            raw=sanitize_json_dict(result) if isinstance(result, dict) else None,
        )
        return result
    except Exception as exc:
        db.rollback()
        refresh_log_service.log(
            db,
            job_type=job_type,
            status="failed",
            ticker=ticker,
            message=str(exc),
            duration_ms=_ms_since(started_at),
        )
        raise


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/instruments/search")
def search_instruments(query: str, limit: int = 10, db: Session = Depends(get_db)):
    return market_service.search_instruments(db, query, limit)


@app.post("/api/instruments/refresh")
def refresh_instrument_index(payload: InstrumentIndexRefreshRequest | None = None, db: Session = Depends(get_db)):
    try:
        request = payload or InstrumentIndexRefreshRequest()

        def run():
            return market_service.refresh_instrument_index(db, request.limit)

        return _run_logged(db, "instrument_index", None, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场标的索引刷新失败: {exc}") from exc


@app.get("/api/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    return market_service.dashboard_summary(db)


@app.get("/api/watchlist")
def list_watchlist(db: Session = Depends(get_db)):
    items = watchlist_service.list_items(db)
    return [serialize(item) for item in items]


@app.post("/api/watchlist")
def add_watchlist_item(payload: WatchlistCreate, db: Session = Depends(get_db)):
    item = watchlist_service.add_item(db, payload)
    market_service.ensure_instrument(db, item.ticker, item.name)
    return serialize(item)


@app.delete("/api/watchlist/{ticker}")
def delete_watchlist_item(ticker: str, list_name: str = "默认关注", db: Session = Depends(get_db)):
    ok = watchlist_service.delete_item(db, ticker, list_name)
    if not ok:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return {"success": True}


@app.post("/api/market/refresh/snapshot/{ticker}")
def refresh_snapshot(ticker: str, db: Session = Depends(get_db)):
    try:
        def run():
            result = market_service.refresh_snapshot_fast(db, ticker)
            snapshot = result.get("snapshot")
            if result.get("status") == "failed":
                raise RuntimeError("; ".join(result.get("errors") or []) or result.get("message") or "snapshot refresh failed")
            events = []
            if result.get("status") == "success":
                events = trigger_service.evaluate_ticker(db, ticker)
            payload = dict(result)
            payload["snapshot"] = _serialize_snapshot(db, snapshot)
            payload["trigger_events"] = [serialize(event) for event in events]
            return payload

        return _run_logged(db, "snapshot", ticker, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"实时快照刷新失败: {exc}") from exc


@app.post("/api/market/refresh/daily")
def refresh_daily(payload: RefreshRequest, db: Session = Depends(get_db)):
    try:
        def run():
            refresh_result = market_service.refresh_daily_prices(db, payload.ticker, payload.start_date, payload.end_date)
            package = stock_package_service.build_package(db, payload.ticker)
            return {
                "ticker": package["ticker"],
                "refresh": refresh_result,
                "daily_meta": package["daily_meta"],
                "quality": package["quality"],
            }

        return _run_logged(db, "daily", payload.ticker, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"日线刷新失败: {exc}") from exc


@app.post("/api/news/refresh/{ticker}")
def refresh_news(ticker: str, limit: int = 20, db: Session = Depends(get_db)):
    try:
        def run():
            normalized = normalize_a_share_ticker(ticker)
            try:
                count = market_service.refresh_news(db, normalized, limit)
                status = "ok"
                error = None
            except Exception as exc:
                db.rollback()
                count = 0
                status = "fallback_only"
                error = str(exc)
            fallback = news_provider.search(db, ticker=normalized, limit=limit)
            return {
                "ticker": normalized,
                "rows": count,
                "status": status,
                "error": error,
                "fallback_rows": len(fallback.get("items") or []),
                "fallback_providers": (fallback.get("meta") or {}).get("providers") or [],
            }

        return _run_logged(db, "news", ticker, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"新闻刷新失败: {exc}") from exc


@app.post("/api/fundamentals/refresh/{ticker}")
def refresh_fundamentals(ticker: str, db: Session = Depends(get_db)):
    def run():
        normalized = normalize_a_share_ticker(ticker)
        if getattr(fundamental_service, "tushare_sync", None) and fundamental_service.tushare_sync.enabled:
            profile_count = fundamental_service.tushare_sync.refresh_company_profile(db, normalized)
            refresh_result = fundamental_service.tushare_sync.refresh_fundamentals(db, normalized)
            profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
            return {
                "ticker": normalized,
                "profile": serialize(profile),
                "profile_rows": profile_count,
                "metrics": refresh_result.get("metrics", 0),
                "income": refresh_result.get("income", 0),
                "balance": refresh_result.get("balance", 0),
                "cashflow": refresh_result.get("cashflow", 0),
                "errors": refresh_result.get("errors", []),
            }
        errors: list[str] = []
        profile = None
        metrics = 0
        income = 0
        balance = 0
        cashflow = 0

        try:
            profile = fundamental_service.refresh_company_profile(db, ticker)
        except Exception as exc:
            db.rollback()
            errors.append(f"profile: {exc}")
        try:
            metrics = fundamental_service.refresh_fundamental_metrics(db, ticker)
        except Exception as exc:
            db.rollback()
            errors.append(f"metrics: {exc}")
        try:
            income = fundamental_service.refresh_financial_statement(db, ticker, "income")
        except Exception as exc:
            db.rollback()
            errors.append(f"income: {exc}")
        try:
            balance = fundamental_service.refresh_financial_statement(db, ticker, "balance")
        except Exception as exc:
            db.rollback()
            errors.append(f"balance: {exc}")
        try:
            cashflow = fundamental_service.refresh_financial_statement(db, ticker, "cashflow")
        except Exception as exc:
            db.rollback()
            errors.append(f"cashflow: {exc}")

        if profile is None and metrics == 0 and income == 0 and balance == 0 and cashflow == 0 and errors:
            raise RuntimeError("; ".join(errors))

        return {
            "ticker": normalized,
            "profile": serialize(profile),
            "metrics": metrics,
            "income": income,
            "balance": balance,
            "cashflow": cashflow,
            "errors": errors,
        }

    try:
        return _run_logged(db, "fundamentals", ticker, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"基本面刷新失败: {exc}") from exc


@app.post("/api/market-context/refresh")
def refresh_market_context(db: Session = Depends(get_db)):
    try:
        def run():
            result = market_context_service.refresh_all_best_effort(db)
            if result["index"] == 0 and result["sector"] == 0 and result["errors"]:
                raise RuntimeError("; ".join(result["errors"]))
            return result

        return _run_logged(db, "market_context", None, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场环境刷新失败: {exc}") from exc


@app.post("/api/market/refresh/indices")
def refresh_market_indices(db: Session = Depends(get_db)):
    try:
        def run():
            result = market_context_service.refresh_core_index_snapshots_fast(db)
            if result.get("status") == "failed":
                raise RuntimeError("; ".join(result.get("errors") or []) or result.get("message") or "core index refresh failed")
            return result

        return _run_logged(db, "market_indices", None, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"核心指数刷新失败: {exc}") from exc


@app.post("/api/market/refresh/breadth")
def refresh_market_breadth(db: Session = Depends(get_db)):
    try:
        def run():
            breadth = market_context_service.refresh_breadth_snapshot(db)
            return {"breadth": breadth, "errors": []}

        return _run_logged(db, "market_breadth", None, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场涨跌家数刷新失败: {exc}") from exc


@app.get("/api/market/overview")
def market_overview(
    limit: int = settings.market_theme_limit,
    include_breadth: bool = True,
    db: Session = Depends(get_db),
):
    return sanitize_json_dict(
        market_context_service.get_market_overview_sidebar_cached(
            db,
            limit=limit,
            include_breadth=include_breadth,
        )
    )


@app.get("/api/market/breadth/latest")
def latest_market_breadth(db: Session = Depends(get_db)):
    row = market_context_service.latest_breadth(db)
    if not row:
        raise HTTPException(status_code=404, detail="market breadth not found")
    return serialize(row)


@app.get("/api/market/fund-flow/latest")
def latest_market_fund_flow(db: Session = Depends(get_db)):
    row = market_context_service.latest_fund_flow(db)
    if not row:
        raise HTTPException(status_code=404, detail="market fund flow not found")
    return serialize(row)


@app.get("/api/market/themes")
def list_market_themes(limit: int = settings.market_theme_limit, db: Session = Depends(get_db)):
    return [serialize(row) for row in market_context_service.latest_theme_snapshots(db, limit)]


@app.get("/api/market/events")
def list_market_events(limit: int = settings.market_event_limit, db: Session = Depends(get_db)):
    return [serialize(row) for row in market_context_service.latest_events(db, limit)]


@app.get("/api/market/international-indices")
def get_international_indices():
    """获取国际指数（纳斯达克、日经225、韩国KOSPI）"""
    return market_context_service.get_international_indices_cached()


@app.post("/api/market/international-indices/refresh")
def refresh_international_indices(db: Session = Depends(get_db)):
    """刷新国际指数快照"""
    count = market_context_service.refresh_international_indices(db)
    return {"refreshed": count}


@app.get("/api/market/snapshot/{ticker}")
def get_snapshot(ticker: str, db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    snapshot = db.query(models.PriceRealtimeSnapshot).filter(
        models.PriceRealtimeSnapshot.ticker == normalized
    ).first()
    if _snapshot_is_missing_or_stale(serialize(snapshot) if snapshot else None):
        try:
            snapshot = market_service.refresh_snapshot(db, normalized)
        except Exception as exc:
            if not snapshot:
                raise HTTPException(status_code=404, detail=f"snapshot not found: {exc}") from exc
            db.rollback()
    return _serialize_snapshot(db, snapshot)


@app.post("/api/market/snapshots/batch")
def get_snapshots_batch(payload: dict[str, Any], db: Session = Depends(get_db)):
    tickers = payload.get("tickers") if isinstance(payload, dict) else []
    if not isinstance(tickers, list):
        raise HTTPException(status_code=400, detail="tickers must be a list")
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for raw in tickers[:50]:
        normalized = normalize_a_share_ticker(str(raw or ""))
        if not normalized:
            continue
        snapshot = db.query(models.PriceRealtimeSnapshot).filter(
            models.PriceRealtimeSnapshot.ticker == normalized
        ).first()
        if _snapshot_is_missing_or_stale(serialize(snapshot) if snapshot else None):
            try:
                snapshot = market_service.refresh_snapshot(db, normalized)
            except Exception as exc:
                db.rollback()
                errors.append({"ticker": normalized, "error": str(exc)})
        if snapshot:
            item = _serialize_snapshot(db, snapshot)
            if item:
                items.append(item)
    return {"items": items, "errors": errors}


@app.get("/api/market/daily/{ticker}")
def get_daily(ticker: str, limit: int = 240, adjustment: str = "qfq", db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    rows = db.query(models.PriceDaily).filter(
        models.PriceDaily.ticker == normalized,
        models.PriceDaily.adjustment == adjustment,
    ).order_by(models.PriceDaily.trade_date.desc()).limit(limit).all()
    if not rows:
        try:
            market_service.refresh_daily_prices(db, normalized)
            rows = db.query(models.PriceDaily).filter(
                models.PriceDaily.ticker == normalized,
                models.PriceDaily.adjustment == adjustment,
            ).order_by(models.PriceDaily.trade_date.desc()).limit(limit).all()
        except Exception:
            db.rollback()
    return [serialize(row) for row in reversed(rows)]


@app.post("/api/market/daily/batch")
def get_daily_batch(payload: dict[str, Any], db: Session = Depends(get_db)):
    tickers = payload.get("tickers") if isinstance(payload, dict) else []
    if not isinstance(tickers, list):
        raise HTTPException(status_code=400, detail="tickers must be a list")
    limit = int(payload.get("limit") or 10)
    adjustment = str(payload.get("adjustment") or "qfq")
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for raw in tickers[:50]:
        normalized = normalize_a_share_ticker(str(raw or ""))
        if not normalized:
            continue
        rows = db.query(models.PriceDaily).filter(
            models.PriceDaily.ticker == normalized,
            models.PriceDaily.adjustment == adjustment,
        ).order_by(models.PriceDaily.trade_date.desc()).limit(limit).all()
        if not rows:
            try:
                market_service.refresh_daily_prices(db, normalized)
                rows = db.query(models.PriceDaily).filter(
                    models.PriceDaily.ticker == normalized,
                    models.PriceDaily.adjustment == adjustment,
                ).order_by(models.PriceDaily.trade_date.desc()).limit(limit).all()
            except Exception as exc:
                db.rollback()
                errors.append({"ticker": normalized, "error": str(exc)})
        items.append({"ticker": normalized, "rows": [serialize(row) for row in reversed(rows)]})
    return {"items": items, "errors": errors}


@app.get("/api/indicators/{ticker}")
def get_indicators(ticker: str, limit: int = 240, adjustment: str = "qfq", db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    rows = db.query(models.TechnicalIndicator).filter(
        models.TechnicalIndicator.ticker == normalized,
        models.TechnicalIndicator.adjustment == adjustment,
    ).order_by(models.TechnicalIndicator.trade_date.desc()).limit(limit).all()
    return [serialize(row) for row in reversed(rows)]


@app.get("/api/news/search")
def search_news(
    query: str | None = None,
    ticker: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    include_web: bool = False,
    db: Session = Depends(get_db),
):
    return news_provider.search(
        db,
        ticker=ticker,
        query=query,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        include_web=include_web,
    )


@app.get("/api/news/market-snapshot")
def get_market_news_snapshot(limit: int = 6, db: Session = Depends(get_db)):
    return market_news_service.get_sidebar_snapshot(db, limit=limit)


@app.post("/api/news/market-refresh")
def refresh_market_news(force: bool = True, limit: int = 6, db: Session = Depends(get_db)):
    try:
        return market_news_service.refresh_snapshot(db, force=force, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场新闻刷新失败: {exc}") from exc


@app.get("/api/news/{ticker}")
def get_news(ticker: str, limit: int = 50, db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    return news_provider.search(db, ticker=normalized, limit=limit).get("items") or []


@app.get("/api/fundamentals/profile/{ticker}")
def get_company_profile(ticker: str, db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    row = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
    if not row:
        try:
            row = fundamental_service.refresh_company_profile(db, normalized)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"company profile not found: {exc}") from exc
    return serialize(row)


@app.get("/api/fundamentals/metrics/{ticker}")
def get_fundamental_metrics(ticker: str, limit: int = 8, db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    rows = db.query(models.FundamentalMetric).filter(
        models.FundamentalMetric.ticker == normalized
    ).order_by(models.FundamentalMetric.report_date.desc()).limit(limit).all()
    return [serialize(row) for row in rows]


@app.get("/api/fundamentals/statements/{ticker}/{statement_type}")
def get_financial_statements(ticker: str, statement_type: str, limit: int = 8, db: Session = Depends(get_db)):
    normalized = normalize_a_share_ticker(ticker)
    rows = db.query(models.FinancialStatement).filter(
        models.FinancialStatement.ticker == normalized,
        models.FinancialStatement.statement_type == statement_type,
    ).order_by(models.FinancialStatement.report_date.desc()).limit(limit).all()
    return [serialize(row) for row in rows]


@app.get("/api/market-context/snapshots")
def get_market_snapshots(
    category: str | None = None,
    limit: int = 100,
    exclude_mock: bool = True,
    db: Session = Depends(get_db),
):
    query = db.query(models.MarketSnapshot)
    if category:
        query = query.filter(models.MarketSnapshot.category == category)
    if exclude_mock:
        query = query.filter(models.MarketSnapshot.source != "mock")
    rows = query.order_by(models.MarketSnapshot.change_pct.desc().nullslast()).limit(limit).all()
    return [serialize(row) for row in rows]


@app.post("/api/market/package/refresh/{ticker}")
def refresh_market_package(
    ticker: str,
    payload: MarketPackageRefreshRequest | None = None,
    db: Session = Depends(get_db),
):
    try:
        def run():
            trade_date = payload.trade_date if payload else None
            return market_package_service.refresh_market_package(db, ticker=ticker, trade_date=trade_date)

        return _run_logged(db, "market_package_refresh", ticker, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"市场包刷新失败: {exc}") from exc


@app.get("/api/market/package/{ticker}")
def get_market_package(
    ticker: str,
    trade_date: str | None = None,
    overview_limit: int = 10,
    news_limit: int = 8,
    db: Session = Depends(get_db),
):
    return market_package_service.build_package(
        db,
        ticker=ticker,
        trade_date=trade_date,
        overview_limit=overview_limit,
        news_limit=news_limit,
    )


@app.get("/api/market/package")
def get_market_overview_package(
    overview_limit: int = 10,
    news_limit: int = 8,
    db: Session = Depends(get_db),
):
    return market_package_service.build_market_overview_package(
        db,
        overview_limit=overview_limit,
        news_limit=news_limit,
    )


@app.get("/api/data-package/{ticker}")
def get_data_package(
    ticker: str,
    daily_limit: int = settings.stock_daily_limit,
    news_limit: int = settings.stock_news_limit,
    db: Session = Depends(get_db),
):
    market_service.ensure_instrument(db, ticker)
    normalized = normalize_a_share_ticker(ticker)
    package = stock_package_service.build_package(db, normalized, daily_limit=daily_limit, news_limit=news_limit)
    if _snapshot_is_missing_or_stale(package.get("snapshot")):
        try:
            market_service.refresh_snapshot(db, normalized)
        except Exception:
            db.rollback()
        package = stock_package_service.build_package(db, normalized, daily_limit=daily_limit, news_limit=news_limit)
    if _package_needs_structured_bootstrap(package):
        try:
            market_service.refresh_daily_prices(db, normalized)
        except Exception:
            db.rollback()
        try:
            fundamental_service.refresh_company_profile(db, normalized)
            if getattr(fundamental_service, "tushare_sync", None) and fundamental_service.tushare_sync.enabled:
                fundamental_service.tushare_sync.refresh_fundamentals(db, normalized)
            else:
                fundamental_service.refresh_fundamental_metrics(db, normalized)
        except Exception:
            db.rollback()
        package = stock_package_service.build_package(db, normalized, daily_limit=daily_limit, news_limit=news_limit)
    return package


def _package_needs_structured_bootstrap(package: dict[str, Any]) -> bool:
    if package.get("profile") is None:
        return True
    if not package.get("daily"):
        return True
    if not package.get("metrics"):
        return True
    if not package.get("valuation_daily"):
        return True
    if not package.get("moneyflow_daily"):
        return True
    if not package.get("limit_prices"):
        return True
    return False


def _snapshot_is_missing_or_stale(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return True
    updated_at = snapshot.get("updated_at")
    if not updated_at:
        return True
    try:
        return datetime.fromisoformat(str(updated_at)).date() < datetime.utcnow().date()
    except Exception:
        return True


@app.get("/api/provider/usage")
def list_provider_usage(provider: str | None = None, api_name: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.ProviderUsageLog)
    if provider:
        query = query.filter(models.ProviderUsageLog.provider == provider)
    if api_name:
        query = query.filter(models.ProviderUsageLog.api_name == api_name)
    rows = query.order_by(models.ProviderUsageLog.created_at.desc()).limit(min(max(limit, 1), 500)).all()
    return [serialize(row) for row in rows]


@app.get("/api/data-availability/{scope}/{key}")
def get_data_availability(scope: str, key: str, db: Session = Depends(get_db)):
    rows = (
        db.query(models.DataAvailability)
        .filter(models.DataAvailability.scope == scope, models.DataAvailability.key == key)
        .order_by(models.DataAvailability.dataset.asc())
        .all()
    )
    return [serialize(row) for row in rows]


@app.get("/api/refresh-logs")
def list_refresh_logs(ticker: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    return [serialize(row) for row in refresh_log_service.list_logs(db, ticker, limit)]


@app.post("/api/positions")
def upsert_position(payload: PositionUpsert, db: Session = Depends(get_db)):
    position = portfolio_service.upsert_position(db, payload)
    if isinstance(position, dict):
        return position
    return serialize(position)


@app.get("/api/positions")
def list_positions(db: Session = Depends(get_db)):
    return [serialize(item) for item in portfolio_service.list_positions(db)]


@app.patch("/api/positions/{ticker}")
def patch_position(ticker: str, payload: PositionPatch, db: Session = Depends(get_db)):
    position = portfolio_service.patch_position(db, ticker, payload)
    if not position:
        raise HTTPException(status_code=404, detail="position not found")
    if isinstance(position, dict):
        return position
    return serialize(position)


@app.delete("/api/positions/{ticker}")
def delete_position(ticker: str, db: Session = Depends(get_db)):
    ok = portfolio_service.delete_position(db, ticker)
    if not ok:
        raise HTTPException(status_code=404, detail="position not found")
    return {"status": "deleted", "ticker": ticker}


@app.get("/api/portfolio/summary")
def portfolio_summary(db: Session = Depends(get_db)):
    return portfolio_service.portfolio_summary(db)


@app.post("/api/daily-plans")
def upsert_daily_plan(payload: DailyPlanUpsert, db: Session = Depends(get_db)):
    plan = plan_service.upsert_daily_plan(db, payload)
    return serialize(plan)


@app.get("/api/daily-plans")
def list_daily_plans(ticker: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    return [serialize(item) for item in plan_service.list_daily_plans(db, ticker, limit)]


@app.get("/api/daily-plans/latest/{ticker}")
def latest_daily_plan(ticker: str, db: Session = Depends(get_db)):
    plan = plan_service.latest_daily_plan(db, ticker)
    if not plan:
        raise HTTPException(status_code=404, detail="daily plan not found")
    return serialize(plan)


@app.post("/api/triggers/rules")
def create_trigger_rule(payload: TriggerRuleCreate, db: Session = Depends(get_db)):
    rule = trigger_service.create_rule(db, payload)
    return serialize(rule)


@app.get("/api/triggers/events")
def list_trigger_events(limit: int = 100, db: Session = Depends(get_db)):
    events = trigger_service.list_events(db, limit)
    return [serialize(event) for event in events]


@app.patch("/api/triggers/events/{event_id}")
def update_trigger_event_status(event_id: int, payload: TriggerEventStatusUpdate, db: Session = Depends(get_db)):
    event = trigger_service.update_event_status(db, event_id, payload.status)
    if not event:
        raise HTTPException(status_code=404, detail="trigger event not found")
    return serialize(event)

@app.post("/api/triggers/evaluate/{ticker}")
def evaluate_triggers(ticker: str, db: Session = Depends(get_db)):
    events = trigger_service.evaluate_ticker(db, ticker)
    return [serialize(event) for event in events]


@app.get("/api/data-quality")
def data_quality(db: Session = Depends(get_db)):
    items = watchlist_service.list_items(db)
    rows = []
    for item in items:
        ticker = normalize_a_share_ticker(item.ticker)
        package = stock_package_service.build_package(db, ticker, daily_limit=240, news_limit=5)
        snapshot = package["snapshot"]
        profile = package["profile"]
        news_count = len(package["news"])
        metric_count = len(package["metrics"])
        statement_count = len(package["statements"])
        daily_qfq = package["daily_meta_qfq"] or {"row_count": 0, "latest_trade_date": None}
        daily_raw = package["daily_meta_raw"] or {"row_count": 0, "latest_trade_date": None}
        indicator_qfq_count = len(package["indicators_qfq"])
        indicator_raw_count = len(package["indicators_raw"])
        last_daily = package["daily_meta"]["latest_trade_date"]
        last_indicator = daily_qfq["latest_trade_date"]
        latest_news_item = next(
            (
                row for row in package["news"]
                if isinstance(row, dict) and (row.get("published_at") or row.get("fetched_at"))
            ),
            None,
        )
        last_metric = db.query(models.FundamentalMetric).filter(
            models.FundamentalMetric.ticker == ticker
        ).order_by(models.FundamentalMetric.report_date.desc()).first()
        last_statement = db.query(models.FinancialStatement).filter(
            models.FinancialStatement.ticker == ticker
        ).order_by(models.FinancialStatement.report_date.desc()).first()
        latest_error = refresh_log_service.latest_error(db, ticker)
        daily_quality = package["quality"]
        missing = []
        if snapshot is None:
            missing.append("snapshot")
        if daily_qfq["row_count"] == 0:
            missing.append("daily_qfq")
        if daily_raw["row_count"] == 0:
            missing.append("daily_raw")
        if indicator_qfq_count == 0:
            missing.append("indicators_qfq")
        if indicator_raw_count == 0:
            missing.append("indicators_raw")
        if news_count == 0:
            missing.append("news")
        if package["daily_plan"] is None:
            missing.append("daily_plan")
        if package["position"] is None:
            missing.append("position")
        if profile is None:
            missing.append("profile")
        if metric_count == 0:
            missing.append("fundamentals")
        if statement_count == 0:
            missing.append("statements")
        ready_count = sum(
            [
                snapshot is not None,
                daily_qfq["row_count"] > 0,
                daily_raw["row_count"] > 0,
                indicator_qfq_count > 0,
                indicator_raw_count > 0,
                news_count > 0,
                package["daily_plan"] is not None,
                package["position"] is not None,
                profile is not None,
                metric_count > 0,
                statement_count > 0,
            ]
        )
        readiness_total = 11
        rows.append(
            {
                "ticker": ticker,
                "name": item.name,
                "snapshot": snapshot is not None,
                "daily_rows_qfq": daily_qfq["row_count"],
                "daily_rows_raw": daily_raw["row_count"],
                "indicator_rows_qfq": indicator_qfq_count,
                "indicator_rows_raw": indicator_raw_count,
                "news_rows": news_count,
                "daily_plan": package["daily_plan"] is not None,
                "position": package["position"] is not None,
                "profile": profile is not None,
                "fundamental_rows": metric_count,
                "statement_rows": statement_count,
                "readiness": round(ready_count / readiness_total * 100, 1),
                "missing": ", ".join(missing),
                "last_snapshot_at": snapshot.get("updated_at") if snapshot else None,
                "last_daily_date": last_daily,
                "last_indicator_date": last_indicator,
                "last_news_at": (
                    latest_news_item.get("published_at") or latest_news_item.get("fetched_at")
                    if latest_news_item
                    else None
                ),
                "last_metric_date": last_metric.report_date.isoformat() if last_metric else None,
                "last_statement_date": last_statement.report_date.isoformat() if last_statement else None,
                "latest_error": latest_error.message if latest_error else None,
                "latest_error_type": latest_error.job_type if latest_error else None,
                "latest_error_at": latest_error.created_at.isoformat() if latest_error else None,
                "daily_quality_status": daily_quality["status"],
                "daily_quality_warnings": daily_quality["warnings"],
                "daily_quality_blocking": daily_quality["blocking_issues"],
            }
        )
    return rows
