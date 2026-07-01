from __future__ import annotations

from datetime import datetime, timedelta
from html import unescape
import re
import time
from typing import Any

import pandas as pd
import requests

from .network import direct_network
from .symbol import normalize_a_share_ticker, to_akshare_symbol, to_sina_symbol


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if pd.isna(value):
            return None
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "").replace("%", "")
            if cleaned in {"--", "-", "None", "null", "nan"}:
                return None
            value = cleaned
        return float(value)
    except Exception:
        return None


class AkShareProvider:
    source = "akshare"
    fund_flow_stale_days = 10

    def __init__(self) -> None:
        self._concept_index_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        self._concept_constituent_cache: dict[str, tuple[datetime, dict[str, int]]] = {}

    def _ak(self):
        import akshare as ak

        return ak

    def _request_json(
        self,
        label: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 10,
        attempts: int = 2,
        backoff_seconds: float = 0.6,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        session = requests.Session()
        session.trust_env = False
        merged_headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        if headers:
            merged_headers.update(headers)
        for attempt in range(1, attempts + 1):
            try:
                with direct_network():
                    response = session.get(url, params=params, headers=merged_headers, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError(f"{label} returned non-dict payload")
                return data
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(backoff_seconds * attempt)
        raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}") from last_error

    def _request_text(
        self,
        label: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 10,
        attempts: int = 2,
        backoff_seconds: float = 0.6,
    ) -> str:
        last_error: Exception | None = None
        session = requests.Session()
        session.trust_env = False
        merged_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            merged_headers.update(headers)
        for attempt in range(1, attempts + 1):
            try:
                with direct_network():
                    response = session.get(url, params=params, headers=merged_headers, timeout=timeout)
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(backoff_seconds * attempt)
        raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}") from last_error

    def _fetch_a_share_spot_df(self) -> pd.DataFrame:
        ak = self._ak()
        candidates = [
            "stock_zh_a_spot_em",
            "stock_zh_a_spot",
        ]
        last_error: Exception | None = None
        for func_name in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                df = self._call_with_retry(func_name, func)
                if "代码" not in df.columns or "名称" not in df.columns:
                    last_error = ValueError(f"AKShare symbol schema missing required columns: {list(df.columns)}")
                    continue
                return df
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"AKShare A-share snapshot unavailable: {last_error}")

    def _call_with_retry(self, label: str, func, *args, attempts: int = 3, backoff_seconds: float = 0.8, **kwargs):
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with direct_network():
                    return func(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(backoff_seconds * attempt)
        raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}") from last_error

    def _normalize_symbol_search(self, query: str) -> dict[str, str | None]:
        raw_query = str(query or "").strip()
        upper_query = raw_query.upper()
        digits = "".join(re.findall(r"\d", upper_query))
        code = digits[-6:] if len(digits) >= 6 else (digits if digits else None)
        ticker = None
        if code and len(code) == 6:
            ticker = normalize_a_share_ticker(code)
        return {
            "raw": raw_query,
            "upper": upper_query,
            "code": code,
            "ticker": ticker,
        }

    def list_a_share_instruments(self) -> list[dict[str, Any]]:
        df = self._fetch_a_share_spot_df()
        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            code = str(row.get("代码") or "").strip().zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            ticker = normalize_a_share_ticker(code)
            rows.append(
                {
                    "ticker": ticker,
                    "code": code,
                    "name": str(row.get("名称") or "").strip() or None,
                    "exchange": ticker.rsplit(".", 1)[-1] if "." in ticker else None,
                    "market": "A股",
                    "source": self.source,
                }
            )
        return rows

    def search_a_share_symbols(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = self._normalize_symbol_search(query)
        raw_query = str(normalized_query["raw"] or "").strip()
        if not raw_query:
            return []
        q_upper = str(normalized_query["upper"] or "")
        q_code = normalized_query["code"]
        q_ticker = str(normalized_query["ticker"] or "")
        df = self._fetch_a_share_spot_df()

        codes = df["代码"].astype(str).str.zfill(6).str.upper()
        tickers = codes.map(normalize_a_share_ticker)
        names = df["名称"].astype(str)
        mask = names.str.contains(raw_query, case=False, na=False)
        if q_code:
            mask = mask | codes.str.contains(q_code, case=False, na=False)
        if q_ticker:
            mask = mask | tickers.str.contains(q_ticker, case=False, na=False)
        rows: list[dict[str, Any]] = []
        for _, row in df[mask].head(max(1, min(limit, 50))).iterrows():
            code = str(row.get("代码") or "").zfill(6)
            ticker = normalize_a_share_ticker(code)
            rows.append(
                {
                    "ticker": ticker,
                    "code": code,
                    "name": row.get("名称"),
                    "exchange": ticker.rsplit(".", 1)[-1] if "." in ticker else None,
                    "market": "A股",
                    "source": self.source,
                }
            )
        if not rows and q_upper:
            exact_mask = codes == q_upper
            if q_ticker:
                exact_mask = exact_mask | (tickers == q_ticker)
            for _, row in df[exact_mask].head(max(1, min(limit, 50))).iterrows():
                code = str(row.get("代码") or "").zfill(6)
                ticker = normalize_a_share_ticker(code)
                rows.append(
                    {
                        "ticker": ticker,
                        "code": code,
                        "name": row.get("名称"),
                        "exchange": ticker.rsplit(".", 1)[-1] if "." in ticker else None,
                        "market": "A股",
                        "source": self.source,
                    }
                )
        return rows

    def get_realtime_snapshot(self, ticker: str) -> dict[str, Any]:
        try:
            data = self._get_realtime_snapshot_sina(ticker)
            data["source"] = "sina"
            return data
        except Exception as sina_error:
            try:
                data = self._get_realtime_snapshot_eastmoney(ticker)
                data["source"] = "eastmoney"
                return data
            except Exception as eastmoney_error:
                raise RuntimeError(
                    f"Sina failed: {sina_error}; EastMoney fallback failed: {eastmoney_error}"
                ) from eastmoney_error

    def get_sina_realtime_snapshot(self, ticker: str, timeout: int = 10) -> dict[str, Any]:
        data = self._get_realtime_snapshot_sina(ticker, timeout=timeout)
        data["source"] = "sina"
        return data

    def get_eastmoney_realtime_snapshot(self, ticker: str, timeout: int = 10) -> dict[str, Any]:
        data = self._get_realtime_snapshot_eastmoney(ticker, timeout=timeout)
        data["source"] = "eastmoney"
        return data

    def _get_realtime_snapshot_eastmoney(self, ticker: str, timeout: int = 10) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        symbol = to_akshare_symbol(normalized)
        market_code = "1" if normalized.endswith(".SH") else "0"
        payload = self._request_json(
            "eastmoney_stock_snapshot",
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
                "secid": f"{market_code}.{symbol}",
            },
            timeout=timeout,
            attempts=1,
        )
        item = payload.get("data") or {}
        if not item or str(item.get("f57") or "").strip() != symbol:
            raise ValueError(f"EastMoney realtime snapshot not found for {ticker}")
        return {
            "ticker": normalized,
            "name": item.get("f58"),
            "price": _safe_float(item.get("f43")),
            "change_pct": _safe_float(item.get("f170")),
            "change_amount": _safe_float(item.get("f169")),
            "open": _safe_float(item.get("f46")),
            "high": _safe_float(item.get("f44")),
            "low": _safe_float(item.get("f45")),
            "prev_close": _safe_float(item.get("f60")),
            "volume": _safe_float(item.get("f47")),
            "amount": _safe_float(item.get("f48")),
            "source": "eastmoney",
        }

    def _get_realtime_snapshot_sina(self, ticker: str, timeout: int = 10) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        sina_symbol = to_sina_symbol(normalized)

        session = requests.Session()
        session.trust_env = False
        with direct_network():
            resp = session.get(
                f"https://hq.sinajs.cn/list={sina_symbol}",
                headers={
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=timeout,
            )
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        if '=""' in text or '="";' in text:
            raise ValueError(f"Sina quote not found for {ticker}")
        raw = text.split('="', 1)[1].rsplit('";', 1)[0]
        fields = raw.split(",")
        if len(fields) < 32:
            raise ValueError(f"Unexpected Sina quote schema for {ticker}: {raw[:120]}")

        open_price = _safe_float(fields[1])
        prev_close = _safe_float(fields[2])
        price = _safe_float(fields[3])
        change_amount = None
        change_pct = None
        if price is not None and prev_close not in (None, 0):
            change_amount = price - prev_close
            change_pct = change_amount / prev_close * 100

        return {
            "ticker": normalized,
            "name": fields[0] or None,
            "price": price,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "open": open_price,
            "high": _safe_float(fields[4]),
            "low": _safe_float(fields[5]),
            "prev_close": prev_close,
            "volume": _safe_float(fields[8]),
            "amount": _safe_float(fields[9]),
            "source": "sina",
        }

    def get_daily_prices(
        self,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjustment: str = "qfq",
    ) -> pd.DataFrame:
        ak = self._ak()
        normalized = normalize_a_share_ticker(ticker)
        symbol = to_akshare_symbol(normalized)
        start = (start_date or "20200101").replace("-", "")
        end = (end_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
        adjust = "" if adjustment == "raw" else "qfq"
        with direct_network():
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust)
        if df.empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "ticker": normalized,
                "trade_date": pd.to_datetime(df["日期"]).dt.date,
                "adjustment": adjustment,
                "open": pd.to_numeric(df["开盘"], errors="coerce"),
                "high": pd.to_numeric(df["最高"], errors="coerce"),
                "low": pd.to_numeric(df["最低"], errors="coerce"),
                "close": pd.to_numeric(df["收盘"], errors="coerce"),
                "volume": pd.to_numeric(df["成交量"], errors="coerce"),
                "amount": pd.to_numeric(df["成交额"], errors="coerce"),
                "source": self.source,
            }
        )

    def get_stock_news(self, ticker: str, limit: int = 20) -> list[dict[str, Any]]:
        ak = self._ak()
        normalized = normalize_a_share_ticker(ticker)
        symbol = to_akshare_symbol(normalized)
        try:
            with direct_network():
                df = ak.stock_news_em(symbol=symbol)
        except TypeError:
            with direct_network():
                df = ak.stock_news_em(symbol)
        if df.empty:
            return []

        items: list[dict[str, Any]] = []
        for _, row in df.head(limit).iterrows():
            published_at = None
            for key in ("发布时间", "时间", "日期"):
                if key in row and pd.notna(row[key]):
                    published_at = pd.to_datetime(row[key], errors="coerce")
                    if pd.notna(published_at):
                        published_at = published_at.to_pydatetime()
                    else:
                        published_at = None
                    break
            items.append(
                {
                    "ticker": normalized,
                    "title": str(row.get("新闻标题") or row.get("标题") or ""),
                    "summary": str(row.get("新闻内容") or row.get("摘要") or "")[:1000] or None,
                    "source": str(row.get("文章来源") or row.get("来源") or "东方财富"),
                    "url": row.get("新闻链接") or row.get("链接"),
                    "published_at": published_at,
                }
            )
        return [item for item in items if item["title"]]

    def get_index_spot(self) -> list[dict[str, Any]]:
        try:
            ak = self._ak()
            with direct_network():
                df = ak.stock_zh_index_spot_em()
            return self._format_index_rows(df)
        except Exception:
            return self._get_sina_index_spot()

    def get_sina_index_spot(self, timeout: int = 10) -> list[dict[str, Any]]:
        return self._get_sina_index_spot(timeout=timeout)

    def get_eastmoney_index_spot(self, core_names: list[str] | None = None) -> list[dict[str, Any]]:
        ak = self._ak()
        with direct_network():
            df = ak.stock_zh_index_spot_em()
        if core_names is not None:
            df = df[df["名称"].isin(core_names)]
        return self._format_index_rows(df)

    def get_core_index_spot(self) -> list[dict[str, Any]]:
        """获取核心指数实时快照（按名称过滤，固定6个）"""
        core_names = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '中证1000']
        try:
            rows = [row for row in self._get_sina_index_spot() if row.get("name") in core_names]
            if rows:
                return rows
        except Exception:
            pass
        try:
            ak = self._ak()
            with direct_network():
                df = ak.stock_zh_index_spot_em()
            df_core = df[df["名称"].isin(core_names)]
            return self._format_index_rows(df_core)
        except Exception:
            return []

    def _format_index_rows(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "symbol": str(row.get("代码") or ""),
                    "name": row.get("名称"),
                    "category": "index",
                    "price": _safe_float(row.get("最新价")),
                    "change_pct": _safe_float(row.get("涨跌幅")),
                    "amount": _safe_float(row.get("成交额")),
                    "raw": row.where(pd.notna(row), None).to_dict(),
                    "source": self.source,
                }
            )
        return [row for row in rows if row["symbol"]]

    def _get_sina_index_spot(self, timeout: int = 10) -> list[dict[str, Any]]:
        symbols = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000688": "科创50",
            "sh000300": "沪深300",
            "sh000905": "中证500",
            "sh000852": "中证1000",
        }
        session = requests.Session()
        session.trust_env = False
        with direct_network():
            resp = session.get(
                "https://hq.sinajs.cn/list=" + ",".join(symbols.keys()),
                headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            if '="' not in line or '";' not in line:
                continue
            symbol = line.split("hq_str_", 1)[1].split("=", 1)[0]
            raw = line.split('="', 1)[1].rsplit('";', 1)[0]
            fields = raw.split(",")
            if len(fields) < 4:
                continue
            open_price = _safe_float(fields[1])
            prev_close = _safe_float(fields[2])
            latest_price = _safe_float(fields[3])
            change_pct = None
            if latest_price is not None and prev_close not in (None, 0):
                change_pct = (latest_price - prev_close) / prev_close * 100
            rows.append(
                {
                    "symbol": symbol,
                    "name": fields[0] or symbols.get(symbol),
                    "category": "index",
                    "price": latest_price,
                    "change_pct": change_pct,
                    "open": open_price,
                    "prev_close": prev_close,
                    "amount": _safe_float(fields[9]) if len(fields) > 9 else None,
                    "raw": {"raw": raw},
                    "source": "sina",
                }
            )
        return rows

    def get_sector_spot(self) -> list[dict[str, Any]]:
        ak = self._ak()
        candidates = [
            ("stock_board_industry_spot_em", (), {}, "eastmoney_industry"),
            ("stock_board_industry_name_em", (), {}, "eastmoney_industry"),
            ("stock_sector_spot", (), {"indicator": "行业"}, "sina_sector"),
            ("stock_sector_spot", (), {"indicator": "新浪行业"}, "sina_sector"),
        ]
        last_error: Exception | None = None
        for func_name, args, kwargs, source_name in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                df = self._call_with_retry(func_name, func, *args, **kwargs)
                result = self._format_sector_rows(df, source_name)
                if result:
                    return result
                last_error = ValueError(f"{func_name} returned no usable sector rows")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"AKShare sector board unavailable: {last_error}")

    def get_market_breadth_snapshot(self) -> dict[str, Any]:
        """获取市场广度快照，包含涨跌数和两市成交额"""
        try:
            return self._get_market_breadth_snapshot_legu()
        except Exception:
            pass

        df_stocks = self._fetch_a_share_spot_df()
        if df_stocks.empty or "涨跌幅" not in df_stocks.columns:
            raise ValueError("AKShare breadth schema missing 涨跌幅 column")

        change_pct = pd.to_numeric(df_stocks["涨跌幅"], errors="coerce")
        up_count = int((change_pct > 0).sum())
        down_count = int((change_pct < 0).sum())
        flat_count = int((change_pct == 0).sum())
        limit_up_count = int((change_pct >= 9.5).sum())
        limit_down_count = int((change_pct <= -9.5).sum())
        strong_count = int((change_pct >= 5).sum())
        weak_count = int((change_pct <= -5).sum())

        # 从指数数据获取两市成交额（优先 Sina，避免 EastMoney 盘中不稳定）
        total_amount = 0.0
        total_volume = 0.0
        try:
            sina_rows = self._get_sina_index_spot()
            market_rows = [row for row in sina_rows if row.get("name") in {"上证指数", "深证成指"}]
            if market_rows:
                amount_series = pd.to_numeric(pd.Series([row.get("amount") for row in market_rows]), errors="coerce")
                volume_series = pd.to_numeric(pd.Series([row.get("raw", {}).get("raw", "").split(",")[8] if row.get("raw") else None for row in market_rows]), errors="coerce")
                if amount_series.notna().any():
                    total_amount = float(amount_series.sum())
                if volume_series.notna().any():
                    total_volume = float(volume_series.sum())
        except Exception as e:
            # 如果获取指数数据失败，记录但不影响其他数据
            import logging
            logging.getLogger(__name__).warning(f"Failed to get market amount from indices: {e}")
            if "成交额" in df_stocks.columns:
                amount_series = pd.to_numeric(df_stocks["成交额"], errors="coerce")
                if amount_series.notna().any():
                    total_amount = float(amount_series.sum())
            if "成交量" in df_stocks.columns:
                volume_series = pd.to_numeric(df_stocks["成交量"], errors="coerce")
                if volume_series.notna().any():
                    total_volume = float(volume_series.sum())

        return {
            "market": "A股",
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": flat_count,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "strong_count": strong_count,
            "weak_count": weak_count,
            "median_change_pct": _safe_float(change_pct.median()),
            "avg_change_pct": _safe_float(change_pct.mean()),
            "total_amount": total_amount,  # 两市总成交额（元）
            "total_amount_billion": total_amount / 100000000 if total_amount > 0 else 0.0,  # 亿元
            "total_volume": total_volume,  # 两市总成交量（手）
            "source": self.source,
            "raw": {
                "sample_size": int(len(df_stocks)),
                "top_gainers": self._top_movers(df_stocks, ascending=False),
                "top_losers": self._top_movers(df_stocks, ascending=True),
            },
        }

    def _get_market_breadth_snapshot_legu(self) -> dict[str, Any]:
        html = self._request_text(
            "legu_market_breadth",
            "https://legulegu.com/stockdata/market-activity",
            timeout=12,
        )
        match = re.search(
            r'<meta[^>]+name="description"[^>]+content="([^"]+)"',
            html,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("Legu market breadth description meta not found")
        description = unescape(match.group(1))

        def extract(pattern: str) -> int | None:
            found = re.search(pattern, description)
            if not found:
                return None
            return int(found.group(1))

        up_count = extract(r"(\d+)家上涨")
        down_count = extract(r"(\d+)家下跌")
        limit_up_count = extract(r"(\d+)家涨停")
        limit_down_count = extract(r"(\d+)家跌停")
        up_5_7 = extract(r"(\d+)家上涨5%~7%") or 0
        up_7_10 = extract(r"(\d+)家上涨7%~10%") or 0
        up_10_20 = extract(r"(\d+)家上涨10%~20%") or 0
        down_5_7 = extract(r"(\d+)家下跌5%~7%") or 0
        down_7_10 = extract(r"(\d+)家下跌7%~10%") or 0
        down_10_20 = extract(r"(\d+)家下跌10%~20%") or 0
        stats_date = None
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", description)
        if date_match:
            stats_date = date_match.group(1)

        if up_count is None or down_count is None:
            raise ValueError(f"Legu market breadth parse failed: {description[:200]}")

        total_amount = 0.0
        total_volume = 0.0
        try:
            sina_rows = self._get_sina_index_spot()
            market_rows = [row for row in sina_rows if row.get("name") in {"上证指数", "深证成指"}]
            if market_rows:
                amount_series = pd.to_numeric(pd.Series([row.get("amount") for row in market_rows]), errors="coerce")
                volume_series = pd.to_numeric(
                    pd.Series(
                        [
                            row.get("raw", {}).get("raw", "").split(",")[8] if row.get("raw") else None
                            for row in market_rows
                        ]
                    ),
                    errors="coerce",
                )
                if amount_series.notna().any():
                    total_amount = float(amount_series.sum())
                if volume_series.notna().any():
                    total_volume = float(volume_series.sum())
        except Exception:
            pass

        return {
            "market": "A股",
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": None,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "strong_count": up_5_7 + up_7_10 + up_10_20,
            "weak_count": down_5_7 + down_7_10 + down_10_20,
            "median_change_pct": None,
            "avg_change_pct": None,
            "total_amount": total_amount,
            "total_amount_billion": total_amount / 100000000 if total_amount > 0 else 0.0,
            "total_volume": total_volume,
            "source": "legu",
            "raw": {
                "description": description,
                "stats_date": stats_date,
            },
        }

    def get_market_fund_flow_snapshot(self) -> dict[str, Any]:
        ak = self._ak()
        candidates = [
            ("stock_market_fund_flow", (), {}),
            ("stock_hsgt_fund_flow_summary_em", (), {}),
            ("stock_hsgt_hist_em", ("北向资金",), {}),
        ]
        last_error: Exception | None = None
        combined: dict[str, Any] | None = None
        for func_name, args, kwargs in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                with direct_network():
                    df = func(*args, **kwargs)
                if df is None or getattr(df, "empty", True):
                    continue
                payload = self._format_fund_flow_rows(df, func_name)
                if self._fund_flow_signal_count(payload) == 0:
                    continue
                if combined is None:
                    combined = payload
                else:
                    combined = self._merge_fund_flow_payload(combined, payload)
            except Exception as exc:
                last_error = exc
        if combined is not None and self._fund_flow_signal_count(combined) > 0:
            return combined
        raise RuntimeError(f"AKShare market fund flow unavailable: {last_error}")

    def get_theme_spot(self, limit: int = 50) -> list[dict[str, Any]]:
        ak = self._ak()
        candidates = [
            ("stock_board_concept_spot_em", (), {}, "eastmoney_concept"),
            ("stock_board_concept_name_em", (), {}, "eastmoney_concept"),
            ("stock_sector_spot", (), {"indicator": "概念"}, "sina_concept"),
        ]
        last_error: Exception | None = None
        for func_name, args, kwargs, source_name in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                df = self._call_with_retry(func_name, func, *args, **kwargs)
                if df is None or getattr(df, "empty", True):
                    continue
                if source_name == "sina_concept":
                    return self._format_theme_rows_from_sector_spot(df, limit, category="concept")
                rows = self._format_theme_rows(df, func_name, limit)
                rows.sort(key=lambda item: item.get("change_pct") if item.get("change_pct") is not None else float("-inf"), reverse=True)
                return rows[:limit]
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"AKShare theme board unavailable: {last_error}")

    def get_stock_individual_info(self, symbol: str) -> dict[str, Any]:
        ak = self._ak()
        normalized = normalize_a_share_ticker(symbol)
        code = to_akshare_symbol(normalized)
        with direct_network():
            df = ak.stock_individual_info_em(code)
        if df is None or getattr(df, "empty", True):
            raise RuntimeError(f"AKShare individual info unavailable for {normalized}")
        result: dict[str, Any] = {}
        for _, row in df.iterrows():
            item = str(row.get("item") or "").strip()
            if not item:
                continue
            result[item] = row.get("value")
        return result

    def get_stock_hot_ranks(self, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
        ak = self._ak()
        with direct_network():
            rank_df = ak.stock_hot_rank_em()
            up_df = ak.stock_hot_up_em()
        return {
            "rank": self._frame_to_records(rank_df, limit, ["当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"]),
            "up": self._frame_to_records(up_df, limit, ["排名较昨日变动", "当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"]),
        }

    def get_stock_northbound_history(self, symbol: str = "北向资金", limit: int = 60) -> list[dict[str, Any]]:
        ak = self._ak()
        with direct_network():
            df = ak.stock_hsgt_hist_em(symbol)
        return self._frame_to_records(
            df.sort_values("日期", ascending=False).head(limit),
            limit,
            ["日期", "当日成交净买额", "买入成交额", "卖出成交额", "历史累计净买额", "当日资金流入", "当日余额", "持股市值", "领涨股", "领涨股-涨跌幅"],
        )

    def get_stock_individual_fund_flow(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        ak = self._ak()
        normalized = normalize_a_share_ticker(symbol)
        code = to_akshare_symbol(normalized)
        market = "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj"
        with direct_network():
            df = ak.stock_individual_fund_flow(stock=code, market=market)
        return self._frame_to_records(
            df.sort_values("日期", ascending=False).head(limit),
            limit,
            [
                "日期",
                "收盘价",
                "涨跌幅",
                "主力净流入-净额",
                "主力净流入-净占比",
                "超大单净流入-净额",
                "超大单净流入-净占比",
                "大单净流入-净额",
                "大单净流入-净占比",
                "中单净流入-净额",
                "中单净流入-净占比",
                "小单净流入-净额",
                "小单净流入-净占比",
            ],
        )

    def get_stock_dragon_tiger_board(self, symbol: str, look_back_days: int = 30) -> list[dict[str, Any]]:
        ak = self._ak()
        end = datetime.now().date()
        start = end - timedelta(days=max(look_back_days, 7))
        with direct_network():
            df = ak.stock_lhb_detail_em(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if df is None or getattr(df, "empty", True):
            return []
        code = str(symbol).replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        if "代码" in df.columns:
            df = df[df["代码"].astype(str).str.zfill(6) == code.zfill(6)]
        return self._frame_to_records(
            df.sort_values("上榜日", ascending=False).head(20),
            20,
            [
                "序号",
                "代码",
                "名称",
                "上榜日",
                "解读",
                "收盘价",
                "涨跌幅",
                "龙虎榜净买额",
                "龙虎榜买入额",
                "龙虎榜卖出额",
                "龙虎榜成交额",
                "市场总成交额",
                "净买额占总成交比",
                "成交额占总成交比",
                "换手率",
                "流通市值",
                "上榜原因",
                "上榜后1日",
                "上榜后2日",
                "上榜后5日",
                "上榜后10日",
            ],
        )

    def get_stock_lockup_expiry(self, symbol: str, forward_days: int = 90, look_back_days: int = 180) -> dict[str, list[dict[str, Any]]]:
        ak = self._ak()
        code = str(symbol).replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        end = datetime.now().date()
        start = end - timedelta(days=max(look_back_days, 30))
        future_end = end + timedelta(days=max(forward_days, 30))
        with direct_network():
            queue_df = ak.stock_restricted_release_queue_em(symbol=code)
            detail_df = ak.stock_restricted_release_detail_em(start_date=start.strftime("%Y%m%d"), end_date=future_end.strftime("%Y%m%d"))
        queue_rows = []
        if queue_df is not None and not getattr(queue_df, "empty", True):
            queue_rows = self._frame_to_records(
                queue_df.sort_values("解禁时间", ascending=False).head(20),
                20,
                [
                    "序号",
                    "解禁时间",
                    "解禁股东数",
                    "解禁数量",
                    "实际解禁数量",
                    "未解禁数量",
                    "实际解禁数量市值",
                    "占总市值比例",
                    "占流通市值比例",
                    "解禁前一交易日收盘价",
                    "限售股类型",
                    "解禁前20日涨跌幅",
                    "解禁后20日涨跌幅",
                ],
            )
        detail_rows = []
        if detail_df is not None and not getattr(detail_df, "empty", True):
            detail_rows = self._frame_to_records(
                detail_df.sort_values("解禁时间", ascending=False).head(20),
                20,
                [
                    "序号",
                    "股票代码",
                    "股票简称",
                    "解禁时间",
                    "限售股类型",
                    "解禁数量",
                    "实际解禁数量",
                    "实际解禁市值",
                    "占解禁前流通市值比例",
                    "解禁前一交易日收盘价",
                    "解禁前20日涨跌幅",
                    "解禁后20日涨跌幅",
                ],
            )
        return {"queue": queue_rows, "detail": detail_rows}

    def get_stock_main_fund_flow(self, symbol: str = "沪深A股") -> list[dict[str, Any]]:
        ak = self._ak()
        with direct_network():
            df = ak.stock_main_fund_flow(symbol=symbol)
        return self._frame_to_records(
            df.head(100),
            100,
            [
                "序号",
                "代码",
                "名称",
                "最新价",
                "今日排行榜-主力净占比",
                "今日排行榜-今日排名",
                "今日排行榜-今日涨跌",
                "5日排行榜-主力净占比",
                "5日排行榜-5日排名",
                "5日排行榜-5日涨跌",
                "10日排行榜-主力净占比",
                "10日排行榜-10日排名",
                "10日排行榜-10日涨跌",
                "所属板块",
            ],
        )

    def get_stock_board_membership(self, symbol: str, top_n: int = 10) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(symbol)
        code = to_akshare_symbol(normalized)
        info = self.get_stock_individual_info(normalized)
        result: dict[str, Any] = {
            "profile": info,
            "industry": info.get("行业"),
            "list_date": info.get("上市时间"),
            "market_cap": info.get("总市值"),
            "float_market_cap": info.get("流通市值"),
            "all_concepts": [],
            "top_concepts": [],
            "top_industries": [],
            "confidence": "low",
        }
        try:
            main_flow = self.get_stock_main_fund_flow("沪深A股")
            for row in main_flow:
                if str(row.get("代码") or "").zfill(6) == code.zfill(6):
                    result["market_rank"] = row
                    if row.get("所属板块"):
                        result["sector"] = row.get("所属板块")
                    break
        except Exception:
            pass
        try:
            matches = self._scan_stock_concepts(code, sector_hint=str(result.get("sector") or "").strip())
            result["all_concepts"] = matches
            result["top_concepts"] = matches[:top_n]
        except Exception:
            pass
        try:
            with direct_network():
                ind_df = self._ak().stock_board_industry_name_em()
            if ind_df is not None and not getattr(ind_df, "empty", True):
                industry_name = result.get("industry")
                if industry_name:
                    board_row = ind_df[ind_df["板块名称"] == industry_name].head(1)
                    if not board_row.empty:
                        with direct_network():
                            cons_df = self._ak().stock_board_industry_cons_em(industry_name)
                        result["top_industries"] = self._frame_to_records(
                            cons_df.head(top_n),
                            top_n,
                            ["序号", "代码", "名称", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅", "最高", "最低", "今开", "昨收", "换手率", "市盈率-动态", "市净率"],
                        )
        except Exception:
            pass
        if result["all_concepts"]:
            result["confidence"] = "high"
        elif result.get("industry") or result.get("sector"):
            result["confidence"] = "medium"
        return result

    def _scan_stock_concepts(self, code: str, sector_hint: str = "") -> list[dict[str, Any]]:
        concept_rows = self._get_concept_index_rows()
        matches: list[dict[str, Any]] = []
        normalized_code = str(code).zfill(6)
        normalized_hint = sector_hint.strip()

        for board in concept_rows:
            name = str(board.get("name") or "").strip()
            if not name:
                continue
            try:
                constituents = self._get_concept_constituents(name)
            except Exception:
                continue
            if normalized_code not in constituents:
                continue
            rank = constituents[normalized_code]
            score = self._score_concept_match(
                board_name=name,
                sector_hint=normalized_hint,
                constituent_rank=rank,
                change_pct=_safe_float(board.get("change_pct")),
            )
            matches.append(
                {
                    "code": board.get("code") or name,
                    "name": name,
                    "change_pct": _safe_float(board.get("change_pct")),
                    "constituent_rank": rank,
                    "score": score,
                    "source_trace": "concept_member_exact",
                }
            )
        matches.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                int(item.get("constituent_rank") or 10**9),
                str(item.get("name") or ""),
            )
        )
        return matches

    def _get_concept_index_rows(self) -> list[dict[str, Any]]:
        cache = self._concept_index_cache
        if cache and cache[0] >= datetime.utcnow() - timedelta(minutes=30):
            return cache[1]

        with direct_network():
            df = self._ak().stock_board_concept_name_em()
        if df is None or getattr(df, "empty", True):
            return []

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            name = str(row.get("板块名称") or row.get("名称") or "").strip()
            if not name:
                continue
            rows.append(
                {
                    "code": str(row.get("板块代码") or row.get("代码") or name).strip(),
                    "name": name,
                    "change_pct": _safe_float(row.get("涨跌幅")),
                }
            )
        self._concept_index_cache = (datetime.utcnow(), rows)
        return rows

    def _get_concept_constituents(self, board_name: str) -> dict[str, int]:
        cached = self._concept_constituent_cache.get(board_name)
        if cached and cached[0] >= datetime.utcnow() - timedelta(days=1):
            return cached[1]

        with direct_network():
            df = self._ak().stock_board_concept_cons_em(board_name)
        mapping: dict[str, int] = {}
        if df is not None and not getattr(df, "empty", True) and "代码" in df.columns:
            for idx, raw_code in enumerate(df["代码"].astype(str).tolist()):
                normalized = str(raw_code).strip().zfill(6)
                if len(normalized) == 6 and normalized.isdigit():
                    mapping[normalized] = idx + 1
        self._concept_constituent_cache[board_name] = (datetime.utcnow(), mapping)
        return mapping

    def _score_concept_match(
        self,
        board_name: str,
        sector_hint: str,
        constituent_rank: int,
        change_pct: float | None,
    ) -> float:
        score = 0.0
        if constituent_rank > 0:
            score += max(0.0, 120.0 - float(constituent_rank))
        if change_pct is not None:
            score += max(-10.0, min(10.0, change_pct))
        if sector_hint and (board_name == sector_hint or board_name in sector_hint or sector_hint in board_name):
            score += 80.0
        return score

    def get_stock_industry_comparison(self, symbol: str, top_n: int = 20) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(symbol)
        code = to_akshare_symbol(normalized)
        info = self.get_stock_individual_info(normalized)
        industry_name = str(info.get("行业") or "").strip()
        rows: list[dict[str, Any]] = []
        target_row: dict[str, Any] | None = None
        if industry_name:
            with direct_network():
                df = self._ak().stock_board_industry_cons_em(industry_name)
            if df is not None and not getattr(df, "empty", True):
                rows = self._frame_to_records(
                    df.sort_values("涨跌幅", ascending=False).head(top_n),
                    top_n,
                    ["序号", "代码", "名称", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅", "最高", "最低", "今开", "昨收", "换手率", "市盈率-动态", "市净率"],
                )
                for row in rows:
                    if str(row.get("代码") or "").zfill(6) == code:
                        target_row = row
                        break
        return {
            "industry_name": industry_name or None,
            "profile": info,
            "rows": rows,
            "target": target_row,
        }

    def _frame_to_records(self, df: pd.DataFrame, limit: int, columns: list[str]) -> list[dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return []
        rows: list[dict[str, Any]] = []
        subset = df.head(limit)
        cols = [col for col in columns if col in subset.columns]
        for _, row in subset.iterrows():
            item = {col: row.get(col) for col in cols}
            rows.append(item)
        return rows

    def _top_movers(self, df: pd.DataFrame, ascending: bool) -> list[dict[str, Any]]:
        ordered = df.sort_values(by="涨跌幅", ascending=ascending).head(5)
        rows: list[dict[str, Any]] = []
        for _, row in ordered.iterrows():
            rows.append(
                {
                    "code": str(row.get("代码") or ""),
                    "name": row.get("名称"),
                    "change_pct": _safe_float(row.get("涨跌幅")),
                    "price": _safe_float(row.get("最新价")),
                }
            )
        return rows

    def _format_fund_flow_rows(self, df: pd.DataFrame, source_name: str) -> dict[str, Any]:
        working = df.copy()
        normalized_columns = {re.sub(r"\s+", "", str(col)).lower(): col for col in working.columns}
        date_column = None
        for candidate in ("日期", "交易日期", "时间", "date", "Date", "trade_date"):
            candidate_key = re.sub(r"\s+", "", candidate).lower()
            for normalized, original in normalized_columns.items():
                if candidate_key == normalized or candidate_key in normalized or normalized in candidate_key:
                    date_column = original
                    break
            if date_column is not None:
                break
        latest_trade_date = None
        if date_column:
            working[date_column] = pd.to_datetime(working[date_column], errors="coerce")
            working = working.sort_values(date_column, ascending=False, na_position="last")
            latest_trade_date = working.iloc[0].get(date_column)
            if pd.notna(latest_trade_date):
                age_days = (datetime.utcnow().date() - pd.Timestamp(latest_trade_date).date()).days
                if age_days > self.fund_flow_stale_days:
                    raise ValueError(
                        f"AKShare fund flow data is stale: {pd.Timestamp(latest_trade_date).date().isoformat()}"
                    )
        lower_map = {str(key).lower(): key for key in df.columns}
        row_data: dict[str, Any] | None = None
        value_keys: dict[str, float | None] = {}
        for _, candidate in working.iterrows():
            candidate_row = candidate.to_dict()
            candidate_values = {
                "northbound_net_amount": self._pick_float(
                    candidate_row,
                    lower_map,
                    [
                        "北向资金净流入",
                        "北向净流入",
                        "北向资金",
                        "成交净买额",
                        "当日成交净买额",
                        "资金净流入",
                        "当日资金流入",
                        "净流入",
                    ],
                ),
                "main_net_inflow": self._pick_float(
                    candidate_row,
                    lower_map,
                    [
                        "主力净流入",
                        "主力净额",
                        "主力净流入-净额",
                        "大单净流入",
                    ],
                ),
                "super_large_net_inflow": self._pick_float(candidate_row, lower_map, ["超大单净流入", "超大单净额"]),
                "large_net_inflow": self._pick_float(candidate_row, lower_map, ["大单净流入", "大单净额"]),
                "medium_net_inflow": self._pick_float(candidate_row, lower_map, ["中单净流入", "中单净额"]),
                "small_net_inflow": self._pick_float(candidate_row, lower_map, ["小单净流入", "小单净额"]),
            }
            if self._fund_flow_signal_count(candidate_values) > 0:
                row_data = candidate_row
                value_keys = candidate_values
                break
        if row_data is None:
            row_data = working.iloc[0].to_dict()
            value_keys = {
                "northbound_net_amount": self._pick_float(
                    row_data,
                    lower_map,
                    [
                        "北向资金净流入",
                        "北向净流入",
                        "北向资金",
                        "成交净买额",
                        "当日成交净买额",
                        "资金净流入",
                        "当日资金流入",
                        "净流入",
                    ],
                ),
                "main_net_inflow": self._pick_float(
                    row_data,
                    lower_map,
                    [
                        "主力净流入",
                        "主力净额",
                        "主力净流入-净额",
                        "大单净流入",
                    ],
                ),
                "super_large_net_inflow": self._pick_float(row_data, lower_map, ["超大单净流入", "超大单净额"]),
                "large_net_inflow": self._pick_float(row_data, lower_map, ["大单净流入", "大单净额"]),
                "medium_net_inflow": self._pick_float(row_data, lower_map, ["中单净流入", "中单净额"]),
                "small_net_inflow": self._pick_float(row_data, lower_map, ["小单净流入", "小单净额"]),
            }
        return {
            "market": "A股",
            **value_keys,
            "source": self.source,
            "raw": row_data,
        }

    def _pick_float(self, row: dict[str, Any], lower_map: dict[str, str], candidates: list[str]) -> float | None:
        for candidate in candidates:
            key = lower_map.get(candidate.lower())
            if key is None:
                continue
            value = _safe_float(row.get(key))
            if value is not None:
                return value
        return None

    def _fund_flow_signal_count(self, payload: dict[str, Any] | None) -> int:
        if not isinstance(payload, dict):
            return 0
        return sum(
            1
            for key in (
                "northbound_net_amount",
                "main_net_inflow",
                "super_large_net_inflow",
                "large_net_inflow",
                "medium_net_inflow",
                "small_net_inflow",
            )
            if payload.get(key) is not None
        )

    def _merge_fund_flow_payload(self, base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key in (
            "northbound_net_amount",
            "main_net_inflow",
            "super_large_net_inflow",
            "large_net_inflow",
            "medium_net_inflow",
            "small_net_inflow",
        ):
            if merged.get(key) is None and extra.get(key) is not None:
                merged[key] = extra.get(key)
        if not merged.get("raw") and extra.get("raw"):
            merged["raw"] = extra["raw"]
        return merged

    def _format_theme_rows(self, df: pd.DataFrame, source_name: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _, row in df.head(limit).iterrows():
            data = row.to_dict()
            code = str(data.get("板块代码") or data.get("代码") or data.get("概念代码") or data.get("主题代码") or data.get("name") or data.get("名称") or "")
            if not code:
                continue
            rows.append(
                {
                    "theme_code": code,
                    "name": data.get("板块名称") or data.get("名称") or data.get("概念名称") or code,
                    "category": "concept" if "concept" in source_name else "industry",
                    "change_pct": _safe_float(data.get("涨跌幅")),
                    "amount": _safe_float(data.get("成交额")),
                    "leader": data.get("领涨股票") or data.get("龙头股") or data.get("领涨股"),
                    "heat_score": _safe_float(data.get("热度")) or _safe_float(data.get("热度值")),
                    "source": self.source,
                    "raw": data,
                }
            )
        return rows

    def _format_sector_rows(self, df: pd.DataFrame, source_name: str) -> list[dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return []
        if source_name == "sina_sector":
            rows: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                data = row.where(pd.notna(row), None).to_dict()
                symbol = str(data.get("label") or data.get("板块") or "")
                if not symbol:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "name": data.get("板块") or symbol,
                        "category": "sector",
                        "price": _safe_float(data.get("平均价格")),
                        "change_pct": _safe_float(data.get("涨跌幅")),
                        "amount": _safe_float(data.get("总成交额")),
                        "raw": data,
                        "source": "sina",
                    }
                )
            return rows

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "symbol": str(row.get("板块代码") or row.get("代码") or row.get("板块名称") or ""),
                    "name": row.get("板块名称") or row.get("名称"),
                    "category": "sector",
                    "price": _safe_float(row.get("最新价")),
                    "change_pct": _safe_float(row.get("涨跌幅")),
                    "amount": _safe_float(row.get("成交额")),
                    "raw": row.where(pd.notna(row), None).to_dict(),
                    "source": self.source,
                }
            )
        return [row for row in rows if row["symbol"]]

    def _format_theme_rows_from_sector_spot(
        self,
        df: pd.DataFrame,
        limit: int,
        category: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _, row in df.head(limit).iterrows():
            data = row.where(pd.notna(row), None).to_dict()
            code = str(data.get("label") or data.get("板块") or "")
            if not code:
                continue
            rows.append(
                {
                    "theme_code": code,
                    "name": data.get("板块") or code,
                    "category": category,
                    "change_pct": _safe_float(data.get("涨跌幅")),
                    "amount": _safe_float(data.get("总成交额")),
                    "leader": data.get("股票名称"),
                    "heat_score": _safe_float(data.get("公司家数")),
                    "source": "sina",
                    "raw": data,
                }
            )
        return rows

    def get_international_indices(self) -> list[dict[str, Any]]:
        """获取国际指数 - 当前 AkShare 版本不支持，返回空列表"""
        # 当前 AkShare 版本不支持国际指数
        # 未来可以考虑使用其他数据源（如 yfinance）
        return []
