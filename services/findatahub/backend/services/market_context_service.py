from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import time
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..json_utils import sanitize_json_dict
from ..providers.akshare_provider import AkShareProvider


class MarketContextService:
    fund_flow_stale_days = 10
    core_index_names = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '中证1000']

    def __init__(self) -> None:
        self.akshare = AkShareProvider()

    def refresh_index_snapshots(self, db: Session) -> int:
        return self._upsert_snapshots(db, self.akshare.get_index_spot())

    def refresh_core_index_snapshots(self, db: Session) -> int:
        """刷新核心指数快照（只包含8个核心指数）"""
        rows = [
            row
            for row in self.akshare.get_core_index_spot()
            if row.get("name") in self.core_index_names
        ]
        return self._upsert_snapshots(db, rows)

    def refresh_core_index_snapshots_fast(
        self,
        db: Session,
        deadline_seconds: float = 8.0,
        provider_timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        result = self._race_core_index_providers(
            deadline_seconds=deadline_seconds,
            provider_timeout_seconds=provider_timeout_seconds,
        )
        provider_results = result["provider_results"]
        rows = result.get("rows") or []
        if rows:
            count = self._upsert_snapshots(db, rows)
            latest = [self._serialize_sidebar_index(row) for row in self.latest_core_index_snapshots(db)]
            return {
                "status": "success",
                "stale": False,
                "index": count,
                "source": result.get("source"),
                "indices": latest,
                "provider_results": provider_results,
                "errors": result.get("errors", []),
            }

        db.rollback()
        cached_rows = self.latest_core_index_snapshots(db)
        if cached_rows:
            return {
                "status": "stale_but_available",
                "stale": True,
                "index": len(cached_rows),
                "source": "cache",
                "indices": [self._serialize_sidebar_index(row) for row in cached_rows],
                "provider_results": provider_results,
                "errors": result.get("errors", []),
                "message": "核心指数实时源未在时间预算内返回有效数据，已返回缓存指数",
            }
        return {
            "status": "failed",
            "stale": False,
            "index": 0,
            "source": None,
            "indices": [],
            "provider_results": provider_results,
            "errors": result.get("errors", []),
            "message": "核心指数实时源未在时间预算内返回有效数据，且本地无缓存指数",
        }

    def refresh_sector_snapshots(self, db: Session) -> int:
        return self._upsert_snapshots(db, self.akshare.get_sector_spot())

    def refresh_breadth_snapshot(self, db: Session) -> dict:
        data = self.akshare.get_market_breadth_snapshot()
        total_amount = data.get("total_amount")
        total_amount_billion = data.get("total_amount_billion")
        total_volume = data.get("total_volume")
        if total_amount is None and total_amount_billion is not None:
            total_amount = float(total_amount_billion) * 100000000
        if total_amount_billion is None and total_amount is not None:
            total_amount_billion = float(total_amount) / 100000000
        row = models.MarketBreadthSnapshot(
            market="A股",
            up_count=data.get("up_count"),
            down_count=data.get("down_count"),
            flat_count=data.get("flat_count"),
            limit_up_count=data.get("limit_up_count"),
            limit_down_count=data.get("limit_down_count"),
            strong_count=data.get("strong_count"),
            weak_count=data.get("weak_count"),
            median_change_pct=data.get("median_change_pct"),
            avg_change_pct=data.get("avg_change_pct"),
            total_amount=total_amount,
            total_amount_billion=total_amount_billion,
            total_volume=total_volume,
            source=data.get("source", "unknown"),
            raw=sanitize_json_dict(data.get("raw")),
            captured_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return self._serialize(row)

    def refresh_fund_flow_snapshot(self, db: Session) -> dict:
        data = self.akshare.get_market_fund_flow_snapshot()
        if not self._has_fund_flow_signal(data):
            raise RuntimeError("AKShare market fund flow returned no usable values")
        row = models.MarketFundFlowSnapshot(
            market="A股",
            northbound_net_amount=data.get("northbound_net_amount"),
            main_net_inflow=data.get("main_net_inflow"),
            super_large_net_inflow=data.get("super_large_net_inflow"),
            large_net_inflow=data.get("large_net_inflow"),
            medium_net_inflow=data.get("medium_net_inflow"),
            small_net_inflow=data.get("small_net_inflow"),
            source=data.get("source", "unknown"),
            raw=sanitize_json_dict(data.get("raw")),
            captured_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return self._serialize(row)

    def refresh_theme_snapshots(self, db: Session, limit: int = 50) -> int:
        rows = self.akshare.get_theme_spot(limit)
        count = 0
        for row in rows:
            existing = db.query(models.ThemeSnapshot).filter(
                models.ThemeSnapshot.theme_code == row["theme_code"],
                models.ThemeSnapshot.category == row["category"],
            ).first()
            if not existing:
                existing = models.ThemeSnapshot(
                    theme_code=row["theme_code"],
                    category=row["category"],
                )
                db.add(existing)
            existing.name = row.get("name")
            existing.change_pct = row.get("change_pct")
            existing.amount = row.get("amount")
            existing.leader = row.get("leader")
            existing.heat_score = row.get("heat_score")
            existing.source = row.get("source", "unknown")
            existing.raw = sanitize_json_dict(row.get("raw"))
            existing.updated_at = datetime.utcnow()
            count += 1
        db.commit()
        return count

    def refresh_market_events(self, db: Session) -> int:
        breadth = self.latest_breadth(db)
        fund_flow = self.latest_fund_flow(db)
        indices = self.latest_market_snapshots(db, category="index", limit=3)
        reference_dt = self._market_reference_datetime(breadth, fund_flow, indices)
        if reference_dt is not None:
            indices = [row for row in indices if self._is_same_market_day(row.updated_at, reference_dt)]
        sectors = self.latest_market_snapshots(db, category="sector", limit=5)
        if reference_dt is not None:
            sectors = [row for row in sectors if self._is_same_market_day(row.updated_at, reference_dt)]
        events = self._derive_events(breadth, fund_flow, indices, sectors)
        count = 0
        for event in events:
            row = models.MarketEvent(**event)
            db.add(row)
            count += 1
        db.commit()
        return count

    def refresh_all_best_effort(self, db: Session) -> dict:
        result = {"index": 0, "sector": 0, "breadth": None, "events": 0, "errors": []}
        self._best_effort(db, result, "index", self.refresh_core_index_snapshots)
        self._best_effort(db, result, "sector", self.refresh_sector_snapshots)
        self._best_effort(db, result, "breadth", self.refresh_breadth_snapshot)
        self._best_effort(db, result, "events", self.refresh_market_events)
        return result

    def latest_breadth(self, db: Session, exclude_mock: bool = True) -> models.MarketBreadthSnapshot | None:
        query = db.query(models.MarketBreadthSnapshot)
        if exclude_mock:
            query = query.filter(models.MarketBreadthSnapshot.source != "mock")
        row = query.order_by(models.MarketBreadthSnapshot.captured_at.desc()).first()
        if row and row.total_amount is None and row.total_amount_billion is None:
            derived_total_amount = self._derive_total_amount_from_indices(db)
            if derived_total_amount is not None:
                row.total_amount = derived_total_amount
                row.total_amount_billion = derived_total_amount / 100000000
        return row

    def latest_fund_flow(self, db: Session, exclude_mock: bool = True) -> models.MarketFundFlowSnapshot | None:
        query = db.query(models.MarketFundFlowSnapshot)
        if exclude_mock:
            query = query.filter(models.MarketFundFlowSnapshot.source != "mock")
        for row in query.order_by(models.MarketFundFlowSnapshot.captured_at.desc()).all():
            if self._is_recent_fund_flow(row) and self._has_fund_flow_signal(self._serialize(row)):
                return row
        return None

    def latest_market_snapshots(
        self,
        db: Session,
        category: str,
        limit: int = 10,
        exclude_mock: bool = True,
    ) -> list[models.MarketSnapshot]:
        query = db.query(models.MarketSnapshot).filter(models.MarketSnapshot.category == category)
        if exclude_mock:
            query = query.filter(models.MarketSnapshot.source != "mock")
        rows = query.order_by(models.MarketSnapshot.updated_at.desc()).all()
        latest_by_symbol: dict[str, models.MarketSnapshot] = {}
        for row in rows:
            existing = latest_by_symbol.get(row.symbol)
            if existing is None or (row.updated_at or datetime.min) > (existing.updated_at or datetime.min):
                latest_by_symbol[row.symbol] = row
        ordered_rows = sorted(
            latest_by_symbol.values(),
            key=lambda row: (
                row.change_pct if row.change_pct is not None else float("-inf"),
                row.updated_at or datetime.min,
            ),
            reverse=True,
        )
        return ordered_rows[:limit]

    def latest_core_index_snapshots(
        self,
        db: Session,
        exclude_mock: bool = True,
    ) -> list[models.MarketSnapshot]:
        query = db.query(models.MarketSnapshot).filter(
            models.MarketSnapshot.category == "index",
            models.MarketSnapshot.name.in_(self.core_index_names),
        )
        if exclude_mock:
            query = query.filter(models.MarketSnapshot.source != "mock")
        rows = query.order_by(models.MarketSnapshot.updated_at.desc()).all()
        latest_by_name: dict[str, models.MarketSnapshot] = {}
        for row in rows:
            name = str(row.name or "")
            existing = latest_by_name.get(name)
            if existing is None or (row.updated_at or datetime.min) > (existing.updated_at or datetime.min):
                latest_by_name[name] = row
        return [latest_by_name[name] for name in self.core_index_names if name in latest_by_name]

    def _race_core_index_providers(
        self,
        *,
        deadline_seconds: float,
        provider_timeout_seconds: float,
    ) -> dict[str, Any]:
        core_names = list(self.core_index_names)
        timeout = max(1.0, float(provider_timeout_seconds))
        providers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
            (
                "sina",
                lambda: [
                    row
                    for row in self.akshare.get_sina_index_spot(timeout=int(timeout))
                    if row.get("name") in core_names
                ],
            ),
        ]
        executor = ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="index-race")
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
                        rows = future.result()
                        valid_rows = [row for row in rows if self._is_valid_index_row(row)]
                        if len(valid_rows) < 3:
                            raise ValueError(f"only {len(valid_rows)} usable core index rows")
                        provider_results.append(
                            {
                                "provider": provider,
                                "status": "success",
                                "duration_ms": duration_ms,
                                "rows": len(valid_rows),
                            }
                        )
                        return {
                            "rows": valid_rows,
                            "source": provider,
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
            return {"rows": [], "source": None, "provider_results": provider_results, "errors": errors}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _is_valid_index_row(self, row: dict[str, Any]) -> bool:
        return (
            isinstance(row, dict)
            and bool(row.get("symbol"))
            and row.get("name") in self.core_index_names
            and row.get("price") is not None
        )

    def latest_theme_snapshots(self, db: Session, limit: int = 20, exclude_mock: bool = True) -> list[models.ThemeSnapshot]:
        query = db.query(models.ThemeSnapshot)
        if exclude_mock:
            query = query.filter(models.ThemeSnapshot.source != "mock")
        rows = query.order_by(models.ThemeSnapshot.updated_at.desc()).all()
        latest_by_key: dict[tuple[str, str], models.ThemeSnapshot] = {}
        for row in rows:
            key = (row.category, row.theme_code)
            existing = latest_by_key.get(key)
            if existing is None or (row.updated_at or datetime.min) > (existing.updated_at or datetime.min):
                latest_by_key[key] = row
        ordered_rows = sorted(
            latest_by_key.values(),
            key=lambda row: (
                row.heat_score if row.heat_score is not None else float("-inf"),
                row.updated_at or datetime.min,
            ),
            reverse=True,
        )
        return ordered_rows[:limit]

    def get_market_overview(self, db: Session, limit: int = 10) -> dict:
        """兼容入口：市场总览读取已切换为本地快照聚合。"""
        return self.get_market_overview_cached(db, limit=limit)

    def get_market_overview_sidebar_cached(
        self,
        db: Session,
        limit: int = 10,
        include_breadth: bool = True,
    ) -> dict:
        """左栏专用轻量快照：默认返回核心指数，可按需附带涨跌家数。"""
        index_rows = self.latest_core_index_snapshots(db)
        indices = [self._serialize_sidebar_index(row) for row in index_rows]

        breadth_row = self.latest_breadth(db) if include_breadth else None
        breadth = self._serialize_sidebar_breadth(breadth_row) if breadth_row else None

        generated_at = self._latest_overview_timestamp(
            breadth_row=breadth_row,
            fund_flow_row=None,
            index_rows=index_rows,
            sector_rows=[],
            event_rows=[],
        )

        payload = {
            "generated_at": generated_at,
            "indices": indices,
            "errors": [],
        }
        if include_breadth:
            payload["breadth"] = breadth
        return payload

    def get_market_overview_cached(self, db: Session, limit: int = 10) -> dict:
        """纯快照市场总览：只读本地数据库，不发起任何上游实时请求。"""
        index_rows = self.latest_core_index_snapshots(db)
        indices = [self._serialize(row) for row in index_rows]

        breadth_row = self.latest_breadth(db)
        breadth = self._serialize(breadth_row) if breadth_row else None
        if isinstance(breadth, dict) and breadth.get("total_amount") is None and breadth.get("total_amount_billion") is None:
            derived_total_amount = self._derive_total_amount_from_indices(db)
            if derived_total_amount is not None:
                breadth["total_amount"] = derived_total_amount
                breadth["total_amount_billion"] = derived_total_amount / 100000000

        fund_flow_row = self.latest_fund_flow(db)
        fund_flow = self._serialize(fund_flow_row) if fund_flow_row else None
        if not self._has_fund_flow_signal(fund_flow):
            fund_flow = None

        sector_rows = self.latest_market_snapshots(db, category="sector", limit=limit)
        sectors = [self._serialize(row) for row in sector_rows]
        sectors = sorted(
            [row for row in sectors if isinstance(row, dict) and row.get("change_pct") is not None],
            key=lambda row: row.get("change_pct", float("-inf")),
            reverse=True,
        )[:limit]

        theme_rows = self.latest_theme_snapshots(db, limit=limit)
        themes = [self._serialize(row) for row in theme_rows]
        themes = sorted(
            [row for row in themes if isinstance(row, dict) and row.get("change_pct") is not None],
            key=lambda row: row.get("change_pct", float("-inf")),
            reverse=True,
        )[:limit]

        event_rows = self.latest_events(db, limit=8)
        events = [self._serialize(row) for row in event_rows]

        generated_at = self._latest_overview_timestamp(
            breadth_row=breadth_row,
            fund_flow_row=fund_flow_row,
            index_rows=index_rows,
            sector_rows=sector_rows,
            event_rows=event_rows,
        )

        return {
            "generated_at": generated_at,
            "breadth": breadth,
            "fund_flow": fund_flow,
            "indices": indices,
            "sectors": sectors,
            "themes": themes,
            "events": events,
            "errors": [],
        }

    def _derive_total_amount_from_indices(self, db: Session) -> float | None:
        rows = self.latest_market_snapshots(db, category="index", limit=6)
        target_names = {"上证指数", "深证成指"}
        total = 0.0
        matched = False
        for row in rows:
            if str(row.name or "") not in target_names:
                continue
            if row.amount is None:
                continue
            total += float(row.amount)
            matched = True
        return total if matched and total > 0 else None

    def latest_events(self, db: Session, limit: int = 20, exclude_mock: bool = True) -> list[models.MarketEvent]:
        rows = db.query(models.MarketEvent).order_by(models.MarketEvent.event_time.desc()).limit(limit * 3).all()
        if not exclude_mock:
            return rows[:limit]
        filtered: list[models.MarketEvent] = []
        for row in rows:
            if row.source == "mock":
                continue
            raw = row.raw if isinstance(row.raw, dict) else {}
            raw_source = raw.get("source")
            raw_inner_source = (raw.get("raw") or {}).get("source") if isinstance(raw.get("raw"), dict) else None
            if row.source == "system" and (raw_source == "mock" or raw_inner_source == "mock"):
                continue
            filtered.append(row)
            if len(filtered) >= limit:
                break
        return filtered

    def _upsert_snapshots(self, db: Session, rows: list[dict]) -> int:
        count = 0
        for row in rows:
            existing = db.query(models.MarketSnapshot).filter(
                models.MarketSnapshot.symbol == row["symbol"],
                models.MarketSnapshot.category == row["category"],
            ).first()
            if not existing:
                existing = models.MarketSnapshot(symbol=row["symbol"], category=row["category"])
                db.add(existing)
            existing.name = row.get("name")
            existing.price = row.get("price")
            existing.change_pct = row.get("change_pct")
            existing.amount = row.get("amount")
            existing.raw = sanitize_json_dict(row.get("raw"))
            existing.source = row.get("source", "unknown")
            existing.updated_at = datetime.utcnow()
            count += 1
        db.commit()
        return count

    def _derive_events(
        self,
        breadth: models.MarketBreadthSnapshot | None,
        fund_flow: models.MarketFundFlowSnapshot | None,
        indices: list[models.MarketSnapshot],
        sectors: list[models.MarketSnapshot],
    ) -> list[dict]:
        events: list[dict] = []
        reference_dt = self._market_reference_datetime(breadth, fund_flow, indices)
        if breadth:
            total = (breadth.up_count or 0) + (breadth.down_count or 0) + (breadth.flat_count or 0)
            if total and (breadth.up_count or 0) / max(total, 1) < 0.35:
                events.append(
                    {
                        "event_type": "breadth_alert",
                        "title": "市场广度偏弱",
                        "summary": f"上涨家数 {breadth.up_count or 0}，下跌家数 {breadth.down_count or 0}，市场广度偏弱。",
                        "category": "market",
                        "source": "system",
                        "raw": self._serialize(breadth),
                    }
                )
            if (breadth.limit_up_count or 0) > 0:
                events.append(
                    {
                        "event_type": "limit_up_cluster",
                        "title": "涨停活跃",
                        "summary": f"当前涨停家数 {breadth.limit_up_count or 0}，存在短线情绪活跃迹象。",
                        "category": "market",
                        "source": "system",
                        "raw": self._serialize(breadth),
                    }
                )
        if fund_flow and fund_flow.northbound_net_amount is not None:
            if fund_flow.northbound_net_amount < 0:
                events.append(
                    {
                        "event_type": "northbound_outflow",
                        "title": "北向资金净流出",
                        "summary": f"北向资金净流入为 {fund_flow.northbound_net_amount:.2f}，需要关注外资流向变化。",
                        "category": "market",
                        "source": "system",
                        "raw": self._serialize(fund_flow),
                    }
                )
        if indices:
            top_index = indices[0]
            events.append(
                {
                    "event_type": "index_leader",
                    "title": f"指数观察：{top_index.name or top_index.symbol}",
                    "summary": f"最新涨跌幅 {top_index.change_pct or 0:.2f}%，成交额 {top_index.amount or 0:.2f}。",
                    "category": "index",
                    "symbol": top_index.symbol,
                    "source": top_index.source,
                    "raw": self._serialize(top_index),
                }
            )
        if sectors:
            top_sector = sectors[0]
            if reference_dt is not None and not self._is_same_market_day(top_sector.updated_at, reference_dt):
                return events[:8]
            events.append(
                {
                    "event_type": "sector_leader",
                    "title": f"板块领涨：{top_sector.name or top_sector.symbol}",
                    "summary": f"板块涨跌幅 {top_sector.change_pct or 0:.2f}%，成交额 {top_sector.amount or 0:.2f}。",
                    "category": "sector",
                    "symbol": top_sector.symbol,
                    "source": top_sector.source,
                    "raw": self._serialize(top_sector),
                }
            )
        return events[:8]

    def _best_effort(self, db: Session, result: dict, key: str, fn) -> None:
        try:
            result[key] = fn(db)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"{key}: {exc}")

    def _is_recent_fund_flow(self, row: models.MarketFundFlowSnapshot) -> bool:
        raw = row.raw if isinstance(row.raw, dict) else {}
        raw_date = raw.get("日期") or raw.get("date") or raw.get("交易日期") or raw.get("Date")
        parsed = None
        if raw_date is not None:
            try:
                parsed_value = pd.to_datetime(raw_date, errors="coerce")
                if pd.notna(parsed_value):
                    parsed = pd.Timestamp(parsed_value).to_pydatetime()
            except Exception:
                parsed = None
        if parsed is None:
            return False
        return (datetime.utcnow().date() - parsed.date()).days <= self.fund_flow_stale_days

    def _has_fund_flow_signal(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        return any(
            payload.get(key) is not None
            for key in (
                "northbound_net_amount",
                "main_net_inflow",
                "super_large_net_inflow",
                "large_net_inflow",
                "medium_net_inflow",
                "small_net_inflow",
            )
        )

    def _serialize(self, obj):
        if obj is None:
            return None
        data = {}
        for col in obj.__table__.columns:
            value = getattr(obj, col.name)
            data[col.name] = value.isoformat() if hasattr(value, "isoformat") else value
        return sanitize_json_dict(data)

    def _serialize_sidebar_breadth(self, obj):
        data = self._serialize(obj)
        if not isinstance(data, dict):
            return data
        for key in ("id", "market", "source", "raw", "total_amount", "total_amount_billion", "total_volume"):
            data.pop(key, None)
        return data

    def _serialize_sidebar_index(self, obj):
        if obj is None:
            return None
        updated_at = obj.updated_at.isoformat() if hasattr(obj.updated_at, "isoformat") else obj.updated_at
        return sanitize_json_dict(
            {
                "symbol": obj.symbol,
                "name": obj.name,
                "category": obj.category,
                "price": obj.price,
                "change_pct": obj.change_pct,
                "updated_at": updated_at,
            }
        )

    def _latest_overview_timestamp(
        self,
        breadth_row: models.MarketBreadthSnapshot | None,
        fund_flow_row: models.MarketFundFlowSnapshot | None,
        index_rows: list[models.MarketSnapshot],
        sector_rows: list[models.MarketSnapshot],
        event_rows: list[models.MarketEvent],
    ) -> str | None:
        candidates: list[datetime] = []
        for row in (breadth_row, fund_flow_row):
            if row is None:
                continue
            for attr in ("captured_at", "updated_at"):
                value = getattr(row, attr, None)
                if isinstance(value, datetime):
                    candidates.append(value)
                    break
        for row in [*index_rows, *sector_rows]:
            value = getattr(row, "updated_at", None)
            if isinstance(value, datetime):
                candidates.append(value)
        for row in event_rows:
            value = getattr(row, "event_time", None)
            if isinstance(value, datetime):
                candidates.append(value)
        return max(candidates).isoformat() if candidates else None

    def _deserialize_market_snapshot(self, data: dict | None) -> models.MarketSnapshot | None:
        if not data:
            return None
        return models.MarketSnapshot(
            symbol=str(data.get("symbol") or ""),
            category=str(data.get("category") or ""),
            name=data.get("name"),
            price=data.get("price"),
            change_pct=data.get("change_pct"),
            amount=data.get("amount"),
            raw=data.get("raw"),
            source=data.get("source", "unknown"),
            updated_at=pd.to_datetime(data.get("updated_at"), errors="coerce").to_pydatetime()
            if data.get("updated_at")
            else None,
        )

    def _market_reference_datetime(
        self,
        breadth: models.MarketBreadthSnapshot | None,
        fund_flow: models.MarketFundFlowSnapshot | None,
        indices: list[models.MarketSnapshot],
    ) -> datetime | None:
        candidates: list[datetime] = []
        for obj in (breadth, fund_flow):
            if obj is None:
                continue
            for attr in ("captured_at", "updated_at"):
                value = getattr(obj, attr, None)
                if value is not None:
                    candidates.append(value)
                    break
        for row in indices:
            value = getattr(row, "updated_at", None)
            if value is not None:
                candidates.append(value)
        return max(candidates) if candidates else None

    def _is_same_market_day(self, value: object, reference_dt: datetime | None) -> bool:
        if reference_dt is None or value is None:
            return False
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return False
            return pd.Timestamp(parsed).date() == reference_dt.date()
        except Exception:
            return False

    def refresh_international_indices(self, db: Session) -> int:
        """刷新国际指数快照（纳斯达克、日经225、韩国KOSPI）"""
        indices = self.akshare.get_international_indices()
        return self._upsert_snapshots(db, indices)

    def get_international_indices_cached(self) -> list[dict]:
        """获取国际指数（不刷新，仅从 provider 获取）"""
        return self.akshare.get_international_indices()
