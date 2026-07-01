from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from backend.core.config import DATA_DIR


DB_PATH = DATA_DIR / "observability.sqlite"


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def summarize(value: Any, max_chars: int = 4000) -> Any:
    text = _json(value)
    if len(text) <= max_chars:
        return value
    return {
        "summary": text[:max_chars],
        "truncated": True,
        "original_chars": len(text),
    }


def estimate_tokens(value: Any) -> int:
    text = _json(value)
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]", text))
    # OpenAI-compatible exact tokenizers are model-specific; this is a local debug estimate.
    return max(1, int(cjk_chars * 1.15 + ascii_words * 0.75))


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    name: str
    started_at: float


class TraceStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def start_trace(self, session_id: str, run_id: str, trigger_type: str, input_summary: dict[str, Any]) -> str:
        trace_id = str(uuid4())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into traces(trace_id, session_id, run_id, trigger_type, status, input_json, started_at)
                values (?, ?, ?, ?, 'running', ?, ?)
                """,
                (trace_id, session_id, run_id, trigger_type, _json(summarize(input_summary)), _now()),
            )
        return trace_id

    def finish_trace(self, trace_id: str, status: str = "completed", error: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "update traces set status=?, error=?, completed_at=? where trace_id=?",
                (status, error, _now(), trace_id),
            )

    @contextmanager
    def span(
        self,
        trace_id: str,
        name: str,
        span_type: str,
        parent_span_id: str | None = None,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[SpanContext]:
        span_id = str(uuid4())
        started = time.perf_counter()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into spans(trace_id, span_id, parent_span_id, name, span_type, status, input_json, metadata_json, started_at)
                values (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    trace_id,
                    span_id,
                    parent_span_id,
                    name,
                    span_type,
                    _json(summarize(input)) if input is not None else None,
                    _json(metadata or {}),
                    _now(),
                ),
            )
        context = SpanContext(trace_id=trace_id, span_id=span_id, name=name, started_at=started)
        try:
            yield context
        except Exception as exc:
            self.finish_span(context, status="failed", error=str(exc))
            raise
        else:
            self.finish_span(context)

    def finish_span(
        self,
        context: SpanContext,
        status: str = "completed",
        output: Any | None = None,
        error: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = int((time.perf_counter() - context.started_at) * 1000)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update spans
                set status=?, output_json=?, error=?, metrics_json=?, duration_ms=?, completed_at=?
                where span_id=?
                """,
                (
                    status,
                    _json(summarize(output)) if output is not None else None,
                    error,
                    _json(metrics or {}),
                    duration_ms,
                    _now(),
                    context.span_id,
                ),
            )

    def event(
        self,
        trace_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        span_id: str | None = None,
        level: str = "info",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into trace_events(trace_id, span_id, event_type, level, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (trace_id, span_id, event_type, level, _json(summarize(payload or {})), _now()),
            )

    def record_llm_call(
        self,
        *,
        trace_id: str | None,
        session_id: str | None,
        run_id: str | None,
        model: str,
        base_url: str,
        tool_choice: str | dict[str, Any],
        temperature: float,
        request: dict[str, Any],
        response: dict[str, Any] | None = None,
        status: str = "completed",
        error: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        first_token_ms: int | None = None,
    ) -> int:
        completed = completed_at or _now()
        request_tokens_estimate = estimate_tokens(request)
        response_tokens_estimate = estimate_tokens(response or {})
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                insert into llm_call_logs(
                    trace_id, session_id, run_id, model, base_url, tool_choice_json,
                    temperature, request_json, response_json, status, error,
                    started_at, completed_at, duration_ms, first_token_ms,
                    request_tokens_estimate, response_tokens_estimate, total_tokens_estimate
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    run_id,
                    model,
                    base_url,
                    _json(tool_choice),
                    temperature,
                    _json(request),
                    _json(response or {}),
                    status,
                    error,
                    started_at or completed,
                    completed,
                    duration_ms,
                    first_token_ms,
                    request_tokens_estimate,
                    response_tokens_estimate,
                    request_tokens_estimate + response_tokens_estimate,
                ),
            )
            self._trim_llm_logs(conn, keep=500)
            return int(cursor.lastrowid)

    def recent_llm_logs(self, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 500))
        with self._lock, self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    """
                    select id, trace_id, session_id, run_id, model, base_url, tool_choice_json,
                           temperature, status, error, started_at, completed_at, duration_ms, first_token_ms,
                           request_tokens_estimate, response_tokens_estimate, total_tokens_estimate,
                           length(request_json) as request_chars,
                           length(response_json) as response_chars
                    from llm_call_logs
                    where session_id=?
                    order by id desc
                    limit ?
                    """,
                    (session_id, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select id, trace_id, session_id, run_id, model, base_url, tool_choice_json,
                           temperature, status, error, started_at, completed_at, duration_ms, first_token_ms,
                           request_tokens_estimate, response_tokens_estimate, total_tokens_estimate,
                           length(request_json) as request_chars,
                           length(response_json) as response_chars
                    from llm_call_logs
                    order by id desc
                    limit ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [self._decode_llm_log_row(row, summary=True) for row in rows]

    def get_llm_log(self, log_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from llm_call_logs where id=?", (log_id,)).fetchone()
        return self._decode_llm_log_row(row, summary=False) if row else None

    def clear_llm_logs(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            deleted = conn.execute("delete from llm_call_logs").rowcount
        return {"deleted": int(deleted or 0)}

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select trace_id, session_id, run_id, trigger_type, status, error, started_at, completed_at
                from traces
                order by started_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            trace = conn.execute("select * from traces where trace_id=?", (trace_id,)).fetchone()
            if not trace:
                return None
            spans = conn.execute(
                "select * from spans where trace_id=? order by started_at asc",
                (trace_id,),
            ).fetchall()
            events = conn.execute(
                "select * from trace_events where trace_id=? order by created_at asc",
                (trace_id,),
            ).fetchall()
        return {
            "trace": self._decode_row(trace),
            "spans": [self._decode_row(row) for row in spans],
            "events": [self._decode_row(row) for row in events],
        }

    def metrics_summary(self, limit: int = 500) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select span_type, status, duration_ms
                from spans
                order by started_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        summary: dict[str, Any] = {"count": len(rows), "by_type": {}}
        for row in rows:
            key = row["span_type"]
            bucket = summary["by_type"].setdefault(key, {"count": 0, "failed": 0, "avg_duration_ms": 0, "_durations": []})
            bucket["count"] += 1
            if row["status"] == "failed":
                bucket["failed"] += 1
            if row["duration_ms"] is not None:
                bucket["_durations"].append(int(row["duration_ms"]))
        for bucket in summary["by_type"].values():
            durations = bucket.pop("_durations")
            bucket["avg_duration_ms"] = round(sum(durations) / len(durations), 1) if durations else 0
        return summary

    def delete_by_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            trace_ids = [row["trace_id"] for row in conn.execute(
                "select trace_id from traces where session_id=?",
                (session_id,),
            ).fetchall()]
            if trace_ids:
                conn.executemany("delete from trace_events where trace_id=?", ((trace_id,) for trace_id in trace_ids))
                conn.executemany("delete from spans where trace_id=?", ((trace_id,) for trace_id in trace_ids))
                conn.executemany("delete from traces where trace_id=?", ((trace_id,) for trace_id in trace_ids))
            else:
                conn.execute("delete from traces where session_id=?", (session_id,))

    def _decode_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("input_json", "output_json", "metadata_json", "metrics_json", "payload_json"):
            if key in data and data[key]:
                try:
                    data[key[:-5] if key.endswith("_json") else key] = json.loads(data[key])
                except Exception:
                    data[key[:-5] if key.endswith("_json") else key] = data[key]
                del data[key]
        return data

    def _decode_llm_log_row(self, row: sqlite3.Row, *, summary: bool) -> dict[str, Any]:
        data = dict(row)
        if "tool_choice_json" in data:
            try:
                data["tool_choice"] = json.loads(data.get("tool_choice_json") or "null")
            except Exception:
                data["tool_choice"] = data.get("tool_choice_json")
            del data["tool_choice_json"]
        if not summary:
            for key in ("request_json", "response_json"):
                try:
                    data[key[:-5]] = json.loads(data.get(key) or "{}")
                except Exception:
                    data[key[:-5]] = data.get(key)
                del data[key]
            if data.get("request_tokens_estimate") is None and "request" in data:
                data["request_tokens_estimate"] = estimate_tokens(data["request"])
            if data.get("response_tokens_estimate") is None and "response" in data:
                data["response_tokens_estimate"] = estimate_tokens(data["response"])
            if data.get("total_tokens_estimate") is None:
                data["total_tokens_estimate"] = int(data.get("request_tokens_estimate") or 0) + int(data.get("response_tokens_estimate") or 0)
        else:
            if data.get("request_tokens_estimate") is None:
                data["request_tokens_estimate"] = int((data.get("request_chars") or 0) / 3.2)
            if data.get("response_tokens_estimate") is None:
                data["response_tokens_estimate"] = int((data.get("response_chars") or 0) / 3.2)
            if data.get("total_tokens_estimate") is None:
                data["total_tokens_estimate"] = int(data.get("request_tokens_estimate") or 0) + int(data.get("response_tokens_estimate") or 0)
        return data

    def _trim_llm_logs(self, conn: sqlite3.Connection, keep: int = 500) -> None:
        conn.execute(
            """
            delete from llm_call_logs
            where id not in (
                select id from llm_call_logs order by id desc limit ?
            )
            """,
            (keep,),
        )

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists traces (
                    trace_id text primary key,
                    session_id text not null,
                    run_id text not null,
                    trigger_type text not null,
                    status text not null,
                    input_json text,
                    error text,
                    started_at text not null,
                    completed_at text
                );
                create table if not exists spans (
                    span_id text primary key,
                    trace_id text not null,
                    parent_span_id text,
                    name text not null,
                    span_type text not null,
                    status text not null,
                    input_json text,
                    output_json text,
                    metadata_json text,
                    metrics_json text,
                    error text,
                    duration_ms integer,
                    started_at text not null,
                    completed_at text
                );
                create table if not exists trace_events (
                    event_id integer primary key autoincrement,
                    trace_id text not null,
                    span_id text,
                    event_type text not null,
                    level text not null,
                    payload_json text not null,
                    created_at text not null
                );
                create table if not exists llm_call_logs (
                    id integer primary key autoincrement,
                    trace_id text,
                    session_id text,
                    run_id text,
                    model text not null,
                    base_url text not null,
                    tool_choice_json text,
                    temperature real,
                    request_json text not null,
                    response_json text not null,
                    status text not null,
                    error text,
                    started_at text not null,
                    completed_at text,
                    duration_ms integer,
                    first_token_ms integer,
                    request_tokens_estimate integer,
                    response_tokens_estimate integer,
                    total_tokens_estimate integer
                );
                create index if not exists idx_traces_started on traces(started_at);
                create index if not exists idx_spans_trace on spans(trace_id);
                create index if not exists idx_events_trace on trace_events(trace_id);
                create index if not exists idx_llm_logs_started on llm_call_logs(started_at);
                create index if not exists idx_llm_logs_session on llm_call_logs(session_id);
                """
            )
            self._ensure_column(conn, "llm_call_logs", "request_tokens_estimate", "integer")
            self._ensure_column(conn, "llm_call_logs", "response_tokens_estimate", "integer")
            self._ensure_column(conn, "llm_call_logs", "total_tokens_estimate", "integer")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {column_type}")


trace_store = TraceStore()
