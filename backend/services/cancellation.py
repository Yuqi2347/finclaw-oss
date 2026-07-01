from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from backend.core.config import DATA_DIR


DB_PATH = DATA_DIR / "cancellations.sqlite"


class RunCancelled(RuntimeError):
    pass


class CancellationStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def request_cancel(self, session_id: str, run_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into cancellations(session_id, run_id, requested_at)
                values (?, ?, ?)
                """,
                (session_id, run_id, datetime.now().isoformat(timespec="seconds")),
            )

    def is_cancelled(self, session_id: str, run_id: str | None = None) -> bool:
        with self._lock, self._connect() as conn:
            if run_id:
                row = conn.execute(
                    "select 1 from cancellations where session_id=? and (run_id=? or run_id is null) limit 1",
                    (session_id, run_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "select 1 from cancellations where session_id=? and run_id is null limit 1",
                    (session_id,),
                ).fetchone()
        return row is not None

    def raise_if_cancelled(self, session_id: str, run_id: str | None = None) -> None:
        if self.is_cancelled(session_id, run_id):
            raise RunCancelled("用户已取消当前运行")

    def clear(self, session_id: str, run_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if run_id:
                conn.execute("delete from cancellations where session_id=? and run_id=?", (session_id, run_id))
            else:
                conn.execute("delete from cancellations where session_id=?", (session_id,))

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists cancellations (
                    id integer primary key autoincrement,
                    session_id text not null,
                    run_id text,
                    requested_at text not null
                );
                create index if not exists idx_cancellations_session_run on cancellations(session_id, run_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


cancellation_store = CancellationStore()
