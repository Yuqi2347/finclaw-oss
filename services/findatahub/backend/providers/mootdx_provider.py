from __future__ import annotations

from datetime import datetime
import pandas as pd

from .symbol import normalize_a_share_ticker, to_akshare_symbol


class MootdxProvider:
    source = "mootdx"

    def __init__(self):
        self._client = None

    def _quotes(self):
        if self._client is None:
            from mootdx.quotes import Quotes

            self._client = Quotes.factory(market="std")
        return self._client

    def get_daily_prices(
        self,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjustment: str = "raw",
    ) -> pd.DataFrame:
        if adjustment != "raw":
            raise NotImplementedError("Mootdx provider only supplies raw daily prices")
        normalized = normalize_a_share_ticker(ticker)
        code = to_akshare_symbol(normalized)
        df = self._quotes().bars(symbol=code, category=4, offset=800)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.drop(columns=["datetime", "year", "month", "day", "hour", "minute"], errors="ignore")
        df = df.reset_index().rename(
            columns={
                "datetime": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "amount": "amount",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        if start_date:
            start = pd.to_datetime(start_date).date()
            df = df[df["trade_date"] >= start]
        if end_date:
            end = pd.to_datetime(end_date).date()
            df = df[df["trade_date"] <= end]
        if df.empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "ticker": normalized,
                "trade_date": df["trade_date"],
                "adjustment": adjustment,
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "volume": pd.to_numeric(df["volume"], errors="coerce"),
                "amount": pd.to_numeric(df.get("amount"), errors="coerce") if "amount" in df else None,
                "source": self.source,
            }
        )

    def get_finance_snapshot(self, ticker: str) -> dict:
        normalized = normalize_a_share_ticker(ticker)
        code = to_akshare_symbol(normalized)
        try:
            client = self._quotes()
            df = client.finance(symbol=code)
        except Exception:
            return {}
        if df is None or getattr(df, "empty", True):
            return {}
        row = df.iloc[0]
        return {k: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for k, v in row.to_dict().items()}
