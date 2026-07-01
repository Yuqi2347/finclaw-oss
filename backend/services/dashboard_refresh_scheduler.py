from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from backend.tools.datahub import datahub_client

logger = logging.getLogger(__name__)

_A_SHARE_TICKER_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_SH_TZ = ZoneInfo("Asia/Shanghai")


def _normalize_dashboard_ticker(value: object) -> str | None:
    ticker = str(value or "").strip().upper()
    if not ticker:
        return None
    if _A_SHARE_TICKER_RE.fullmatch(ticker):
        if "." in ticker:
            return ticker
        if ticker.startswith("6"):
            return f"{ticker}.SH"
        if ticker.startswith(("0", "3")):
            return f"{ticker}.SZ"
        if ticker.startswith(("4", "8")):
            return f"{ticker}.BJ"
    return None


class DashboardRefreshScheduler:
    def __init__(self, loop_interval_seconds: int = 15, initial_delay_seconds: int = 3) -> None:
        self.loop_interval_seconds = loop_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self._lock = threading.RLock()
        self._scheduler_started = False
        self._scheduler_thread: threading.Thread | None = None
        self._running_jobs: set[str] = set()
        self._last_buckets: dict[str, str] = {}

    def start_background_scheduler(self) -> None:
        with self._lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="dashboard-refresh-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()
            logger.info("Dashboard refresh scheduler started")

    def _scheduler_loop(self) -> None:
        if self.initial_delay_seconds > 0:
            time.sleep(self.initial_delay_seconds)
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("Dashboard refresh scheduler loop failed")
            time.sleep(self.loop_interval_seconds)

    def _tick(self) -> None:
        now = datetime.now(_SH_TZ)

        if self._is_trading_session(now):
            self._schedule_bucket_job(
                name="realtime_group",
                bucket=now.strftime("%Y-%m-%d %H:%M"),
                fn=self._run_realtime_group,
            )

        if self._should_run_daily_batch(now):
            self._schedule_bucket_job(
                name="daily_batch",
                bucket=now.strftime("%Y-%m-%d"),
                fn=self._run_daily_batch,
            )

    def _schedule_bucket_job(self, name: str, bucket: str, fn) -> None:
        with self._lock:
            if name in self._running_jobs:
                return
            if self._last_buckets.get(name) == bucket:
                return
            self._last_buckets[name] = bucket
            self._running_jobs.add(name)

        worker = threading.Thread(
            target=self._run_job,
            args=(name, bucket, fn),
            name=f"dashboard-refresh-{name}",
            daemon=True,
        )
        worker.start()

    def _run_job(self, name: str, bucket: str, fn) -> None:
        try:
            logger.info("Dashboard scheduler job started: %s bucket=%s", name, bucket)
            fn()
            logger.info("Dashboard scheduler job completed: %s bucket=%s", name, bucket)
        except Exception:
            logger.exception("Dashboard scheduler job failed: %s bucket=%s", name, bucket)
        finally:
            with self._lock:
                self._running_jobs.discard(name)

    def _run_realtime_group(self) -> None:
        tickers = self._collect_targets()
        datahub_client.refresh_market_indices(timeout=15)
        if not tickers:
            return

        with ThreadPoolExecutor(max_workers=min(3, len(tickers))) as executor:
            future_map = {
                executor.submit(datahub_client.refresh_stock_snapshot, ticker, 12): ticker
                for ticker in tickers
            }
            for future in as_completed(future_map):
                ticker = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Realtime snapshot refresh failed: %s error=%s", ticker, exc)

    def _run_daily_batch(self) -> None:
        tickers = self._collect_targets()
        if not tickers:
            logger.info("Daily batch refresh skipped: no positions/watchlist tickers")
            return

        start_date = (datetime.now(_SH_TZ).date() - timedelta(days=90)).isoformat()
        for ticker in tickers:
            try:
                datahub_client.refresh_stock_daily(ticker, start_date=start_date, timeout=45)
            except Exception as exc:
                logger.warning("Daily batch refresh failed: %s error=%s", ticker, exc)
            try:
                datahub_client.refresh_stock_news(ticker, limit=20)
            except Exception as exc:
                logger.warning("Daily news refresh failed: %s error=%s", ticker, exc)
            try:
                datahub_client.refresh_stock_fundamentals(ticker)
            except Exception as exc:
                logger.warning("Daily fundamentals refresh failed: %s error=%s", ticker, exc)

    def _collect_targets(self) -> list[str]:
        tickers: set[str] = set()

        try:
            positions = datahub_client.get_positions(timeout=8, use_cache=False)
        except Exception as exc:
            positions = []
            logger.warning("Failed to load positions for dashboard scheduler: %s", exc)

        try:
            watchlist = datahub_client.get_watchlist(timeout=8, use_cache=False)
        except Exception as exc:
            watchlist = []
            logger.warning("Failed to load watchlist for dashboard scheduler: %s", exc)

        for rows in (positions, watchlist):
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                ticker = _normalize_dashboard_ticker(item.get("ticker"))
                if ticker:
                    tickers.add(ticker)
        return sorted(tickers)

    def _is_trading_session(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        current = now.time()
        in_morning = dt_time(9, 30) <= current < dt_time(11, 30)
        in_afternoon = dt_time(13, 0) <= current < dt_time(15, 0)
        return in_morning or in_afternoon

    def _should_run_daily_batch(self, now: datetime) -> bool:
        return now.weekday() < 5 and now.time() >= dt_time(15, 30)


dashboard_refresh_scheduler = DashboardRefreshScheduler()
