from __future__ import annotations

import logging
import json
import time
from datetime import datetime
from typing import Any

import requests

from backend.core.config import DATAHUB_BASE_URL

logger = logging.getLogger(__name__)

STOCK_PACKAGE_DEFAULT_DAILY = 5
STOCK_PACKAGE_DEFAULT_NEWS = 5
STOCK_PACKAGE_SECTION_CHAR_BUDGET = 10_000
STOCK_PACKAGE_MAX_CHAR_BUDGET = 12_000
STOCK_PACKAGE_SECTIONS = {
    "daily",
    "indicators",
    "financials",
    "profile",
    "valuation",
    "moneyflow",
    "limits",
    "freshness",
    "availability",
    "news",
    "events",
    "quality",
    "position",
}


class DataHubClient:
    """DataHub 客户端，支持重试、降级和缓存"""

    # 重试配置
    MAX_RETRIES = 3
    BACKOFF_FACTOR = 0.5
    RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    def __init__(self, base_url: str = DATAHUB_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._cache: dict[str, tuple[Any, float]] = {}  # {key: (data, timestamp)}
        self._cache_ttl = 60  # 缓存 TTL（秒）

    def _get_with_retry(
        self,
        path: str,
        timeout: int = 20,
        use_cache: bool = False,
        retries: int | None = None,
        **params: Any,
    ) -> Any:
        """GET 请求，支持重试和缓存

        Args:
            path: API 路径
            timeout: 超时时间（秒）
            use_cache: 是否使用缓存
            **params: 查询参数

        Returns:
            响应 JSON 数据

        Raises:
            requests.HTTPError: HTTP 错误
            requests.Timeout: 超时错误
        """
        cache_key = f"GET:{path}:{params}"

        # 尝试从缓存读取
        if use_cache:
            cached_data = self._get_from_cache(cache_key)
            if cached_data is not None:
                logger.debug(f"Cache hit: {cache_key}")
                return cached_data

        last_exception = None
        max_retries = max(1, retries if retries is not None else self.MAX_RETRIES)
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    f"{self.base_url}{path}",
                    params=params,
                    timeout=timeout
                )
                resp.raise_for_status()
                data = resp.json()

                # 写入缓存
                if use_cache:
                    self._put_to_cache(cache_key, data)

                return data

            except requests.HTTPError as exc:
                last_exception = exc
                status_code = exc.response.status_code if exc.response is not None else None

                # 不重试的错误码
                if status_code not in self.RETRY_STATUS_CODES:
                    raise

                # 最后一次尝试，不再重试
                if attempt == max_retries - 1:
                    # 尝试从过期缓存读取
                    if use_cache:
                        stale_data = self._get_from_cache(cache_key, allow_stale=True)
                        if stale_data is not None:
                            logger.warning(f"Using stale cache due to error: {exc}")
                            return {"status": "stale_cache", "data": stale_data, "warning": str(exc)}
                    raise

                # 指数退避
                sleep_time = self.BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{max_retries} after {sleep_time}s: {exc}")
                time.sleep(sleep_time)

            except requests.Timeout as exc:
                last_exception = exc

                # 最后一次尝试，尝试从缓存读取
                if attempt == max_retries - 1:
                    if use_cache:
                        stale_data = self._get_from_cache(cache_key, allow_stale=True)
                        if stale_data is not None:
                            logger.warning(f"Using stale cache due to timeout: {exc}")
                            return {"status": "stale_cache", "data": stale_data, "warning": "DataHub timeout"}
                    raise

                # 指数退避
                sleep_time = self.BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{max_retries} after {sleep_time}s: timeout")
                time.sleep(sleep_time)

        # 不应该到达这里
        if last_exception:
            raise last_exception
        raise RuntimeError("Unexpected retry loop exit")

    def _get(self, path: str, timeout: int = 20, **params: Any) -> Any:
        """GET 请求（无重试，保持向后兼容）"""
        resp = requests.get(f"{self.base_url}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict[str, Any], timeout: int = 20) -> Any:
        resp = requests.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, **params: Any) -> Any:
        resp = requests.delete(f"{self.base_url}{path}", params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        resp = requests.patch(f"{self.base_url}{path}", json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _get_from_cache(self, key: str, allow_stale: bool = False) -> Any | None:
        """从缓存读取数据

        Args:
            key: 缓存键
            allow_stale: 是否允许返回过期数据

        Returns:
            缓存数据，如果不存在或过期则返回 None
        """
        if key not in self._cache:
            return None

        data, timestamp = self._cache[key]
        age = time.time() - timestamp

        if age <= self._cache_ttl:
            return data

        if allow_stale:
            return data

        # 过期，删除缓存
        del self._cache[key]
        return None

    def _put_to_cache(self, key: str, data: Any):
        """写入缓存"""
        self._cache[key] = (data, time.time())

    @staticmethod
    def _unwrap_cached_payload(data: Any) -> Any:
        if isinstance(data, dict) and data.get("status") == "stale_cache" and "data" in data:
            return data.get("data")
        return data

    @staticmethod
    def _unwrap_data_payload(data: Any) -> Any:
        """Accept both raw DataHub payloads and optional {"data": ...} envelopes."""
        data = DataHubClient._unwrap_cached_payload(data)
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            return data.get("data")
        return data

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()

    def _invalidate_cache(self) -> None:
        """在写操作后清空缓存，避免看板继续读取旧数据。"""
        self.clear_cache()

    def get_watchlist(self, timeout: int = 20, use_cache: bool = True) -> Any:
        return self._unwrap_cached_payload(self._get_with_retry("/api/watchlist", timeout=timeout, use_cache=use_cache))

    def get_positions(self, timeout: int = 20, use_cache: bool = True) -> Any:
        return self._unwrap_cached_payload(self._get_with_retry("/api/positions", timeout=timeout, use_cache=use_cache))

    def get_portfolio_summary(self, timeout: int = 20, use_cache: bool = True) -> Any:
        return self._unwrap_cached_payload(self._get_with_retry("/api/portfolio/summary", timeout=timeout, use_cache=use_cache))

    def search_stock_symbol(self, query: str, limit: int = 10) -> Any:
        return self._get_with_retry("/api/instruments/search", timeout=4, retries=1, query=query, limit=limit)

    def get_stock_snapshot(
        self,
        ticker: str,
        timeout: int = 20,
        use_cache: bool = True,
        auto_refresh_on_404: bool = True,
    ) -> Any:
        def wrap_snapshot(snapshot: Any, refreshed: bool = False, refresh_result: Any | None = None) -> Any:
            if not isinstance(snapshot, dict):
                return snapshot
            note = "读取 DataHub 行情快照。"
            if refreshed:
                note = "缓存缺失，系统已自动刷新单标的快照后读取。"
            payload = dict(snapshot)
            payload["snapshot"] = snapshot
            payload["time_context"] = self._snapshot_time_context(snapshot)
            payload["refreshed"] = refreshed
            payload["refresh_result"] = self._summarize_refresh_result(refresh_result) if refreshed else None
            payload["note"] = note
            return {
                key: value
                for key, value in payload.items()
                if key != "valuation"
            }

        try:
            snapshot = self._unwrap_cached_payload(
                self._get_with_retry(
                    f"/api/market/snapshot/{ticker}",
                    timeout=timeout,
                    use_cache=use_cache,
                )
            )
            return wrap_snapshot(snapshot)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code != 404 or not auto_refresh_on_404:
                raise
            logger.info(f"Snapshot not found for {ticker}, refreshing...")
            refresh_result = self.refresh_stock_snapshot(ticker, timeout=min(max(timeout, 5), 12))
            time.sleep(0.3)
            try:
                snapshot = self._unwrap_cached_payload(
                    self._get_with_retry(
                        f"/api/market/snapshot/{ticker}",
                        timeout=timeout,
                        use_cache=False,
                        retries=1,
                    )
                )
                return wrap_snapshot(snapshot, refreshed=True, refresh_result=refresh_result)
            except requests.HTTPError as retry_exc:
                if isinstance(refresh_result, dict) and refresh_result.get("snapshot") is not None:
                    return wrap_snapshot(refresh_result["snapshot"], refreshed=True, refresh_result=refresh_result)
                raise retry_exc

    def get_stock_daily(
        self,
        ticker: str,
        limit: int = 5,
        adjustment: str = "qfq",
        timeout: int = 20,
        use_cache: bool = True,
    ) -> Any:
        return self._unwrap_cached_payload(
            self._get_with_retry(
                f"/api/market/daily/{ticker}",
                limit=limit,
                adjustment=adjustment,
                timeout=timeout,
                use_cache=use_cache,
            )
        )

    def get_stock_snapshots_batch(self, tickers: list[str], timeout: int = 20) -> Any:
        return self._unwrap_data_payload(
            self._post("/api/market/snapshots/batch", {"tickers": tickers}, timeout=timeout)
        )

    def get_stock_daily_batch(
        self,
        tickers: list[str],
        limit: int = 10,
        adjustment: str = "qfq",
        timeout: int = 20,
    ) -> Any:
        return self._unwrap_data_payload(
            self._post(
                "/api/market/daily/batch",
                {"tickers": tickers, "limit": limit, "adjustment": adjustment},
                timeout=timeout,
            )
        )

    def get_stock_data_package(
        self,
        ticker: str,
        mode: str = "overview",
        section: str | None = None,
        offset: int = 0,
        limit: int = 0,
        max_chars: int = STOCK_PACKAGE_SECTION_CHAR_BUDGET,
        ensure: bool = True,
    ) -> Any:
        package = self._load_stock_package(ticker, use_cache=True)
        package = self._unwrap_cached_payload(package)
        if not isinstance(package, dict):
            return package

        mode = (mode or "overview").strip().lower()
        selected = str(section or "").strip().lower()
        if ensure:
            required_sections = self._required_stock_package_sections(mode, selected)
            refresh_report = self._ensure_stock_package_coverage(ticker, package, required_sections)
            if refresh_report.get("attempted"):
                package = self._load_stock_package(ticker, use_cache=False)
                package = self._unwrap_cached_payload(package)
                if not isinstance(package, dict):
                    return package
                package["refresh_report"] = refresh_report
        coverage = self._stock_package_coverage(package)
        if mode == "overview":
            overview = self._stock_package_overview(package)
            overview["coverage"] = coverage
            overview["refresh_report"] = package.get("refresh_report")
            return overview
        if mode == "section":
            if selected not in STOCK_PACKAGE_SECTIONS:
                raise ValueError(f"section must be one of: {', '.join(sorted(STOCK_PACKAGE_SECTIONS))}")
            value = self._stock_package_section_value(package, selected)
            return {
                "ticker": package.get("ticker"),
                "mode": "section",
                "section": selected,
                "time_context": self._stock_package_time_context(package, selected),
                "coverage": coverage,
                "refresh_report": package.get("refresh_report"),
                "read_window": self._page_value(value, offset=offset, limit=limit, max_chars=max_chars),
            }
        raise ValueError("mode must be overview or section")

    def _load_stock_package(self, ticker: str, use_cache: bool) -> Any:
        return self._get_with_retry(f"/api/data-package/{ticker}", use_cache=use_cache)

    def _required_stock_package_sections(self, mode: str, section: str) -> set[str]:
        if mode == "section":
            return {section} if section in STOCK_PACKAGE_SECTIONS else set()
        return {"snapshot", "daily", "profile", "financials", "valuation", "moneyflow", "limits"}

    def _ensure_stock_package_coverage(self, ticker: str, package: dict[str, Any], required_sections: set[str]) -> dict[str, Any]:
        report: dict[str, Any] = {"attempted": False, "steps": {}, "errors": []}

        def need_snapshot() -> bool:
            return not bool(package.get("snapshot"))

        def need_daily() -> bool:
            return not bool(package.get("daily"))

        def need_indicators() -> bool:
            return not bool(package.get("indicators"))

        def need_profile() -> bool:
            return not bool(package.get("profile"))

        def need_financials() -> bool:
            return not bool(package.get("metrics")) and not bool(package.get("statements"))

        def need_valuation() -> bool:
            return not bool(package.get("valuation_daily"))

        def need_moneyflow() -> bool:
            return not bool(package.get("moneyflow_daily"))

        def need_limits() -> bool:
            return not bool(package.get("limit_prices"))

        def need_news() -> bool:
            return not bool(package.get("news"))

        def run_step(name: str, fn: Any) -> None:
            report["attempted"] = True
            try:
                report["steps"][name] = self._summarize_refresh_result(fn())
            except Exception as exc:
                report["errors"].append({"step": name, "error": str(exc)})

        if ("snapshot" in required_sections or "profile" in required_sections or "financials" in required_sections) and need_snapshot():
            run_step("snapshot", lambda: self.refresh_stock_snapshot(ticker, timeout=12))
        needs_market_daily = (
            need_daily()
            or need_indicators()
            or ("valuation" in required_sections and need_valuation())
            or ("moneyflow" in required_sections and need_moneyflow())
            or ("limits" in required_sections and need_limits())
        )
        if ("daily" in required_sections or "indicators" in required_sections or "valuation" in required_sections or "moneyflow" in required_sections or "limits" in required_sections) and needs_market_daily:
            run_step("daily", lambda: self.refresh_stock_daily(ticker, timeout=35))
        if ("profile" in required_sections or "financials" in required_sections) and (need_profile() or need_financials()):
            run_step("fundamentals", lambda: self.refresh_stock_fundamentals(ticker))
        if "news" in required_sections and need_news():
            run_step("news", lambda: self.refresh_stock_news(ticker, limit=20))
        return report

    def _stock_package_coverage(self, package: dict[str, Any]) -> dict[str, Any]:
        daily_meta = package.get("daily_meta") or {}
        quality = package.get("quality") or {}
        def rows_status(rows: Any) -> str:
            return "available" if isinstance(rows, list) and len(rows) > 0 else "missing"
        def object_status(value: Any) -> str:
            return "available" if bool(value) else "missing"
        return {
            "instrument": {"status": object_status(package.get("instrument"))},
            "snapshot": {
                "status": object_status(package.get("snapshot")),
                "updated_at": (package.get("snapshot") or {}).get("updated_at") if isinstance(package.get("snapshot"), dict) else None,
                "source": (package.get("snapshot") or {}).get("source") if isinstance(package.get("snapshot"), dict) else None,
            },
            "daily": {
                "status": rows_status(package.get("daily")),
                "rows": len(package.get("daily") or []),
                "latest_trade_date": daily_meta.get("latest_trade_date"),
            },
            "indicators": {"status": rows_status(package.get("indicators")), "rows": len(package.get("indicators") or [])},
            "valuation": {"status": rows_status(package.get("valuation_daily")), "rows": len(package.get("valuation_daily") or [])},
            "moneyflow": {"status": rows_status(package.get("moneyflow_daily")), "rows": len(package.get("moneyflow_daily") or [])},
            "limits": {"status": rows_status(package.get("limit_prices")), "rows": len(package.get("limit_prices") or [])},
            "news": {
                "status": rows_status(package.get("news")),
                "rows": len(package.get("news") or []),
                "providers": (package.get("news_meta") or {}).get("providers") if isinstance(package.get("news_meta"), dict) else None,
            },
            "profile": {
                "status": object_status(package.get("profile")),
                "updated_at": (package.get("profile") or {}).get("updated_at") if isinstance(package.get("profile"), dict) else None,
                "source": (package.get("profile") or {}).get("source") if isinstance(package.get("profile"), dict) else None,
            },
            "financials": self._financials_coverage(package),
            "position": {"status": object_status(package.get("position"))},
            "events": {"status": "available" if (package.get("daily_plan") or package.get("trigger_events")) else "missing"},
            "quality": quality,
            "data_freshness": package.get("data_freshness") or {},
            "data_availability": self._availability_summary(package.get("data_availability")),
        }

    def get_market_context(self, category: str | None = None, limit: int = 50) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        return self._unwrap_cached_payload(self._get_with_retry("/api/market-context/snapshots", use_cache=True, **params))

    def get_market_overview(
        self,
        limit: int = 10,
        timeout: int = 20,
        use_cache: bool = True,
        include_breadth: bool = True,
    ) -> Any:
        return self._unwrap_cached_payload(
            self._get_with_retry(
                "/api/market/overview",
                limit=limit,
                timeout=timeout,
                use_cache=use_cache,
                include_breadth=include_breadth,
            )
        )

    def get_market_package(
        self,
        ticker: str,
        trade_date: str | None = None,
        overview_limit: int = 10,
        news_limit: int = 8,
    ) -> Any:
        params: dict[str, Any] = {"overview_limit": overview_limit, "news_limit": news_limit}
        if trade_date:
            params["trade_date"] = trade_date
        return self._get_with_retry(f"/api/market/package/{ticker}", use_cache=True, **params)

    def add_watchlist_item(
        self,
        ticker: str,
        name: str | None = None,
        list_name: str = "默认关注",
        note: str | None = None,
        reason: str | None = None,
        status: str = "观察",
    ) -> Any:
        try:
            return self._post(
                "/api/watchlist",
                {
                    "ticker": ticker,
                    "name": name,
                    "list_name": list_name,
                    "status": status,
                    "reason": reason if reason is not None else note,
                },
            )
        finally:
            self._invalidate_cache()

    def remove_watchlist_item(self, ticker: str, list_name: str = "默认关注") -> Any:
        try:
            return self._delete(f"/api/watchlist/{ticker}", list_name=list_name)
        finally:
            self._invalidate_cache()

    def remove_position(self, ticker: str) -> Any:
        try:
            return self._delete(f"/api/positions/{ticker}")
        finally:
            self._invalidate_cache()

    def upsert_position(
        self,
        ticker: str,
        name: str | None = None,
        quantity: float | None = None,
        avg_cost: float | None = None,
        cost_price: float | None = None,
        note: str | None = None,
    ) -> Any:
        existing = self._find_position(ticker)
        resolved_cost = cost_price if cost_price is not None else avg_cost
        if quantity is not None and quantity <= 0:
            if existing:
                return self.remove_position(ticker)
            return {
                "status": "ignored",
                "ticker": ticker,
                "reason": "quantity is not positive and no active position exists",
            }
        if existing:
            payload: dict[str, Any] = {}
            if name is not None:
                payload["name"] = name
            if quantity is not None:
                payload["quantity"] = quantity
            if resolved_cost is not None:
                payload["cost_price"] = resolved_cost
            if note is not None:
                payload["note"] = note
            if not payload:
                return existing
            try:
                result = self._patch(f"/api/positions/{ticker}", payload)
                self._invalidate_cache()
                return result
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code not in {404, 405}:
                    raise
                merged = {
                    "ticker": existing.get("ticker", ticker),
                    "name": payload.get("name", existing.get("name")),
                    "quantity": payload.get("quantity", existing.get("quantity", 0)),
                    "cost_price": payload.get("cost_price", existing.get("cost_price", 0)),
                    "note": payload.get("note", existing.get("note")),
                }
                result = self._post("/api/positions", merged)
                self._invalidate_cache()
                return result
        result = self._post(
            "/api/positions",
            {
                "ticker": ticker,
                "name": name,
                "quantity": quantity or 0,
                "cost_price": resolved_cost or 0,
                "note": note,
            },
        )
        self._invalidate_cache()
        return result

    def _find_position(self, ticker: str) -> Any | None:
        normalized = ticker.upper()
        for item in self.get_positions():
            if str(item.get("ticker", "")).upper() == normalized:
                return item
        return None

    def get_data_quality(self) -> Any:
        return self._get("/api/data-quality")

    def get_refresh_logs(self, ticker: str | None = None, limit: int = 100) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/api/refresh-logs", **params)

    def refresh_stock_snapshot(self, ticker: str, timeout: int = 20) -> Any:
        try:
            return self._post(f"/api/market/refresh/snapshot/{ticker}", {}, timeout=timeout)
        finally:
            self._invalidate_cache()

    def refresh_stock_daily(
        self,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        timeout: int = 20,
    ) -> Any:
        try:
            return self._post(
                "/api/market/refresh/daily",
                {"ticker": ticker, "start_date": start_date, "end_date": end_date},
                timeout=timeout,
            )
        finally:
            self._invalidate_cache()

    def refresh_stock_news(self, ticker: str, limit: int = 20) -> Any:
        try:
            return self._post(f"/api/news/refresh/{ticker}?limit={limit}", {})
        finally:
            self._invalidate_cache()

    def get_market_news_snapshot(self, limit: int = 6, timeout: int = 8, use_cache: bool = True) -> Any:
        return self._unwrap_cached_payload(
            self._get_with_retry(
                "/api/news/market-snapshot",
                timeout=timeout,
                use_cache=use_cache,
                retries=1,
                limit=limit,
            )
        )

    def refresh_market_news(self, limit: int = 6, timeout: int = 45, force: bool = True) -> Any:
        try:
            return self._post(f"/api/news/market-refresh?force={str(force).lower()}&limit={limit}", {}, timeout=timeout)
        finally:
            self._invalidate_cache()

    def refresh_stock_fundamentals(self, ticker: str) -> Any:
        try:
            return self._post(f"/api/fundamentals/refresh/{ticker}", {})
        finally:
            self._invalidate_cache()

    def refresh_market_context(self) -> Any:
        try:
            return self._post("/api/market-context/refresh", {}, timeout=120)
        finally:
            self._invalidate_cache()

    def refresh_market_indices(self, timeout: int = 30) -> Any:
        try:
            return self._post("/api/market/refresh/indices", {}, timeout=timeout)
        finally:
            self._invalidate_cache()

    def refresh_market_breadth(self, timeout: int = 60) -> Any:
        try:
            return self._post("/api/market/refresh/breadth", {}, timeout=timeout)
        finally:
            self._invalidate_cache()

    def refresh_instrument_index(self, limit: int | None = None) -> Any:
        payload: dict[str, Any] = {}
        if limit is not None:
            payload["limit"] = limit
        try:
            return self._post("/api/instruments/refresh", payload)
        finally:
            self._invalidate_cache()

    def refresh_market_package(self, ticker: str, trade_date: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if trade_date:
            params["trade_date"] = trade_date
        try:
            return self._post(f"/api/market/package/refresh/{ticker}", params)
        finally:
            self._invalidate_cache()

    def refresh_stock_all(
        self,
        ticker: str,
        include_daily: bool = True,
        include_news: bool = True,
        include_fundamentals: bool = True,
        news_limit: int = 20,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"ticker": ticker, "steps": {}, "errors": []}
        for key, fn in [
            ("snapshot", lambda: self.refresh_stock_snapshot(ticker)),
            ("daily", lambda: self.refresh_stock_daily(ticker) if include_daily else {"skipped": True}),
            ("news", lambda: self.refresh_stock_news(ticker, news_limit) if include_news else {"skipped": True}),
            ("fundamentals", lambda: self.refresh_stock_fundamentals(ticker) if include_fundamentals else {"skipped": True}),
        ]:
            try:
                result["steps"][key] = fn()
            except Exception as exc:
                result["errors"].append(f"{key}: {exc}")
        if len(result["errors"]) == 4:
            raise RuntimeError("; ".join(result["errors"]))
        return result

    def _stock_package_overview(self, package: dict[str, Any]) -> dict[str, Any]:
        sections = []
        for name in sorted(STOCK_PACKAGE_SECTIONS):
            value = self._stock_package_section_value(package, name)
            sections.append(
                {
                    "section": name,
                    "item_count": self._item_count(value),
                    "char_count": self._char_count(value),
                }
            )

        snapshot = package.get("snapshot") or {}
        instrument = package.get("instrument") or {}
        profile = package.get("profile") or {}
        return {
            "ticker": package.get("ticker"),
            "mode": "overview",
            "time_context": self._stock_package_time_context(package),
            "instrument": self._pick(
                instrument,
                ["ticker", "symbol", "name", "exchange", "market", "industry", "list_date"],
            ),
            "snapshot": self._pick(
                snapshot,
                [
                    "name",
                    "price",
                    "change_pct",
                    "change_amount",
                    "open",
                    "high",
                    "low",
                    "prev_close",
                    "volume",
                    "amount",
                    "source",
                    "updated_at",
                ],
            ),
            "profile": self._pick(profile, ["name", "industry", "area", "market", "list_date", "source", "updated_at"]),
            "position": package.get("position"),
            "daily_recent": self._tail_list(package.get("daily") or [], STOCK_PACKAGE_DEFAULT_DAILY),
            "valuation_recent": self._tail_list(package.get("valuation_daily") or [], STOCK_PACKAGE_DEFAULT_DAILY),
            "moneyflow_recent": self._tail_list(package.get("moneyflow_daily") or [], STOCK_PACKAGE_DEFAULT_DAILY),
            "limit_prices_recent": self._tail_list(package.get("limit_prices") or [], STOCK_PACKAGE_DEFAULT_DAILY),
            "news_recent": self._head_list(package.get("news") or [], STOCK_PACKAGE_DEFAULT_NEWS),
            "financials_recent": {
                "metrics": self._head_list(package.get("metrics") or [], 3),
                "statements": self._head_list(package.get("statements") or [], 3),
            },
            "data_freshness": package.get("data_freshness") or {},
            "data_availability": self._availability_summary(package.get("data_availability")),
            "daily_plan": package.get("daily_plan"),
            "quality": package.get("quality"),
            "daily_meta": package.get("daily_meta"),
            "readable_sections": sections,
            "note": "默认只返回核心摘要。需要详细内容时用 mode=section + section，并按 read_window.has_more/next_offset 续读。",
        }

    def _snapshot_time_context(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        updated_at = snapshot.get("updated_at")
        age_seconds = self._age_seconds(updated_at)
        is_today = self._is_today_iso(updated_at)
        return {
            "snapshot_updated_at": updated_at,
            "snapshot_source": snapshot.get("source"),
            "age_seconds": age_seconds,
            "freshness_status": "fresh_today" if is_today else "stale_or_unknown",
            "is_stale": not is_today,
            "data_semantics": "snapshot_updated_at 是 DataHub 行情快照写入/更新时间；价格、涨跌幅等字段应按该时间理解。",
        }

    def _summarize_refresh_result(self, refresh_result: Any) -> dict[str, Any] | None:
        if not isinstance(refresh_result, dict):
            return None
        snapshot = refresh_result.get("snapshot") if isinstance(refresh_result.get("snapshot"), dict) else {}
        summary = {
            "status": refresh_result.get("status") or refresh_result.get("message") or "ok",
            "ticker": refresh_result.get("ticker") or snapshot.get("ticker"),
            "updated_at": refresh_result.get("updated_at") or snapshot.get("updated_at"),
            "source": refresh_result.get("source") or snapshot.get("source"),
        }
        return {key: value for key, value in summary.items() if value is not None}

    @staticmethod
    def _age_seconds(value: Any) -> int | None:
        try:
            dt = datetime.fromisoformat(str(value))
            return max(0, int((datetime.utcnow() - dt.replace(tzinfo=None)).total_seconds()))
        except Exception:
            return None

    @staticmethod
    def _is_today_iso(value: Any) -> bool:
        try:
            return datetime.fromisoformat(str(value)).date() == datetime.utcnow().date()
        except Exception:
            return False

    def _stock_package_time_context(self, package: dict[str, Any], section: str | None = None) -> dict[str, Any]:
        snapshot = package.get("snapshot") or {}
        profile = package.get("profile") or {}
        daily_meta = package.get("daily_meta") or {}
        daily_meta_qfq = package.get("daily_meta_qfq") or {}
        daily_meta_raw = package.get("daily_meta_raw") or {}
        news = package.get("news") or []
        metrics = package.get("metrics") or []
        statements = package.get("statements") or []
        valuation = package.get("valuation_daily") or []
        moneyflow = package.get("moneyflow_daily") or []
        limit_prices = package.get("limit_prices") or []
        daily_plan = package.get("daily_plan") or {}
        trigger_events = package.get("trigger_events") or []

        context = {
            "section": section or "overview",
            "snapshot_updated_at": snapshot.get("updated_at"),
            "snapshot_source": snapshot.get("source"),
            "daily_latest_trade_date": daily_meta.get("latest_trade_date"),
            "daily_qfq_latest_trade_date": daily_meta_qfq.get("latest_trade_date"),
            "daily_raw_latest_trade_date": daily_meta_raw.get("latest_trade_date"),
            "profile_updated_at": profile.get("updated_at"),
            "profile_source": profile.get("source"),
            "latest_news_time": self._first_present(news, ["published_at", "fetched_at", "updated_at"]),
            "latest_financial_period": self._first_present(metrics, ["report_date", "end_date", "ann_date", "period"]),
            "latest_statement_period": self._first_present(statements, ["report_date", "end_date", "ann_date", "period"]),
            "latest_valuation_trade_date": self._first_present(valuation, ["trade_date"]),
            "latest_moneyflow_trade_date": self._first_present(moneyflow, ["trade_date"]),
            "latest_limit_price_trade_date": self._first_present(limit_prices, ["trade_date"]),
            "daily_plan_date": daily_plan.get("plan_date") if isinstance(daily_plan, dict) else None,
            "latest_trigger_event_time": self._first_present(trigger_events, ["triggered_at", "created_at", "updated_at"]),
            "data_freshness": package.get("data_freshness") or {},
            "data_semantics": (
                "所有内容均来自 DataHub 当前快照包；updated_at/fetched_at 表示入库或刷新时间，"
                "trade_date/end_date/report_date/published_at 表示源数据对应日期。若 ensure=true，本工具会对缺失 section 做按需补齐并返回 refresh_report。"
            ),
        }
        return {key: value for key, value in context.items() if value is not None}

    def _stock_package_section_value(self, package: dict[str, Any], section: str) -> Any:
        if section == "daily":
            return {
                "daily": package.get("daily") or [],
                "daily_meta": package.get("daily_meta"),
            }
        if section == "indicators":
            return package.get("indicators") or []
        if section == "news":
            return {
                "items": package.get("news") or [],
                "meta": package.get("news_meta") or {},
            }
        if section == "financials":
            return {
                "metrics": package.get("metrics") or [],
                "statements": package.get("statements") or [],
            }
        if section == "valuation":
            return package.get("valuation_daily") or []
        if section == "moneyflow":
            return package.get("moneyflow_daily") or []
        if section == "limits":
            return package.get("limit_prices") or []
        if section == "freshness":
            return package.get("data_freshness") or {}
        if section == "availability":
            return package.get("data_availability") or []
        if section == "profile":
            return {
                "instrument": package.get("instrument"),
                "profile": package.get("profile"),
            }
        if section == "events":
            return {
                "daily_plan": package.get("daily_plan"),
                "trigger_events": package.get("trigger_events") or [],
            }
        if section == "quality":
            return {
                "quality": package.get("quality"),
                "daily_meta": package.get("daily_meta"),
                "daily_meta_qfq": package.get("daily_meta_qfq"),
                "daily_meta_raw": package.get("daily_meta_raw"),
            }
        if section == "position":
            return package.get("position")
        return None

    def _page_value(self, value: Any, offset: int = 0, limit: int = 0, max_chars: int = STOCK_PACKAGE_SECTION_CHAR_BUDGET) -> dict[str, Any]:
        offset = max(0, int(offset or 0))
        limit = max(0, int(limit or 0))
        max_chars = max(1, min(int(max_chars or STOCK_PACKAGE_SECTION_CHAR_BUDGET), STOCK_PACKAGE_MAX_CHAR_BUDGET))
        if isinstance(value, list):
            rows = value[offset:(offset + limit) if limit else None]
            text = json.dumps(rows, ensure_ascii=False, default=str)
            while rows and len(text) > max_chars:
                rows = rows[:-1]
                text = json.dumps(rows, ensure_ascii=False, default=str)
            next_offset = offset + len(rows)
            return {
                "content": rows,
                "offset": offset,
                "limit": limit,
                "max_chars": max_chars,
                "total_items": len(value),
                "total_chars": self._char_count(value),
                "returned_items": len(rows),
                "has_more": next_offset < len(value),
                "next_offset": next_offset if next_offset < len(value) else None,
            }
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        end = min(offset + max_chars, len(text))
        return {
            "content": text[offset:end],
            "offset": offset,
            "limit": limit,
            "max_chars": max_chars,
            "total_items": self._item_count(value),
            "total_chars": len(text),
            "has_more": end < len(text),
            "next_offset": end if end < len(text) else None,
        }

    @staticmethod
    def _pick(data: Any, keys: list[str]) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        return {key: data.get(key) for key in keys if key in data}

    @staticmethod
    def _financials_coverage(package: dict[str, Any]) -> dict[str, Any]:
        metric_rows = len(package.get("metrics") or [])
        statement_rows = len(package.get("statements") or [])
        if metric_rows and statement_rows:
            status = "available"
        elif metric_rows or statement_rows:
            status = "partial"
        else:
            status = "missing"
        return {"status": status, "metric_rows": metric_rows, "statement_rows": statement_rows}

    @staticmethod
    def _availability_summary(value: Any) -> dict[str, Any]:
        if not isinstance(value, list):
            return {"rows": 0, "ready": [], "empty": [], "failed": []}
        summary: dict[str, Any] = {"rows": len(value), "ready": [], "empty": [], "failed": []}
        for row in value:
            if not isinstance(row, dict):
                continue
            dataset = str(row.get("dataset") or "").strip()
            if not dataset:
                continue
            status = str(row.get("status") or "").strip().lower()
            item = {
                "dataset": dataset,
                "provider": row.get("provider"),
                "status": row.get("status"),
                "as_of": row.get("as_of"),
                "row_count": row.get("row_count"),
                "missing_reason": row.get("missing_reason"),
            }
            if status == "ready":
                summary["ready"].append(item)
            elif status in {"empty", "missing"}:
                summary["empty"].append(item)
            elif status == "failed":
                summary["failed"].append(item)
        for key in ("ready", "empty", "failed"):
            summary[key] = summary[key][:8]
        return summary

    @staticmethod
    def _head_list(value: Any, count: int) -> list[Any]:
        return list(value[:count]) if isinstance(value, list) else []

    @staticmethod
    def _first_present(rows: Any, keys: list[str]) -> Any | None:
        if isinstance(rows, dict):
            for key in keys:
                value = rows.get(key)
                if value not in (None, ""):
                    return value
            return None
        if not isinstance(rows, list):
            return None
        values: list[Any] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    values.append(value)
                    break
        if not values:
            return None
        return max(values, key=lambda item: str(item))

    @staticmethod
    def _tail_list(value: Any, count: int) -> list[Any]:
        return list(value[-count:]) if isinstance(value, list) else []

    @staticmethod
    def _char_count(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _item_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
        if value in (None, ""):
            return 0
        return 1


datahub_client = DataHubClient()
