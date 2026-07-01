from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..providers.akshare_provider import AkShareProvider
from ..providers.mootdx_provider import MootdxProvider
from ..providers.network import direct_network
from ..providers.symbol import normalize_a_share_ticker, to_akshare_symbol, to_sina_symbol
from ..providers.tencent_provider import TencentProvider
from .tushare_sync_service import TushareSyncService


class FundamentalService:
    def __init__(self):
        self.akshare = AkShareProvider()
        self.mootdx = MootdxProvider()
        self.tencent = TencentProvider()
        self.tushare_sync = TushareSyncService()

    def refresh_company_profile(self, db: Session, ticker: str) -> models.CompanyProfile:
        normalized = normalize_a_share_ticker(ticker)
        if self.tushare_sync.enabled:
            count = self.tushare_sync.refresh_company_profile(db, normalized)
            profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
            if count and profile:
                return profile
        errors = []
        try:
            raw = self._ak_stock_info(normalized)
        except Exception as exc:
            raw = {"代码": to_akshare_symbol(normalized)}
            errors.append(f"akshare_stock_info: {exc}")

        try:
            finance = self.mootdx.get_finance_snapshot(normalized)
        except Exception as exc:
            finance = {}
            errors.append(f"mootdx_finance: {exc}")
        if finance:
            raw["mootdx_finance"] = finance
        try:
            snapshot = self.tencent.get_realtime_snapshot(normalized)
            raw["tencent_snapshot"] = {
                "name": snapshot.get("name"),
                "price": snapshot.get("price"),
                "valuation": snapshot.get("valuation"),
            }
        except Exception as exc:
            snapshot = {}
            errors.append(f"tencent_snapshot: {exc}")
        if errors:
            raw["data_errors"] = errors

        profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == normalized).first()
        if not profile:
            profile = models.CompanyProfile(ticker=normalized)
            db.add(profile)
        profile.name = raw.get("股票简称") or raw.get("名称") or raw.get("name") or snapshot.get("name")
        profile.industry = raw.get("行业") or raw.get("所属行业")
        profile.area = raw.get("地区") or raw.get("区域")
        profile.market = raw.get("上市市场") or raw.get("market") or "A股"
        profile.list_date = raw.get("上市时间") or raw.get("上市日期")
        profile.raw = _clean_dict(raw)
        profile.source = "akshare+mootdx"
        profile.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(profile)
        return profile

    def refresh_fundamental_metrics(self, db: Session, ticker: str, limit: int = 8) -> int:
        normalized = normalize_a_share_ticker(ticker)
        if self.tushare_sync.enabled:
            result = self.tushare_sync.refresh_fundamentals(db, normalized, limit=limit)
            return int(result.get("metrics") or 0)
        count = 0
        today = datetime.utcnow().date()

        try:
            snap = self.tencent.get_realtime_snapshot(normalized)
            valuation = snap.get("valuation") or {}
            metric = self._get_or_create_metric(db, normalized, today)
            raw = metric.raw or {}
            raw.update({"tencent_valuation": valuation})
            metric.raw = raw
            metric.source = "tencent"
            metric.eps = _first_float(valuation, ("eps",))
            count += 1
        except Exception:
            pass

        try:
            finance = self.mootdx.get_finance_snapshot(normalized)
        except Exception:
            finance = {}
        if finance:
            metric = self._get_or_create_metric(db, normalized, today)
            raw = metric.raw or {}
            raw.update({"mootdx_finance": finance})
            metric.raw = raw
            metric.source = "tencent+mootdx"
            metric.eps = _first_float(finance, ("eps", "meigushouyi"))
            metric.roe = _first_float(finance, ("roe", "jingzichanshouyilv"))
            metric.debt_to_assets = _first_float(finance, ("debt_to_assets", "zichanfuzhailv"))
            count += 1

        forecast = self._ak_profit_forecast(normalized)
        if forecast:
            metric = self._get_or_create_metric(db, normalized, today)
            raw = metric.raw or {}
            raw.update({"profit_forecast_ths": forecast})
            metric.raw = raw
            metric.source = "tencent+mootdx+akshare"
            count += 1

        db.commit()
        return count

    def _get_or_create_metric(self, db: Session, ticker: str, report_date) -> models.FundamentalMetric:
        metric = db.query(models.FundamentalMetric).filter(
            models.FundamentalMetric.ticker == ticker,
            models.FundamentalMetric.report_date == report_date,
        ).first()
        if metric:
            return metric
        metric = models.FundamentalMetric(ticker=ticker, report_date=report_date, period=str(report_date))
        db.add(metric)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            metric = db.query(models.FundamentalMetric).filter(
                models.FundamentalMetric.ticker == ticker,
                models.FundamentalMetric.report_date == report_date,
            ).first()
            if metric:
                return metric
            raise
        return metric

    def refresh_financial_statement(self, db: Session, ticker: str, statement_type: str, limit: int = 8) -> int:
        normalized = normalize_a_share_ticker(ticker)
        if self.tushare_sync.enabled:
            result = self.tushare_sync.refresh_fundamentals(db, normalized, limit=limit)
            key = "balance" if statement_type == "balance" else statement_type
            return int(result.get(key) or 0)
        df = self._ak_financial_statement(normalized, statement_type)
        if df.empty:
            return 0
        count = 0
        for _, row in df.head(limit).iterrows():
            report_date = _guess_report_date(row)
            if report_date is None:
                continue
            statement = db.query(models.FinancialStatement).filter(
                models.FinancialStatement.ticker == normalized,
                models.FinancialStatement.statement_type == statement_type,
                models.FinancialStatement.report_date == report_date,
            ).first()
            if not statement:
                statement = models.FinancialStatement(
                    ticker=normalized,
                    statement_type=statement_type,
                    report_date=report_date,
                )
                db.add(statement)
            statement.period = str(report_date)
            statement.raw = _clean_dict(row.to_dict())
            statement.source = "akshare"
            count += 1
        db.commit()
        return count

    def _ak_stock_info(self, ticker: str) -> dict:
        import akshare as ak

        symbol = to_akshare_symbol(ticker)
        try:
            with direct_network():
                df = ak.stock_individual_info_em(symbol=symbol)
        except TypeError:
            with direct_network():
                df = ak.stock_individual_info_em(symbol)
        if df is None or df.empty:
            return {"代码": symbol}
        if {"item", "value"}.issubset(set(df.columns)):
            return dict(zip(df["item"], df["value"]))
        if {"项目", "值"}.issubset(set(df.columns)):
            return dict(zip(df["项目"], df["值"]))
        return df.iloc[0].where(pd.notna(df.iloc[0]), None).to_dict()

    def _ak_financial_statement(self, ticker: str, statement_type: str) -> pd.DataFrame:
        import akshare as ak

        symbol = to_sina_symbol(ticker)
        funcs = {
            "income": ["stock_financial_report_sina"],
            "balance": ["stock_financial_report_sina"],
            "cashflow": ["stock_financial_report_sina"],
        }
        stock = "利润表" if statement_type == "income" else "资产负债表" if statement_type == "balance" else "现金流量表"
        for func_name in funcs.get(statement_type, []):
            func = getattr(ak, func_name, None)
            if not func:
                continue
            try:
                with direct_network():
                    return func(stock=symbol, symbol=stock)
            except TypeError:
                try:
                    with direct_network():
                        return func(symbol=symbol, stock=stock)
                except TypeError:
                    continue
            except Exception:
                continue
        return pd.DataFrame()

    def _ak_profit_forecast(self, ticker: str) -> list[dict]:
        import akshare as ak

        symbol = to_akshare_symbol(ticker)
        func = getattr(ak, "stock_profit_forecast_ths", None)
        if not func:
            return []
        try:
            with direct_network():
                df = func(symbol=symbol, indicator="预测年报每股收益")
        except Exception:
            return []
        if df is None or df.empty:
            return []
        rows = []
        for _, row in df.head(12).iterrows():
            rows.append(_clean_dict(row.to_dict()))
        return rows


def _guess_report_date(row) -> object | None:
    for key in ("报告日", "报告日期", "截止日期", "报表日期", "date", "end_date"):
        if key in row and pd.notna(row[key]):
            try:
                return pd.to_datetime(row[key]).date()
            except Exception:
                continue
    return None


def _clean_dict(data: dict) -> dict:
    cleaned = {}
    for key, value in data.items():
        try:
            if pd.isna(value):
                cleaned[key] = None
            elif hasattr(value, "item"):
                cleaned[key] = value.item()
            else:
                cleaned[key] = value
        except Exception:
            cleaned[key] = value
    return cleaned


def _first_float(data: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
            return float(value)
        except Exception:
            continue
    return None
