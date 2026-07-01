from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from backend.core.config import DATA_DIR


DB_PATH = DATA_DIR / "resource_limits.sqlite"


class ResourceLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResourceGuard:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def check_and_record_tool(self, session_id: str, run_id: str | None, tool_name: str, group: str) -> None:
        now = datetime.now()
        if run_id:
            self._enforce_window(
                scope=f"run:{run_id}",
                key="tool_calls",
                limit=6,
                window_start=now - timedelta(hours=12),
                message="单轮工具调用次数已达上限",
            )
        if group == "datahub.refresh":
            self._enforce_window(
                scope=f"session:{session_id}",
                key="external_refresh",
                limit=20,
                window_start=now - timedelta(hours=1),
                message="当前会话每小时外部数据刷新次数已达上限",
            )
        if tool_name == "run_market_discovery":
            self._enforce_window(
                scope=f"session:{session_id}",
                key="market_discovery_daily",
                limit=3,
                window_start=now - timedelta(days=1),
                message="当前会话每日主线雷达研究次数已达上限",
            )
        self._record(f"run:{run_id}" if run_id else f"session:{session_id}", "tool_calls", tool_name)
        if group == "datahub.refresh":
            self._record(f"session:{session_id}", "external_refresh", tool_name)
        if tool_name == "run_market_discovery":
            self._record(f"session:{session_id}", "market_discovery_daily", tool_name)

    def check_and_record_stock_research_start(self, session_id: str) -> None:
        now = datetime.now()
        self._enforce_window(
            scope=f"session:{session_id}",
            key="stock_research_daily",
            limit=5,
            window_start=now - timedelta(days=1),
            message="当前会话每日个股深研次数已达上限",
        )
        self._record(f"session:{session_id}", "stock_research_daily", "run_stock_research")

    def _enforce_window(self, scope: str, key: str, limit: int, window_start: datetime, message: str) -> None:
        with self._lock, self._connect() as conn:
            count = conn.execute(
                """
                select count(*) as c from resource_events
                where scope=? and event_key=? and created_at>=?
                """,
                (scope, key, window_start.isoformat(timespec="seconds")),
            ).fetchone()["c"]
        if int(count) >= limit:
            raise ResourceLimitExceeded("rate_limited", message)

    def _record(self, scope: str, key: str, detail: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into resource_events(scope, event_key, detail, created_at)
                values (?, ?, ?, ?)
                """,
                (scope, key, detail, datetime.now().isoformat(timespec="seconds")),
            )

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists resource_events (
                    id integer primary key autoincrement,
                    scope text not null,
                    event_key text not null,
                    detail text,
                    created_at text not null
                );
                create index if not exists idx_resource_scope_key_time on resource_events(scope, event_key, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


resource_guard = ResourceGuard()
