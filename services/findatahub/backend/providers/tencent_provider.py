from __future__ import annotations

from typing import Any
import re
import urllib.parse
import urllib.request

from .network import direct_network
from .symbol import infer_exchange, normalize_a_share_ticker, to_akshare_symbol


class TencentProvider:
    source = "tencent"

    def search_instruments(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return []
        url = f"https://smartbox.gtimg.cn/s3/?q={urllib.parse.quote(raw_query)}&t=all"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with direct_network():
            raw = urllib.request.urlopen(req, timeout=3).read().decode("gbk", errors="ignore")

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in re.split(r"[\n\r\^;]", raw):
            match = re.search(r"\b(?P<prefix>sh|sz|bj)(?P<code>\d{6})~(?P<name>[^~,\"]+)", record, re.IGNORECASE)
            if not match:
                match = re.search(r"~(?P<name>[^~,\"]+)~(?P<code>\d{6})~", record)
            if not match:
                continue
            code = match.group("code")
            name = str(match.group("name") or "").strip()
            if not code or not name:
                continue
            ticker = normalize_a_share_ticker(code)
            if ticker in seen:
                continue
            seen.add(ticker)
            results.append(
                {
                    "ticker": ticker,
                    "code": code,
                    "name": name,
                    "exchange": infer_exchange(ticker),
                    "market": "A股",
                    "source": self.source,
                }
            )
            if len(results) >= max(1, limit):
                break
        return results

    def get_realtime_snapshot(self, ticker: str, timeout: int = 10) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker)
        code = to_akshare_symbol(normalized)
        prefixed = f"{_prefix(code)}{code}"
        url = f"https://qt.gtimg.cn/q={prefixed}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with direct_network():
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", errors="ignore")
        if '=""' in raw or "~" not in raw:
            raise ValueError(f"Tencent quote not found for {ticker}")
        vals = raw.split('"')[1].split("~")
        if len(vals) < 53:
            raise ValueError(f"Unexpected Tencent quote schema for {ticker}: {raw[:120]}")
        return {
            "ticker": normalized,
            "name": vals[1] or None,
            "price": _float(vals[3]),
            "change_pct": _float(vals[32]),
            "change_amount": _float(vals[31]) if len(vals) > 31 else None,
            "open": _float(vals[5]),
            "high": _float(vals[33]),
            "low": _float(vals[34]),
            "prev_close": _float(vals[4]),
            "volume": _float(vals[36]) if len(vals) > 36 else None,
            "amount": _float(vals[37]) if len(vals) > 37 else None,
            "source": self.source,
            "valuation": {
                "turnover_pct": _float(vals[38]) if len(vals) > 38 else None,
                "pe_ttm": _float(vals[39]) if len(vals) > 39 else None,
                "market_cap_yi": _float(vals[44]) if len(vals) > 44 else None,
                "float_market_cap_yi": _float(vals[45]) if len(vals) > 45 else None,
                "pb": _float(vals[46]) if len(vals) > 46 else None,
                "limit_up": _float(vals[47]) if len(vals) > 47 else None,
                "limit_down": _float(vals[48]) if len(vals) > 48 else None,
                "pe_static": _float(vals[52]) if len(vals) > 52 else None,
            },
        }


def _prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith("8"):
        return "bj"
    return "sz"


def _float(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None
