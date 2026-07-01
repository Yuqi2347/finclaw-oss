from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from .. import models
from ..json_utils import sanitize_json_dict
from ..providers.base import ProviderError, ProviderResult
from ..providers.provider_router import ProviderRouter
from ..providers.symbol import infer_exchange, normalize_a_share_ticker
from .data_availability_service import DataAvailabilityService, ProviderUsageService
from .indicators import calculate_indicators


class TushareSyncService:
    def __init__(self) -> None:
        self.router = ProviderRouter()
        self.availability = DataAvailabilityService()
        self.usage = ProviderUsageService()

    @property
    def enabled(self) -> bool:
        return self.router.use_tushare

    def refresh_instrument_index(self, db: Session, limit: int | None = None) -> dict[str, int]:
        result = self._call(
            db,
            "stock_basic",
            {"list_status": "L"},
            "ts_code,symbol,name,area,industry,market,list_date,exchange",
        )
        rows = result.rows[: max(0, limit)] if limit is not None else result.rows
        inserted = 0
        updated = 0
        for item in rows:
            ticker = normalize_a_share_ticker(str(item.get("ts_code") or item.get("symbol") or ""))
            if not ticker:
                continue
            row = db.query(models.Instrument).filter(models.Instrument.ticker == ticker).first()
            if not row:
                row = models.Instrument(ticker=ticker)
                db.add(row)
                inserted += 1
            else:
                updated += 1
            row.name = item.get("name") or row.name
            row.market = item.get("market") or "A股"
            row.exchange = infer_exchange(ticker)
            row.industry = item.get("industry") or row.industry
            row.sector = item.get("area") or row.sector
            row.updated_at = datetime.utcnow()
        self.availability.mark(
            db,
            scope="market",
            key="A股",
            dataset="instrument_index",
            provider="tushare",
            status="ready" if rows else "empty",
            row_count=len(rows),
            as_of=datetime.utcnow().date().isoformat(),
        )
        db.commit()
        return {
            "fetched": len(rows),
            "inserted": inserted,
            "updated": updated,
            "total": db.query(models.Instrument.id).count(),
        }

    def refresh_stock_core(
        self,
        db: Session,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        daily_limit_days: int = 420,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        end = _parse_yyyymmdd_or_iso(end_date) or datetime.utcnow().date()
        start = _parse_yyyymmdd_or_iso(start_date) or (end - timedelta(days=daily_limit_days))
        result = {
            "ticker": normalized,
            "provider": "tushare",
            "profile": 0,
            "daily_raw": 0,
            "daily_qfq": 0,
            "valuation": 0,
            "moneyflow": 0,
            "limit_price": 0,
            "errors": [],
        }
        result["profile"] = self.refresh_company_profile(db, normalized)
        daily_bundle = self.refresh_daily_bundle(db, normalized, start, end)
        result.update(daily_bundle)
        return result

    def refresh_company_profile(self, db: Session, ticker: str) -> int:
        normalized = normalize_a_share_ticker(ticker)
        count = 0
        errors: list[str] = []
        basic_rows: list[dict[str, Any]] = []
        company_rows: list[dict[str, Any]] = []
        try:
            basic_rows = self._call(
                db,
                "stock_basic",
                {"ts_code": normalized},
                "ts_code,symbol,name,area,industry,market,list_date,exchange",
            ).rows
        except Exception as exc:
            errors.append(f"stock_basic:{exc}")
        try:
            company_rows = self._call(
                db,
                "stock_company",
                {"ts_code": normalized, "exchange": _tushare_exchange(normalized)},
                "ts_code,chairman,manager,secretary,reg_capital,setup_date,province,city,website,employees,main_business,business_scope",
            ).rows
        except Exception as exc:
            errors.append(f"stock_company:{exc}")

        basic = basic_rows[0] if basic_rows else {}
        company = company_rows[0] if company_rows else {}
        if not basic and not company:
            self.availability.mark(
                db,
                scope="stock",
                key=normalized,
                dataset="profile",
                provider="tushare",
                status="failed",
                missing_reason="; ".join(errors) or "empty",
            )
            db.commit()
            return 0

        instrument = db.query(models.Instrument).filter(models.Instrument.ticker == normalized).first()
        if not instrument:
            instrument = models.Instrument(ticker=normalized)
            db.add(instrument)
        instrument.name = basic.get("name") or instrument.name
        instrument.industry = basic.get("industry") or instrument.industry
        instrument.sector = basic.get("area") or instrument.sector
        instrument.market = basic.get("market") or instrument.market or "A股"
        instrument.exchange = infer_exchange(normalized)
        instrument.updated_at = datetime.utcnow()

        profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
        if not profile:
            profile = models.CompanyProfile(ticker=normalized)
            db.add(profile)
        profile.name = basic.get("name") or profile.name
        profile.industry = basic.get("industry") or profile.industry
        profile.area = basic.get("area") or company.get("province") or profile.area
        profile.market = basic.get("market") or profile.market or "A股"
        profile.list_date = basic.get("list_date") or profile.list_date
        profile.raw = sanitize_json_dict({"stock_basic": basic, "stock_company": company, "errors": errors}) or {}
        profile.source = "tushare"
        profile.updated_at = datetime.utcnow()
        self.availability.mark(
            db,
            scope="stock",
            key=normalized,
            dataset="profile",
            provider="tushare",
            status="ready",
            row_count=int(bool(basic)) + int(bool(company)),
            as_of=datetime.utcnow().date().isoformat(),
            raw={"has_company": bool(company)},
        )
        db.commit()
        count += 1
        return count

    def refresh_daily_bundle(self, db: Session, ticker: str, start: date, end: date) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        start_text = _fmt_tushare_date(start)
        end_text = _fmt_tushare_date(end)
        result = {"daily_raw": 0, "daily_qfq": 0, "valuation": 0, "moneyflow": 0, "limit_price": 0, "errors": []}

        daily_rows = self._safe_rows(
            db,
            normalized,
            "daily",
            {"ts_code": normalized, "start_date": start_text, "end_date": end_text},
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            result["errors"],
        )
        adj_rows = self._safe_rows(
            db,
            normalized,
            "adj_factor",
            {"ts_code": normalized, "start_date": start_text, "end_date": end_text},
            "ts_code,trade_date,adj_factor",
            result["errors"],
        )
        result["daily_raw"], result["daily_qfq"] = self._upsert_daily_with_qfq(db, normalized, daily_rows, adj_rows)
        self._refresh_indicators(db, normalized, "raw")
        self._refresh_indicators(db, normalized, "qfq")
        self._mark_stock_dataset(db, normalized, "daily_raw", result["daily_raw"], daily_rows)
        self._mark_stock_dataset(db, normalized, "daily_qfq", result["daily_qfq"], daily_rows if adj_rows else [])

        valuation_rows = self._safe_rows(
            db,
            normalized,
            "daily_basic",
            {"ts_code": normalized, "start_date": start_text, "end_date": end_text},
            "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,total_mv,circ_mv",
            result["errors"],
        )
        result["valuation"] = self._upsert_valuation(db, normalized, valuation_rows)
        self._mark_stock_dataset(db, normalized, "valuation_daily", result["valuation"], valuation_rows)

        moneyflow_rows = self._safe_rows(
            db,
            normalized,
            "moneyflow",
            {"ts_code": normalized, "start_date": start_text, "end_date": end_text},
            "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
            result["errors"],
        )
        result["moneyflow"] = self._upsert_moneyflow(db, normalized, moneyflow_rows)
        self._mark_stock_dataset(db, normalized, "moneyflow_daily", result["moneyflow"], moneyflow_rows)

        limit_rows = self._safe_rows(
            db,
            normalized,
            "stk_limit",
            {"ts_code": normalized, "start_date": start_text, "end_date": end_text},
            "ts_code,trade_date,up_limit,down_limit",
            result["errors"],
        )
        result["limit_price"] = self._upsert_limit_price(db, normalized, limit_rows)
        self._mark_stock_dataset(db, normalized, "limit_price_daily", result["limit_price"], limit_rows)
        db.commit()
        return result

    def refresh_fundamentals(self, db: Session, ticker: str, limit: int = 12) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        result = {"ticker": normalized, "metrics": 0, "income": 0, "balance": 0, "cashflow": 0, "errors": []}
        result["metrics"] = self._refresh_fina_indicator(db, normalized, limit, result["errors"])
        result["income"] = self._refresh_statement(db, normalized, "income", "income", limit, result["errors"])
        result["balance"] = self._refresh_statement(db, normalized, "balance", "balancesheet", limit, result["errors"])
        result["cashflow"] = self._refresh_statement(db, normalized, "cashflow", "cashflow", limit, result["errors"])
        db.commit()
        return result

    def _refresh_fina_indicator(self, db: Session, ticker: str, limit: int, errors: list[str]) -> int:
        rows = self._safe_rows(
            db,
            ticker,
            "fina_indicator",
            {"ts_code": ticker},
            "ts_code,end_date,eps,dt_eps,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,assets_turn,ocfps,free_cashflow",
            errors,
        )
        rows = _dedupe_tushare_period_rows(rows)[:limit]
        count = 0
        for item in rows:
            report_date = _parse_tushare_date(item.get("end_date"))
            if not report_date:
                continue
            row = db.query(models.FundamentalMetric).filter(
                models.FundamentalMetric.ticker == ticker,
                models.FundamentalMetric.report_date == report_date,
            ).first()
            if not row:
                row = models.FundamentalMetric(ticker=ticker, report_date=report_date, period=str(report_date))
                db.add(row)
            row.eps = _to_float(item.get("eps"))
            row.roe = _to_float(item.get("roe"))
            row.gross_margin = _to_float(item.get("grossprofit_margin"))
            row.net_margin = _to_float(item.get("netprofit_margin"))
            row.debt_to_assets = _to_float(item.get("debt_to_assets"))
            row.raw = sanitize_json_dict(item)
            row.source = "tushare"
            count += 1
        self._mark_stock_dataset(db, ticker, "fina_indicator", count, rows)
        return count

    def _refresh_statement(
        self,
        db: Session,
        ticker: str,
        statement_type: str,
        api_name: str,
        limit: int,
        errors: list[str],
    ) -> int:
        rows = _dedupe_tushare_period_rows(
            self._safe_rows(db, ticker, api_name, {"ts_code": ticker}, _statement_fields(api_name), errors)
        )[:limit]
        count = 0
        for item in rows:
            report_date = _parse_tushare_date(item.get("end_date"))
            if not report_date:
                continue
            row = db.query(models.FinancialStatement).filter(
                models.FinancialStatement.ticker == ticker,
                models.FinancialStatement.statement_type == statement_type,
                models.FinancialStatement.report_date == report_date,
            ).first()
            if not row:
                row = models.FinancialStatement(ticker=ticker, statement_type=statement_type, report_date=report_date)
                db.add(row)
            row.period = str(report_date)
            row.raw = sanitize_json_dict(item) or {}
            row.source = "tushare"
            count += 1
        self._mark_stock_dataset(db, ticker, f"statement_{statement_type}", count, rows)
        return count

    def _call(self, db: Session, api_name: str, params: dict[str, Any], fields: str) -> ProviderResult:
        provider = self.router.require_tushare(api_name)
        try:
            result = provider.call(api_name, params=params, fields=fields)
            self.usage.log(
                db,
                provider=result.provider,
                api_name=api_name,
                status=result.status,
                row_count=len(result.rows),
                duration_ms=result.duration_ms,
                request={"params": params, "fields": fields},
            )
            db.flush()
            return result
        except ProviderError as exc:
            self.usage.log(
                db,
                provider=exc.provider,
                api_name=api_name,
                status="failed",
                error_code=exc.error_code,
                message=str(exc),
                request={"params": params, "fields": fields},
            )
            db.flush()
            raise

    def _safe_rows(
        self,
        db: Session,
        ticker: str,
        api_name: str,
        params: dict[str, Any],
        fields: str,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        try:
            return self._call(db, api_name, params, fields).rows
        except Exception as exc:
            errors.append(f"{api_name}: {exc}")
            self.availability.mark(
                db,
                scope="stock",
                key=ticker,
                dataset=api_name,
                provider="tushare",
                status="failed",
                missing_reason=str(exc),
            )
            return []

    def _upsert_daily_with_qfq(
        self,
        db: Session,
        ticker: str,
        daily_rows: list[dict[str, Any]],
        adj_rows: list[dict[str, Any]],
    ) -> tuple[int, int]:
        adj_by_date = {str(item.get("trade_date")): _to_float(item.get("adj_factor")) for item in adj_rows}
        latest_factor = None
        for trade_date in sorted(adj_by_date.keys(), reverse=True):
            if adj_by_date[trade_date]:
                latest_factor = adj_by_date[trade_date]
                break
        raw_count = 0
        qfq_count = 0
        for item in daily_rows:
            trade_date = _parse_tushare_date(item.get("trade_date"))
            if not trade_date:
                continue
            raw_count += self._upsert_daily_row(db, ticker, trade_date, item, "raw", factor=None)
            factor = adj_by_date.get(str(item.get("trade_date")))
            if factor and latest_factor:
                qfq_count += self._upsert_daily_row(db, ticker, trade_date, item, "qfq", factor=factor / latest_factor)
        return raw_count, qfq_count

    def _upsert_daily_row(
        self,
        db: Session,
        ticker: str,
        trade_date: date,
        item: dict[str, Any],
        adjustment: str,
        factor: float | None,
    ) -> int:
        row = db.query(models.PriceDaily).filter(
            models.PriceDaily.ticker == ticker,
            models.PriceDaily.trade_date == trade_date,
            models.PriceDaily.adjustment == adjustment,
        ).first()
        if not row:
            row = models.PriceDaily(ticker=ticker, trade_date=trade_date, adjustment=adjustment)
            db.add(row)
        multiplier = factor if factor is not None else 1.0
        row.open = _mul(item.get("open"), multiplier)
        row.high = _mul(item.get("high"), multiplier)
        row.low = _mul(item.get("low"), multiplier)
        row.close = _mul(item.get("close"), multiplier)
        row.volume = _to_float(item.get("vol"))
        row.amount = _to_float(item.get("amount"))
        row.source = "tushare"
        return 1

    def _upsert_valuation(self, db: Session, ticker: str, rows: list[dict[str, Any]]) -> int:
        count = 0
        for item in rows:
            trade_date = _parse_tushare_date(item.get("trade_date"))
            if not trade_date:
                continue
            row = db.query(models.ValuationDaily).filter(
                models.ValuationDaily.ticker == ticker,
                models.ValuationDaily.trade_date == trade_date,
            ).first()
            if not row:
                row = models.ValuationDaily(ticker=ticker, trade_date=trade_date)
                db.add(row)
            for key in ("turnover_rate", "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "total_mv", "circ_mv"):
                setattr(row, key, _to_float(item.get(key)))
            row.raw = sanitize_json_dict(item)
            row.source = "tushare"
            count += 1
        return count

    def _upsert_moneyflow(self, db: Session, ticker: str, rows: list[dict[str, Any]]) -> int:
        count = 0
        for item in rows:
            trade_date = _parse_tushare_date(item.get("trade_date"))
            if not trade_date:
                continue
            row = db.query(models.MoneyflowDaily).filter(
                models.MoneyflowDaily.ticker == ticker,
                models.MoneyflowDaily.trade_date == trade_date,
            ).first()
            if not row:
                row = models.MoneyflowDaily(ticker=ticker, trade_date=trade_date)
                db.add(row)
            for key in ("buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"):
                setattr(row, key, _to_float(item.get(key)))
            row.raw = sanitize_json_dict(item)
            row.source = "tushare"
            count += 1
        return count

    def _upsert_limit_price(self, db: Session, ticker: str, rows: list[dict[str, Any]]) -> int:
        count = 0
        for item in rows:
            trade_date = _parse_tushare_date(item.get("trade_date"))
            if not trade_date:
                continue
            row = db.query(models.LimitPriceDaily).filter(
                models.LimitPriceDaily.ticker == ticker,
                models.LimitPriceDaily.trade_date == trade_date,
            ).first()
            if not row:
                row = models.LimitPriceDaily(ticker=ticker, trade_date=trade_date)
                db.add(row)
            row.up_limit = _to_float(item.get("up_limit"))
            row.down_limit = _to_float(item.get("down_limit"))
            row.raw = sanitize_json_dict(item)
            row.source = "tushare"
            count += 1
        return count

    def _refresh_indicators(self, db: Session, ticker: str, adjustment: str) -> int:
        rows = (
            db.query(models.PriceDaily)
            .filter(models.PriceDaily.ticker == ticker, models.PriceDaily.adjustment == adjustment)
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
        indicators = calculate_indicators(pd.DataFrame(daily))
        count = 0
        for _, item in indicators.iterrows():
            row = db.query(models.TechnicalIndicator).filter(
                models.TechnicalIndicator.ticker == ticker,
                models.TechnicalIndicator.trade_date == item["trade_date"],
                models.TechnicalIndicator.adjustment == adjustment,
            ).first()
            if not row:
                row = models.TechnicalIndicator(ticker=ticker, trade_date=item["trade_date"], adjustment=adjustment)
                db.add(row)
            for key in ("ma5", "ma10", "ma20", "ma60", "rsi14", "macd", "macd_signal", "macd_hist", "atr14"):
                setattr(row, key, _to_float(item.get(key)))
            count += 1
        return count

    def _mark_stock_dataset(self, db: Session, ticker: str, dataset: str, count: int, rows: list[dict[str, Any]]) -> None:
        latest = None
        for item in rows:
            trade_date = item.get("trade_date") or item.get("end_date")
            if trade_date and (latest is None or str(trade_date) > latest):
                latest = str(trade_date)
        self.availability.mark(
            db,
            scope="stock",
            key=ticker,
            dataset=dataset,
            provider="tushare",
            status="ready" if count else "empty",
            row_count=count,
            as_of=_date_text(latest),
            missing_reason=None if count else "provider returned no rows",
        )


def _tushare_exchange(ticker: str) -> str:
    normalized = normalize_a_share_ticker(ticker)
    if normalized.endswith(".SH"):
        return "SSE"
    if normalized.endswith(".SZ"):
        return "SZSE"
    if normalized.endswith(".BJ"):
        return "BSE"
    return ""


def _fmt_tushare_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _parse_tushare_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except Exception:
        return None


def _parse_yyyymmdd_or_iso(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            continue
    return None


def _date_text(value: str | None) -> str | None:
    parsed = _parse_tushare_date(value)
    return parsed.isoformat() if parsed else value


def _to_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _mul(value: Any, multiplier: float) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    return number * multiplier


def _dedupe_tushare_period_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_period: dict[str, dict[str, Any]] = {}
    for item in rows:
        period = str(item.get("end_date") or item.get("trade_date") or "").strip()
        if not period:
            continue
        existing = best_by_period.get(period)
        if existing is None or _period_row_rank(item) > _period_row_rank(existing):
            best_by_period[period] = item
    return sorted(best_by_period.values(), key=lambda item: str(item.get("end_date") or item.get("trade_date") or ""), reverse=True)


def _period_row_rank(item: dict[str, Any]) -> tuple[str, str, str]:
    update_flag = str(item.get("update_flag") or "")
    f_ann_date = str(item.get("f_ann_date") or "")
    ann_date = str(item.get("ann_date") or "")
    return (update_flag, f_ann_date, ann_date)


def _statement_fields(api_name: str) -> str:
    fields = {
        "income": "ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,revenue,operate_profit,total_profit,n_income,n_income_attr_p,basic_eps,diluted_eps,update_flag",
        "balancesheet": "ts_code,ann_date,f_ann_date,end_date,report_type,total_assets,total_liab,total_hldr_eqy_exc_min_int,total_share,cap_rese,undistr_porfit,update_flag",
        "cashflow": "ts_code,ann_date,f_ann_date,end_date,report_type,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,c_cash_equ_end_period,free_cashflow,update_flag",
    }
    return fields.get(api_name, "")
