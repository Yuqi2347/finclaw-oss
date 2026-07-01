from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import os
import re
import time
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models
from ..providers.akshare_provider import AkShareProvider
from ..providers.baostock_provider import BaoStockProvider
from ..providers.mootdx_provider import MootdxProvider
from ..providers.symbol import infer_exchange, normalize_a_share_ticker
from ..providers.tencent_provider import TencentProvider
from .indicators import calculate_indicators
from .tushare_sync_service import TushareSyncService


STATIC_INSTRUMENTS: tuple[dict[str, object], ...] = (
    {
        "ticker": "512480.SH",
        "name": "国联安中证全指半导体ETF",
        "aliases": ("半导体ETF国联", "国联安半导体ETF", "中证全指半导体ETF"),
        "asset_type": "ETF",
    },
    {
        "ticker": "510300.SH",
        "name": "沪深300ETF",
        "aliases": ("300ETF", "华泰柏瑞沪深300ETF"),
        "asset_type": "ETF",
    },
    {
        "ticker": "159915.SZ",
        "name": "创业板ETF",
        "aliases": ("易方达创业板ETF",),
        "asset_type": "ETF",
    },
    {
        "ticker": "588000.SH",
        "name": "科创50ETF",
        "aliases": ("华夏科创50ETF",),
        "asset_type": "ETF",
    },
    {
        "ticker": "512760.SH",
        "name": "芯片ETF",
        "aliases": ("国泰CES半导体芯片ETF", "半导体芯片ETF"),
        "asset_type": "ETF",
    },
)


class MarketService:
    INSTRUMENT_BOOTSTRAP_THRESHOLD = 1000

    def __init__(self):
        self.akshare = AkShareProvider()
        self.baostock = BaoStockProvider()
        self.mootdx = MootdxProvider()
        self.tencent = TencentProvider()
        self.tushare_sync = TushareSyncService()
        self.baostock_enabled = os.getenv("DATAHUB_ENABLE_BAOSTOCK", "").strip().lower() in {"1", "true", "yes", "on"}

    def ensure_instrument(self, db: Session, ticker: str, name: str | None = None) -> models.Instrument:
        normalized = normalize_a_share_ticker(ticker)
        instrument = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
        if not instrument:
            raw_ticker = str(ticker or "").strip().upper()
            if raw_ticker and raw_ticker != normalized:
                instrument = db.query(models.Instrument).filter(models.Instrument.ticker == raw_ticker).first()
                if instrument:
                    existing_normalized = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
                    if existing_normalized:
                        if name and not existing_normalized.name:
                            existing_normalized.name = name
                        existing_normalized.updated_at = datetime.utcnow()
                        db.commit()
                        db.refresh(existing_normalized)
                        return existing_normalized
                    instrument.ticker = normalized
        if instrument:
            if name and not instrument.name:
                instrument.name = name
            instrument.exchange = instrument.exchange or infer_exchange(normalized)
            instrument.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(instrument)
            return instrument
        instrument = models.Instrument(
            ticker=normalized,
            name=name,
            exchange=infer_exchange(normalized),
        )
        db.add(instrument)
        db.commit()
        db.refresh(instrument)
        return instrument

    def search_instruments(self, db: Session, query: str, limit: int = 10) -> list[dict]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return []
        capped_limit = max(1, min(limit, 50))
        variants = self._instrument_query_variants(raw_query)
        local_rows = self._search_local_instruments(db, raw_query, capped_limit)

        results: list[dict] = []
        seen: set[str] = set()
        for row in local_rows:
            item = self._instrument_to_result(self.ensure_instrument(db, row.ticker, row.name), source="datahub")
            ticker = str(item["ticker"])
            if ticker in seen:
                continue
            results.append(item)
            seen.add(ticker)

        if len(results) < capped_limit:
            for item in self._search_user_instruments(db, raw_query, capped_limit - len(results)):
                ticker = str(item["ticker"])
                if ticker in seen:
                    continue
                results.append(item)
                seen.add(ticker)
                self.ensure_instrument(db, ticker, item.get("name"))
                if len(results) >= capped_limit:
                    break

        if not results:
            for item in self._search_static_instruments(raw_query, capped_limit):
                ticker = str(item["ticker"])
                if ticker in seen:
                    continue
                results.append(item)
                seen.add(ticker)
                self.ensure_instrument(db, ticker, item.get("name"))
                if len(results) >= capped_limit:
                    break

        if len(results) < capped_limit and variants["ticker"]:
            try:
                item = self._resolve_remote_instrument_by_ticker(str(variants["ticker"]))
                ticker = str(item.get("ticker") or "").strip().upper()
                if ticker and ticker not in seen:
                    results.append(item)
                    seen.add(ticker)
                    self.ensure_instrument(db, ticker, item.get("name"))
            except Exception:
                db.rollback()

        remote_rows: list[dict] = []
        if not results and not variants["ticker"]:
            try:
                remote_rows = self.tencent.search_instruments(raw_query, capped_limit)
            except Exception:
                remote_rows = []
        for item in remote_rows:
            ticker = normalize_a_share_ticker(str(item.get("ticker") or item.get("code") or ""))
            if ticker and ticker not in seen:
                item["ticker"] = ticker
                item["code"] = ticker.split(".", 1)[0]
                item["exchange"] = item.get("exchange") or infer_exchange(ticker)
                results.append(item)
                seen.add(ticker)
                self.ensure_instrument(db, ticker, item.get("name"))
            if len(results) >= capped_limit:
                break
        return results

    def refresh_instrument_index(self, db: Session, limit: int | None = None) -> dict[str, int]:
        if self.tushare_sync.enabled:
            try:
                return self.tushare_sync.refresh_instrument_index(db, limit=limit)
            except Exception:
                db.rollback()
        rows = self.akshare.list_a_share_instruments()
        if limit is not None:
            rows = rows[: max(0, limit)]

        inserted = 0
        updated = 0
        for item in rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            instrument = db.query(models.Instrument).filter(models.Instrument.ticker == ticker).first()
            if instrument is None:
                instrument = models.Instrument(ticker=ticker)
                db.add(instrument)
                inserted += 1
            else:
                updated += 1

            instrument.name = item.get("name") or instrument.name
            instrument.market = item.get("market") or instrument.market or "A股"
            instrument.exchange = infer_exchange(ticker)
            instrument.updated_at = datetime.utcnow()

        db.commit()
        return {
            "fetched": len(rows),
            "inserted": inserted,
            "updated": updated,
            "total": db.query(func.count(models.Instrument.id)).scalar() or 0,
        }

    def refresh_snapshot(self, db: Session, ticker: str) -> models.PriceRealtimeSnapshot:
        try:
            data = self.tencent.get_realtime_snapshot(ticker)
        except Exception:
            data = self.akshare.get_realtime_snapshot(ticker)
        normalized = normalize_a_share_ticker(data["ticker"])
        trusted_name = self._trusted_stored_name(db, normalized)
        if trusted_name:
            data["name"] = trusted_name
        self.ensure_instrument(db, normalized, data.get("name"))
        snapshot = db.query(models.PriceRealtimeSnapshot).filter(
            models.PriceRealtimeSnapshot.ticker == normalized
        ).first()
        if not snapshot:
            snapshot = models.PriceRealtimeSnapshot(ticker=normalized)
            db.add(snapshot)

        for key, value in data.items():
            if hasattr(snapshot, key):
                setattr(snapshot, key, value)
        snapshot.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(snapshot)
        self._upsert_tencent_valuation(db, normalized, data)
        return snapshot

    def refresh_snapshot_fast(
        self,
        db: Session,
        ticker: str,
        deadline_seconds: float = 8.0,
        provider_timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        realtime = self._race_realtime_snapshot_providers(
            normalized,
            deadline_seconds=deadline_seconds,
            provider_timeout_seconds=provider_timeout_seconds,
        )
        provider_results = realtime["provider_results"]

        if realtime.get("data"):
            snapshot = self._upsert_realtime_snapshot(db, realtime["data"])
            return {
                "status": "success",
                "stale": False,
                "source": realtime.get("source"),
                "snapshot": snapshot,
                "provider_results": provider_results,
                "errors": realtime.get("errors", []),
            }

        db.rollback()
        cached = self.latest_snapshot(db, normalized)
        if cached is not None:
            return {
                "status": "stale_but_available",
                "stale": True,
                "source": "cache",
                "snapshot": cached,
                "provider_results": provider_results,
                "errors": realtime.get("errors", []),
                "message": "实时行情源未在时间预算内返回有效数据，已返回缓存快照",
            }
        return {
            "status": "failed",
            "stale": False,
            "source": None,
            "snapshot": None,
            "provider_results": provider_results,
            "errors": realtime.get("errors", []),
            "message": "实时行情源未在时间预算内返回有效数据，且本地无缓存快照",
        }

    def latest_snapshot(self, db: Session, ticker: str) -> models.PriceRealtimeSnapshot | None:
        normalized = normalize_a_share_ticker(ticker)
        return db.query(models.PriceRealtimeSnapshot).filter(
            models.PriceRealtimeSnapshot.ticker == normalized
        ).first()

    def _upsert_realtime_snapshot(self, db: Session, data: dict[str, Any]) -> models.PriceRealtimeSnapshot:
        normalized = normalize_a_share_ticker(data["ticker"])
        trusted_name = self._trusted_stored_name(db, normalized)
        if trusted_name:
            data["name"] = trusted_name
        self.ensure_instrument(db, normalized, data.get("name"))
        snapshot = db.query(models.PriceRealtimeSnapshot).filter(
            models.PriceRealtimeSnapshot.ticker == normalized
        ).first()
        if not snapshot:
            snapshot = models.PriceRealtimeSnapshot(ticker=normalized)
            db.add(snapshot)

        for key, value in data.items():
            if hasattr(snapshot, key):
                setattr(snapshot, key, value)
        snapshot.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(snapshot)
        self._upsert_tencent_valuation(db, normalized, data)
        return snapshot

    def _race_realtime_snapshot_providers(
        self,
        ticker: str,
        *,
        deadline_seconds: float,
        provider_timeout_seconds: float,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        timeout = max(1.0, float(provider_timeout_seconds))
        providers: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            ("tencent", lambda: self.tencent.get_realtime_snapshot(normalized, timeout=int(timeout))),
            ("sina", lambda: self.akshare.get_sina_realtime_snapshot(normalized, timeout=int(timeout))),
            ("eastmoney", lambda: self.akshare.get_eastmoney_realtime_snapshot(normalized, timeout=int(timeout))),
        ]
        executor = ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="quote-race")
        started_at = time.monotonic()
        future_meta = {}
        provider_results: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            for provider, fn in providers:
                future = executor.submit(fn)
                future_meta[future] = {"provider": provider, "started_at": time.monotonic()}

            pending = set(future_meta)
            while pending:
                remaining = deadline_seconds - (time.monotonic() - started_at)
                if remaining <= 0:
                    break
                done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for future in done:
                    meta = future_meta[future]
                    provider = meta["provider"]
                    duration_ms = int((time.monotonic() - meta["started_at"]) * 1000)
                    try:
                        data = future.result()
                        self._validate_realtime_snapshot(normalized, data)
                        data["source"] = data.get("source") or provider
                        provider_results.append(
                            {"provider": provider, "status": "success", "duration_ms": duration_ms}
                        )
                        return {
                            "data": data,
                            "source": data["source"],
                            "provider_results": provider_results,
                            "errors": errors,
                        }
                    except Exception as exc:
                        error = f"{provider}: {exc}"
                        errors.append(error)
                        provider_results.append(
                            {
                                "provider": provider,
                                "status": "error",
                                "duration_ms": duration_ms,
                                "error": str(exc),
                            }
                        )

            for future in pending:
                meta = future_meta[future]
                provider_results.append(
                    {
                        "provider": meta["provider"],
                        "status": "timeout",
                        "duration_ms": int((time.monotonic() - meta["started_at"]) * 1000),
                    }
                )
                errors.append(f"{meta['provider']}: timeout after {deadline_seconds:.1f}s")
            return {"data": None, "source": None, "provider_results": provider_results, "errors": errors}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _validate_realtime_snapshot(self, ticker: str, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("provider returned non-dict snapshot")
        normalized = normalize_a_share_ticker(str(data.get("ticker") or ""))
        if normalized != normalize_a_share_ticker(ticker):
            raise ValueError(f"ticker mismatch: expected {ticker}, got {data.get('ticker')}")
        if data.get("price") is None:
            raise ValueError("snapshot missing price")

    def _trusted_stored_name(self, db: Session, ticker: str) -> str | None:
        normalized = normalize_a_share_ticker(ticker)
        profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
        if profile and str(profile.name or "").strip():
            return str(profile.name).strip()
        instrument = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
        if instrument and str(instrument.name or "").strip():
            return str(instrument.name).strip()
        return None

    def _upsert_tencent_valuation(self, db: Session, ticker: str, data: dict) -> None:
        valuation = data.get("valuation")
        if not valuation:
            return
        today = datetime.utcnow().date()
        metric = db.query(models.FundamentalMetric).filter(
            models.FundamentalMetric.ticker == ticker,
            models.FundamentalMetric.report_date == today,
        ).first()
        if not metric:
            metric = models.FundamentalMetric(ticker=ticker, report_date=today, period=str(today))
            db.add(metric)
        raw = metric.raw or {}
        raw.update({"tencent_valuation": valuation})
        metric.raw = raw
        metric.source = "tencent"
        db.commit()

    def refresh_daily_prices(
        self,
        db: Session,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        normalized = normalize_a_share_ticker(ticker)
        if self.tushare_sync.enabled:
            try:
                start = None
                end = None
                if start_date:
                    start = datetime.strptime(str(start_date)[:10].replace("-", ""), "%Y%m%d").date()
                if end_date:
                    end = datetime.strptime(str(end_date)[:10].replace("-", ""), "%Y%m%d").date()
                if start is None or end is None:
                    return self.tushare_sync.refresh_stock_core(db, normalized, start_date=start_date, end_date=end_date)
                return self.tushare_sync.refresh_daily_bundle(db, normalized, start, end)
            except Exception as exc:
                db.rollback()
                return {"ticker": normalized, "daily_rows": {}, "errors": [f"tushare failed: {exc}"]}
        results: dict[str, int] = {}
        errors: list[str] = []
        for adjustment in ("raw", "qfq"):
            series = self._fetch_daily_series(normalized, start_date, end_date, adjustment, errors)
            if series.empty:
                results[adjustment] = 0
                continue
            results[adjustment] = self._upsert_daily_series(db, series)
            self.refresh_indicators(db, normalized, adjustment=adjustment)
        return {"ticker": normalized, "daily_rows": results, "errors": errors}

    def refresh_indicators(self, db: Session, ticker: str, adjustment: str = "qfq") -> int:
        normalized = normalize_a_share_ticker(ticker)
        rows = (
            db.query(models.PriceDaily)
            .filter(models.PriceDaily.ticker == normalized, models.PriceDaily.adjustment == adjustment)
            .order_by(models.PriceDaily.trade_date.asc())
            .all()
        )
        if not rows:
            return 0
        daily = [
            {
                "ticker": row.ticker,
                "trade_date": row.trade_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for row in rows
        ]
        indicators = calculate_indicators(__import__("pandas").DataFrame(daily))
        count = 0
        for _, row in indicators.iterrows():
            existing = db.query(models.TechnicalIndicator).filter(
                models.TechnicalIndicator.ticker == normalized,
                models.TechnicalIndicator.trade_date == row["trade_date"],
                models.TechnicalIndicator.adjustment == adjustment,
            ).first()
            if not existing:
                existing = models.TechnicalIndicator(ticker=normalized, trade_date=row["trade_date"], adjustment=adjustment)
                db.add(existing)
            for key in (
                "ma5",
                "ma10",
                "ma20",
                "ma60",
                "rsi14",
                "macd",
                "macd_signal",
                "macd_hist",
                "atr14",
                "boll_mid",
                "boll_ub",
                "boll_lb",
                "vwma20",
            ):
                setattr(existing, key, _to_float(row[key]))
            count += 1
        db.commit()
        return count

    def refresh_news(self, db: Session, ticker: str, limit: int = 20) -> int:
        items = self.akshare.get_stock_news(ticker, limit)
        count = 0
        for item in items:
            existing = None
            if item.get("url"):
                existing = db.query(models.NewsArticle).filter(models.NewsArticle.url == item["url"]).first()
            if not existing:
                existing = models.NewsArticle()
                db.add(existing)
            for key, value in item.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            count += 1
        db.commit()
        return count

    def dashboard_summary(self, db: Session) -> dict:
        last_snapshot = db.query(func.max(models.PriceRealtimeSnapshot.updated_at)).scalar()
        return {
            "watchlist_count": db.query(models.WatchlistItem).count(),
            "position_count": db.query(models.Position).filter(models.Position.quantity > 0).count(),
            "trigger_count": db.query(models.TriggerEvent).count(),
            "last_updated": last_snapshot,
        }

    def _fetch_daily_series(
        self,
        ticker: str,
        start_date: str | None,
        end_date: str | None,
        adjustment: str,
        errors: list[str],
    ):
        providers = [("akshare", self.akshare)]
        if adjustment == "raw":
            providers.insert(0, ("mootdx", self.mootdx))
        if self.baostock_enabled:
            providers.append(("baostock", self.baostock))
        for name, provider in providers:
            try:
                daily = provider.get_daily_prices(ticker, start_date, end_date, adjustment=adjustment)
                if daily is not None and not daily.empty:
                    return daily
            except NotImplementedError as exc:
                errors.append(f"{name}:{adjustment} unsupported: {exc}")
            except Exception as exc:
                errors.append(f"{name}:{adjustment} failed: {exc}")
        return __import__("pandas").DataFrame()

    def _upsert_daily_series(self, db: Session, daily) -> int:
        count = 0
        for _, row in daily.iterrows():
            normalized = normalize_a_share_ticker(str(row["ticker"]))
            adjustment = str(row.get("adjustment") or "qfq")
            existing = db.query(models.PriceDaily).filter(
                models.PriceDaily.ticker == normalized,
                models.PriceDaily.trade_date == row["trade_date"],
                models.PriceDaily.adjustment == adjustment,
            ).first()
            if not existing:
                existing = models.PriceDaily(ticker=normalized, trade_date=row["trade_date"], adjustment=adjustment)
                db.add(existing)
            existing.open = _to_float(row["open"])
            existing.high = _to_float(row["high"])
            existing.low = _to_float(row["low"])
            existing.close = _to_float(row["close"])
            existing.volume = _to_float(row["volume"])
            existing.amount = _to_float(row["amount"])
            existing.source = str(row["source"])
            count += 1
        db.commit()
        return count

    def _search_local_instruments(self, db: Session, query: str, limit: int) -> list[models.Instrument]:
        variants = self._instrument_query_variants(query)
        filters = [models.Instrument.name.ilike(f"%{variants['raw']}%")]
        if variants["upper"]:
            filters.append(models.Instrument.ticker.ilike(f"%{variants['upper']}%"))
            if len(variants["upper"]) == 8 and variants["upper"][:2] in {"SH", "SZ", "BJ"}:
                filters.append(models.Instrument.ticker == normalize_a_share_ticker(variants["upper"]))
        if variants["ticker"]:
            filters.append(models.Instrument.ticker == variants["ticker"])
        if variants["code"]:
            filters.append(models.Instrument.ticker.ilike(f"{variants['code']}.%"))
            filters.append(models.Instrument.ticker.ilike(f"%{variants['code']}%"))
        exact_rows = db.query(models.Instrument).filter(or_(*filters)).limit(limit).all()
        if exact_rows or variants["ticker"]:
            return self._dedupe_instrument_rows(exact_rows)[:limit]

        terms = self._meaningful_instrument_terms(query)
        if not terms:
            return []
        candidates = db.query(models.Instrument).filter(models.Instrument.name.isnot(None)).limit(10000).all()
        ranked: list[tuple[int, models.Instrument]] = []
        for row in candidates:
            haystack = self._compact_symbol_text(f"{row.name or ''}{row.ticker or ''}")
            if all(term in haystack for term in terms):
                ranked.append((len(terms), row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return self._dedupe_instrument_rows([row for _, row in ranked])[:limit]

    def _search_user_instruments(self, db: Session, query: str, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        variants = self._instrument_query_variants(query)
        filters = []
        for model in (models.Position, models.WatchlistItem):
            model_filters = [model.name.ilike(f"%{variants['raw']}%")]
            if variants["upper"]:
                model_filters.append(model.ticker.ilike(f"%{variants['upper']}%"))
                if len(variants["upper"]) == 8 and variants["upper"][:2] in {"SH", "SZ", "BJ"}:
                    model_filters.append(model.ticker == normalize_a_share_ticker(variants["upper"]))
            if variants["ticker"]:
                model_filters.append(model.ticker == variants["ticker"])
            if variants["code"]:
                model_filters.append(model.ticker.ilike(f"{variants['code']}.%"))
                model_filters.append(model.ticker.ilike(f"%{variants['code']}%"))
            filters.append((model, or_(*model_filters)))

        items: list[dict] = []
        seen: set[str] = set()
        for model, condition in filters:
            rows = db.query(model).filter(condition).limit(limit).all()
            for row in rows:
                ticker = normalize_a_share_ticker(row.ticker)
                if ticker in seen:
                    continue
                seen.add(ticker)
                items.append(
                    {
                        "ticker": ticker,
                        "code": ticker.split(".", 1)[0],
                        "name": row.name,
                        "exchange": infer_exchange(ticker),
                        "market": "A股",
                        "source": "portfolio_cache" if model is models.Position else "watchlist_cache",
                    }
                )
                if len(items) >= limit:
                    return items
        return items

    def _instrument_query_variants(self, query: str) -> dict[str, str | None]:
        raw = str(query or "").strip()
        upper = raw.upper()
        digits = "".join(re.findall(r"\d", upper))
        code = digits[-6:] if len(digits) >= 6 else (digits if digits else None)
        ticker = None
        if code and len(code) == 6:
            ticker = normalize_a_share_ticker(code)
        return {
            "raw": raw,
            "upper": upper,
            "code": code,
            "ticker": ticker,
        }

    def _instrument_to_result(self, row: models.Instrument, source: str) -> dict:
        ticker = normalize_a_share_ticker(row.ticker)
        return {
            "ticker": ticker,
            "code": ticker.split(".", 1)[0],
            "name": row.name,
            "exchange": row.exchange or infer_exchange(ticker),
            "market": row.market,
            "source": source,
        }

    def _search_static_instruments(self, query: str, limit: int) -> list[dict]:
        compact_query = self._compact_symbol_text(query)
        terms = self._meaningful_instrument_terms(query)
        matched: list[tuple[int, dict]] = []
        for item in STATIC_INSTRUMENTS:
            ticker = normalize_a_share_ticker(str(item["ticker"]))
            aliases = [str(item["name"]), *(str(alias) for alias in item.get("aliases", ()))]
            searchable = self._compact_symbol_text("".join([ticker, *aliases]))
            score = 0
            if compact_query and compact_query in searchable:
                score += 10
            if terms and all(term in searchable for term in terms):
                score += len(terms)
            if not score:
                continue
            matched.append(
                (
                    score,
                    {
                        "ticker": ticker,
                        "code": ticker.split(".", 1)[0],
                        "name": item["name"],
                        "exchange": infer_exchange(ticker),
                        "market": "A股",
                        "source": "static_alias",
                    },
                )
            )
        matched.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in matched[:limit]]

    def _dedupe_instrument_rows(self, rows: list[models.Instrument]) -> list[models.Instrument]:
        deduped: list[models.Instrument] = []
        seen: set[str] = set()
        for row in rows:
            ticker = normalize_a_share_ticker(row.ticker)
            if ticker in seen:
                continue
            seen.add(ticker)
            deduped.append(row)
        return deduped

    def _instrument_query_terms(self, query: str) -> list[str]:
        text = self._compact_symbol_text(query)
        terms = {text} if len(text) >= 2 else set()
        for part in re.findall(r"[A-Z]+|\d+|[\u4e00-\u9fff]{2,}", text):
            if len(part) >= 2 and part not in {"股份", "有限", "公司", "证券"}:
                terms.add(part)
        return sorted(terms, key=len, reverse=True)

    def _meaningful_instrument_terms(self, query: str) -> list[str]:
        generic = {"ETF", "LOF", "基金", "股份", "有限", "公司", "证券"}
        text = self._compact_symbol_text(query)
        domain_terms = {
            token
            for token in ("半导体", "芯片", "国联", "国联安", "创业板", "科创", "沪深300", "光迅", "华天")
            if token in text
        }
        terms: set[str] = set()
        for term in self._instrument_query_terms(query):
            if term in generic:
                continue
            if domain_terms and len(term) > 6 and re.search(r"[\u4e00-\u9fff]", term):
                continue
            terms.add(term)
        terms.update(domain_terms)
        return sorted(terms, key=len, reverse=True)

    def _compact_symbol_text(self, value: str) -> str:
        return re.sub(r"[\s\-_（）()【】\[\]·.]+", "", str(value or "").upper())

    def _resolve_remote_instrument_by_ticker(self, ticker: str) -> dict:
        normalized = normalize_a_share_ticker(ticker)
        code = normalized.split(".", 1)[0]
        name: str | None = None

        try:
            snapshot = self.tencent.get_realtime_snapshot(normalized, timeout=3)
            name = str(snapshot.get("name") or "").strip() or None
        except Exception:
            snapshot = None

        if not name:
            for item in self.tencent.search_instruments(code, 1):
                if normalize_a_share_ticker(str(item.get("ticker") or "")) == normalized:
                    name = str(item.get("name") or "").strip() or None
                    break

        return {
            "ticker": normalized,
            "code": code,
            "name": name,
            "exchange": infer_exchange(normalized),
            "market": "A股",
            "source": "provider" if name else "ticker_guess",
        }


def _to_float(value):
    try:
        if value is None:
            return None
        if __import__("pandas").isna(value):
            return None
        return float(value)
    except Exception:
        return None
