from __future__ import annotations

from datetime import datetime

import pandas as pd

from .symbol import normalize_a_share_ticker, to_akshare_symbol


class BaoStockProvider:
    source = "baostock"

    def _bs_code(self, ticker: str) -> str:
        normalized = normalize_a_share_ticker(ticker)
        symbol = to_akshare_symbol(normalized)
        if normalized.endswith(".SH"):
            return f"sh.{symbol}"
        if normalized.endswith(".SZ"):
            return f"sz.{symbol}"
        if normalized.endswith(".BJ"):
            return f"bj.{symbol}"
        raise ValueError(f"Unsupported A-share ticker for BaoStock: {ticker}")

    def get_daily_prices(
        self,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjustment: str = "qfq",
    ) -> pd.DataFrame:
        import baostock as bs

        normalized = normalize_a_share_ticker(ticker)
        start = _fmt_date(start_date or "20200101")
        end = _fmt_date(end_date or datetime.now().strftime("%Y%m%d"))
        adjustflag = "2" if adjustment == "qfq" else "3"

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                self._bs_code(normalized),
                "date,open,high,low,close,volume,amount",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=adjustflag,
            )
            if rs.error_code != "0":
                raise RuntimeError(f"BaoStock query failed: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=rs.fields)
            return pd.DataFrame(
                {
                "ticker": normalized,
                "trade_date": pd.to_datetime(df["date"]).dt.date,
                "adjustment": adjustment,
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                    "close": pd.to_numeric(df["close"], errors="coerce"),
                    "volume": pd.to_numeric(df["volume"], errors="coerce"),
                    "amount": pd.to_numeric(df["amount"], errors="coerce"),
                    "source": self.source,
                }
            )
        finally:
            bs.logout()


def _fmt_date(value: str) -> str:
    value = value.replace("-", "")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
