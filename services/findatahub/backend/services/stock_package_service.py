from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from .. import models
from ..providers.symbol import normalize_a_share_ticker
from .news_provider import NewsProvider
from .stock_quality_service import StockQualityService


def serialize_row(obj):
    if obj is None:
        return None
    data = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        data[col.name] = value.isoformat() if hasattr(value, "isoformat") else value
    return data


class StockPackageService:
    def __init__(self) -> None:
        self.quality = StockQualityService()
        self.news_provider = NewsProvider()

    def build_package(self, db: Session, ticker: str, daily_limit: int = 240, news_limit: int = 30) -> dict:
        normalized = normalize_a_share_ticker(ticker)
        instrument = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
        snapshot = db.query(models.PriceRealtimeSnapshot).filter(models.PriceRealtimeSnapshot.ticker == normalized).first()
        profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
        metrics = (
            db.query(models.FundamentalMetric)
            .filter(models.FundamentalMetric.ticker == normalized)
            .order_by(models.FundamentalMetric.report_date.desc())
            .limit(8)
            .all()
        )
        statements = (
            db.query(models.FinancialStatement)
            .filter(models.FinancialStatement.ticker == normalized)
            .order_by(models.FinancialStatement.report_date.desc())
            .limit(24)
            .all()
        )
        valuation_daily = (
            db.query(models.ValuationDaily)
            .filter(models.ValuationDaily.ticker == normalized)
            .order_by(models.ValuationDaily.trade_date.desc())
            .limit(daily_limit)
            .all()
        )
        moneyflow_daily = (
            db.query(models.MoneyflowDaily)
            .filter(models.MoneyflowDaily.ticker == normalized)
            .order_by(models.MoneyflowDaily.trade_date.desc())
            .limit(daily_limit)
            .all()
        )
        limit_prices = (
            db.query(models.LimitPriceDaily)
            .filter(models.LimitPriceDaily.ticker == normalized)
            .order_by(models.LimitPriceDaily.trade_date.desc())
            .limit(daily_limit)
            .all()
        )
        availability = (
            db.query(models.DataAvailability)
            .filter(models.DataAvailability.scope == "stock", models.DataAvailability.key == normalized)
            .order_by(models.DataAvailability.dataset.asc())
            .all()
        )
        plan = (
            db.query(models.DailyPlan)
            .filter(models.DailyPlan.ticker == normalized)
            .order_by(models.DailyPlan.plan_date.desc())
            .first()
        )
        position = (
            db.query(models.Position)
            .filter(models.Position.ticker == normalized, models.Position.quantity > 0)
            .first()
        )
        trigger_events = (
            db.query(models.TriggerEvent)
            .filter(models.TriggerEvent.ticker == normalized)
            .order_by(models.TriggerEvent.created_at.desc())
            .limit(20)
            .all()
        )
        news_bundle = self.news_provider.search(db, ticker=normalized, limit=news_limit)

        daily_qfq = self._query_daily(db, normalized, "qfq", daily_limit)
        daily_raw = self._query_daily(db, normalized, "raw", daily_limit)
        indicators_qfq = self._query_indicators(db, normalized, "qfq", daily_limit)
        indicators_raw = self._query_indicators(db, normalized, "raw", daily_limit)
        quality = self.quality.summarize_package_quality(
            {"qfq": daily_qfq, "raw": daily_raw},
            {"qfq": indicators_qfq, "raw": indicators_raw},
            snapshot,
        )

        preferred_daily = daily_qfq or daily_raw
        preferred_indicators = indicators_qfq or indicators_raw
        daily_meta = self.quality.summarize_daily_series(preferred_daily, snapshot)

        return {
            "ticker": normalized,
            "instrument": serialize_row(instrument),
            "snapshot": serialize_row(snapshot),
            "daily": [serialize_row(row) for row in reversed(preferred_daily)],
            "indicators": [serialize_row(row) for row in reversed(preferred_indicators)],
            "daily_qfq": [serialize_row(row) for row in reversed(daily_qfq)],
            "daily_raw": [serialize_row(row) for row in reversed(daily_raw)],
            "indicators_qfq": [serialize_row(row) for row in reversed(indicators_qfq)],
            "indicators_raw": [serialize_row(row) for row in reversed(indicators_raw)],
            "news": news_bundle.get("items") or [],
            "news_meta": news_bundle.get("meta") or {},
            "profile": serialize_row(profile),
            "metrics": [serialize_row(row) for row in metrics],
            "statements": [serialize_row(row) for row in statements],
            "valuation_daily": [serialize_row(row) for row in reversed(valuation_daily)],
            "moneyflow_daily": [serialize_row(row) for row in reversed(moneyflow_daily)],
            "limit_prices": [serialize_row(row) for row in reversed(limit_prices)],
            "daily_plan": serialize_row(plan),
            "position": serialize_row(position),
            "trigger_events": [serialize_row(row) for row in trigger_events],
            "data_availability": [serialize_row(row) for row in availability],
            "data_freshness": self._build_freshness(
                snapshot=snapshot,
                daily=preferred_daily,
                profile=profile,
                metrics=metrics,
                valuation=valuation_daily,
                moneyflow=moneyflow_daily,
                limit_prices=limit_prices,
            ),
            "daily_meta": daily_meta,
            "daily_meta_qfq": self.quality.summarize_daily_series(daily_qfq, snapshot) if daily_qfq else None,
            "daily_meta_raw": self.quality.summarize_daily_series(daily_raw, snapshot) if daily_raw else None,
            "quality": quality,
        }

    def _query_daily(self, db: Session, ticker: str, adjustment: str, limit: int) -> list[models.PriceDaily]:
        return (
            db.query(models.PriceDaily)
            .filter(models.PriceDaily.ticker == ticker, models.PriceDaily.adjustment == adjustment)
            .order_by(models.PriceDaily.trade_date.desc())
            .limit(limit)
            .all()
        )

    def _query_indicators(self, db: Session, ticker: str, adjustment: str, limit: int) -> list[models.TechnicalIndicator]:
        return (
            db.query(models.TechnicalIndicator)
            .filter(models.TechnicalIndicator.ticker == ticker, models.TechnicalIndicator.adjustment == adjustment)
            .order_by(models.TechnicalIndicator.trade_date.desc())
            .limit(limit)
            .all()
        )

    def _build_freshness(
        self,
        *,
        snapshot,
        daily: list[models.PriceDaily],
        profile,
        metrics: list[models.FundamentalMetric],
        valuation: list[models.ValuationDaily],
        moneyflow: list[models.MoneyflowDaily],
        limit_prices: list[models.LimitPriceDaily],
    ) -> dict:
        latest_daily = max((row.trade_date for row in daily if row.trade_date), default=None)
        latest_metric = max((row.report_date for row in metrics if row.report_date), default=None)
        latest_valuation = max((row.trade_date for row in valuation if row.trade_date), default=None)
        latest_moneyflow = max((row.trade_date for row in moneyflow if row.trade_date), default=None)
        latest_limit_price = max((row.trade_date for row in limit_prices if row.trade_date), default=None)
        return {
            "snapshot_updated_at": snapshot.updated_at.isoformat() if snapshot else None,
            "snapshot_source": snapshot.source if snapshot else None,
            "daily_as_of": latest_daily.isoformat() if latest_daily else None,
            "daily_source": daily[0].source if daily else None,
            "profile_updated_at": profile.updated_at.isoformat() if profile else None,
            "profile_source": profile.source if profile else None,
            "fundamental_as_of": latest_metric.isoformat() if latest_metric else None,
            "valuation_as_of": latest_valuation.isoformat() if latest_valuation else None,
            "moneyflow_as_of": latest_moneyflow.isoformat() if latest_moneyflow else None,
            "limit_price_as_of": latest_limit_price.isoformat() if latest_limit_price else None,
        }
