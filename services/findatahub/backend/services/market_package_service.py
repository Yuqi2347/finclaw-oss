from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..json_utils import sanitize_json_dict
from ..providers.akshare_provider import AkShareProvider
from ..providers.symbol import normalize_a_share_ticker
from .market_context_service import MarketContextService


def _serialize_model(obj):
    if obj is None:
        return None
    data: dict[str, Any] = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        data[col.name] = value.isoformat() if hasattr(value, "isoformat") else value
    return sanitize_json_dict(data)


class MarketPackageService:
    def __init__(self) -> None:
        self.akshare = AkShareProvider()
        self.market_context = MarketContextService()

    def refresh_market_package(
        self,
        db: Session,
        ticker: str,
        trade_date: str | None = None,
        hot_limit: int = 20,
        northbound_limit: int = 60,
        board_limit: int = 10,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        result: dict[str, Any] = {
            "ticker": normalized,
            "trade_date": trade_date,
            "generated_at": datetime.utcnow().isoformat(),
            "sections": {},
            "errors": [],
        }

        def save(data_type: str, payload: Any, scope_ticker: str | None = normalized) -> None:
            row = models.SpecialTradingData(
                ticker=scope_ticker,
                data_type=data_type,
                trade_date=datetime.fromisoformat(trade_date).date() if trade_date else None,
                raw=sanitize_json_dict(payload),
                source=getattr(self.akshare, "source", "akshare"),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            result["sections"][data_type] = _serialize_model(row)

        try:
            result["sections"]["market_context"] = self.market_context.refresh_all_best_effort(db)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"market_context: {exc}")

        try:
            save("stock_profile", self.akshare.get_stock_individual_info(normalized))
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_profile: {exc}")

        try:
            save("stock_concept_blocks", self.akshare.get_stock_board_membership(normalized, top_n=board_limit))
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_concept_blocks: {exc}")

        try:
            save("market_hot_stocks", self.akshare.get_stock_hot_ranks(limit=hot_limit), scope_ticker=None)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"market_hot_stocks: {exc}")

        try:
            save("market_northbound_flow", {"history": self.akshare.get_stock_northbound_history("北向资金", limit=northbound_limit)}, scope_ticker=None)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"market_northbound_flow: {exc}")

        try:
            history = self.akshare.get_stock_individual_fund_flow(normalized, limit=20)
            market_rank = None
            try:
                for row in self.akshare.get_stock_main_fund_flow("沪深A股"):
                    if str(row.get("代码") or "").zfill(6) == normalized.split(".", 1)[0]:
                        market_rank = row
                        break
            except Exception:
                pass
            save("stock_individual_fund_flow", {"history": history, "market_rank": market_rank}, scope_ticker=normalized)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_individual_fund_flow: {exc}")

        try:
            save("stock_dragon_tiger_board", {"rows": self.akshare.get_stock_dragon_tiger_board(normalized, look_back_days=30)}, scope_ticker=normalized)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_dragon_tiger_board: {exc}")

        try:
            save("stock_lockup_expiry", self.akshare.get_stock_lockup_expiry(normalized, forward_days=90, look_back_days=180), scope_ticker=normalized)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_lockup_expiry: {exc}")

        try:
            save("stock_industry_comparison", self.akshare.get_stock_industry_comparison(normalized, top_n=board_limit), scope_ticker=normalized)
        except Exception as exc:
            db.rollback()
            result["errors"].append(f"stock_industry_comparison: {exc}")

        result["status"] = "success" if not result["errors"] else "partial"
        return result

    def build_package(
        self,
        db: Session,
        ticker: str,
        trade_date: str | None = None,
        overview_limit: int = 10,
        news_limit: int = 8,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        market_overview = self.market_context.get_market_overview_cached(db, limit=overview_limit)
        package = {
            "ticker": normalized,
            "trade_date": trade_date,
            "generated_at": datetime.utcnow().isoformat(),
            "version": "market_package_v1",
            "overview": market_overview,
            "global_news": self._build_global_news(market_overview, news_limit=news_limit),
            "hot_stocks": self._latest_payload(db, "market_hot_stocks", ticker=None) or {"rank": [], "up": []},
            "northbound_flow": self._latest_payload(db, "market_northbound_flow", ticker=None) or {"history": []},
            "concept_blocks": self._latest_payload(db, "stock_concept_blocks", ticker=normalized) or {},
            "fund_flow": self._latest_payload(db, "stock_individual_fund_flow", ticker=normalized) or {"history": []},
            "dragon_tiger_board": self._latest_payload(db, "stock_dragon_tiger_board", ticker=normalized) or {"rows": []},
            "lockup_expiry": self._latest_payload(db, "stock_lockup_expiry", ticker=normalized) or {"queue": [], "detail": []},
            "industry_comparison": self._latest_payload(db, "stock_industry_comparison", ticker=normalized) or {"rows": []},
        }
        package["quality"] = self._build_quality(package)
        package["summary"] = self._build_summary(package)
        return sanitize_json_dict(package) or package

    def build_market_overview_package(
        self,
        db: Session,
        overview_limit: int = 10,
        news_limit: int = 8,
    ) -> dict[str, Any]:
        market_overview = self.market_context.get_market_overview_cached(db, limit=overview_limit)
        package = {
            "ticker": None,
            "trade_date": None,
            "generated_at": datetime.utcnow().isoformat(),
            "version": "market_package_v1",
            "overview": market_overview,
            "global_news": self._build_global_news(market_overview, news_limit=news_limit),
            "hot_stocks": self._latest_payload(db, "market_hot_stocks", ticker=None) or {"rank": [], "up": []},
            "northbound_flow": self._latest_payload(db, "market_northbound_flow", ticker=None) or {"history": []},
        }
        package["quality"] = self._build_quality(
            package,
            required=[
                "overview",
                "global_news",
                "hot_stocks",
                "northbound_flow",
            ],
        )
        package["summary"] = self._build_summary(package)
        return sanitize_json_dict(package) or package

    def _latest_payload(self, db: Session, data_type: str, ticker: str | None) -> dict[str, Any] | None:
        query = db.query(models.SpecialTradingData).filter(models.SpecialTradingData.data_type == data_type)
        if ticker is None:
            query = query.filter(models.SpecialTradingData.ticker.is_(None))
        else:
            query = query.filter(models.SpecialTradingData.ticker == normalize_a_share_ticker(ticker))
        row = query.order_by(models.SpecialTradingData.created_at.desc()).first()
        if not row:
            return None
        payload = row.raw if isinstance(row.raw, dict) else {}
        payload = sanitize_json_dict(payload) or {}
        payload["_meta"] = _serialize_model(row)
        return payload

    def _build_global_news(self, market_overview: dict[str, Any], news_limit: int = 8) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in (market_overview.get("events") or [])[:news_limit]:
            items.append(
                {
                    "title": event.get("title"),
                    "summary": event.get("summary"),
                    "category": event.get("category") or "market",
                    "source": event.get("source") or "market_context",
                    "event_time": event.get("event_time"),
                }
            )
        if not items:
            for index in (market_overview.get("indices") or [])[:2]:
                items.append(
                    {
                        "title": f"指数观察：{index.get('name') or index.get('symbol')}",
                        "summary": f"最新涨跌幅 {index.get('change_pct')}，成交额 {index.get('amount')}。",
                        "category": "index",
                        "source": "market_context",
                        "event_time": datetime.utcnow().isoformat(),
                    }
                )
        return items[:news_limit]

    def _build_quality(self, package: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        required_keys = required or [
            "overview",
            "global_news",
            "hot_stocks",
            "northbound_flow",
            "concept_blocks",
            "fund_flow",
            "dragon_tiger_board",
            "lockup_expiry",
            "industry_comparison",
        ]
        required = [(key, package.get(key)) for key in required_keys]
        missing = [name for name, value in required if not value]
        return {
            "status": "ready" if not missing else "partial",
            "missing": missing,
        }

    def _build_summary(self, package: dict[str, Any]) -> list[str]:
        summary: list[str] = []
        overview = package.get("overview") or {}
        breadth = overview.get("breadth") or {}
        fund_flow = overview.get("fund_flow") or {}
        if breadth:
            summary.append(
                f"市场广度：上涨 {breadth.get('up_count') or 0} / 下跌 {breadth.get('down_count') or 0}，涨停 {breadth.get('limit_up_count') or 0} 家。"
            )
        if fund_flow:
            summary.append(
                f"资金流：北向净流入 {fund_flow.get('northbound_net_amount')}, 主力净流入 {fund_flow.get('main_net_inflow')}。"
            )
        if package.get("concept_blocks"):
            profile = (package["concept_blocks"].get("profile") or {})
            industry = package["concept_blocks"].get("industry") or profile.get("行业")
            if industry:
                summary.append(f"个股行业：{industry}。")
        return summary[:5]
