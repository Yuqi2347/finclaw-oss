from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.core.env import settings
from backend.core.models import ApprovalPolicy, SessionSecuritySettings


DB_PATH = DATA_DIR / "sessions.sqlite"
RUNTIME_REQUIRED_TOOL_GROUPS = ("memory", "industry_graph.read", "industry_graph.run", "web.read", "skill")


def _ensure_runtime_tool_groups(groups: list[str] | None) -> list[str]:
    if isinstance(groups, list):
        merged = [str(group) for group in groups]
    else:
        merged = list(SessionSecuritySettings().allowed_tool_groups)
    for group in RUNTIME_REQUIRED_TOOL_GROUPS:
        if group not in merged:
            merged.append(group)
    return merged


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    session_id: str
    role: str
    content: str
    reasoning_content: str
    tool_calls: list[dict[str, Any]]
    report_links: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    created_at: str


class ChatSessionStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        report_links: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> int:
        now = _now()
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            cursor = conn.execute(
                """
                insert into chat_messages(
                    session_id,
                    role,
                    content,
                    reasoning_content,
                    tool_calls_json,
                    report_links_json,
                    sources_json,
                    citations_json,
                    attachments_json,
                    created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    reasoning_content or "",
                    json.dumps(tool_calls or [], ensure_ascii=False, default=str),
                    json.dumps(report_links or [], ensure_ascii=False, default=str),
                    json.dumps(sources or [], ensure_ascii=False, default=str),
                    json.dumps(citations or [], ensure_ascii=False, default=str),
                    json.dumps(attachments or [], ensure_ascii=False, default=str),
                    now,
                ),
            )
            conn.execute("update chat_sessions set updated_at=? where session_id=?", (now, session_id))
            if role == "user":
                row = conn.execute(
                    "select title from chat_sessions where session_id=?",
                    (session_id,),
                ).fetchone()
                title = str(row["title"] or "") if row else ""
                if not title or title == session_id or title.startswith("新会话"):
                    new_title = _derive_session_title(content)
                    conn.execute(
                        "update chat_sessions set title=?, updated_at=? where session_id=?",
                        (new_title, now, session_id),
                    )
            return int(cursor.lastrowid)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select
                    s.session_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.active_run_id,
                    s.active_approval_action_id,
                    coalesce(counts.message_count, 0) as message_count,
                    last.role as last_role,
                    last.content as last_content,
                    last.created_at as last_message_at
                from chat_sessions s
                left join (
                    select session_id, count(*) as message_count, max(message_id) as last_message_id
                    from chat_messages
                    group by session_id
                ) counts on counts.session_id = s.session_id
                left join chat_messages last on last.message_id = counts.last_message_id
                order by s.updated_at desc, s.created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_row_to_dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                select
                    s.session_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.active_run_id,
                    s.active_approval_action_id,
                    coalesce(counts.message_count, 0) as message_count,
                    last.role as last_role,
                    last.content as last_content,
                    last.created_at as last_message_at
                from chat_sessions s
                left join (
                    select session_id, count(*) as message_count, max(message_id) as last_message_id
                    from chat_messages
                    group by session_id
                ) counts on counts.session_id = s.session_id
                left join chat_messages last on last.message_id = counts.last_message_id
                where s.session_id=?
                limit 1
                """,
                (session_id,),
            ).fetchone()
        return self._session_row_to_dict(row) if row else None

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        session_id = f"session-{uuid4().hex[:12]}"
        safe_title = (title or "").strip() or f"新会话 {datetime.now().strftime('%m-%d %H:%M')}"
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into chat_sessions(session_id, title, created_at, updated_at)
                values (?, ?, ?, ?)
                """,
                (session_id, safe_title, now, now),
            )
        return self.get_session(session_id) or {
            "session_id": session_id,
            "title": safe_title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
            "last_role": None,
            "last_content": None,
            "last_message_at": None,
            "active_run_id": None,
            "active_approval_action_id": None,
        }

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        safe_title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not safe_title:
            raise ValueError("会话名称不能为空")
        if len(safe_title) > 60:
            safe_title = safe_title[:60].rstrip()
        now = _now()
        with self._lock, self._connect() as conn:
            self._ensure_session_exists(conn, session_id)
            conn.execute(
                "update chat_sessions set title=?, updated_at=? where session_id=?",
                (safe_title, now, session_id),
            )
        return self.get_session(session_id) or {
            "session_id": session_id,
            "title": safe_title,
            "updated_at": now,
        }

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            self._ensure_session_exists(conn, session_id)
            row = conn.execute(
                "select active_run_id from chat_sessions where session_id=?",
                (session_id,),
            ).fetchone()
            if row and row["active_run_id"] and self._is_blocking_active_run(conn, row["active_run_id"]):
                raise ValueError("会话正在运行后台任务，请先等待结束或取消任务后再删除")
            run_rows = conn.execute("select run_id from agent_runs where session_id=?", (session_id,)).fetchall()
            run_ids = [str(item["run_id"]) for item in run_rows if item["run_id"]]
            conn.execute("delete from chat_messages where session_id=?", (session_id,))
            conn.execute("delete from session_events where session_id=?", (session_id,))
            conn.execute("delete from agent_runs where session_id=?", (session_id,))
            conn.execute("delete from chat_sessions where session_id=?", (session_id,))
        return {"session_id": session_id, "run_ids": run_ids}

    def list_messages(self, session_id: str, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, session_id, role, content, reasoning_content, tool_calls_json, report_links_json, sources_json, citations_json, attachments_json, created_at
                from chat_messages
                where session_id=? and message_id>?
                order by message_id asc
                limit ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [self._row_to_message(row).__dict__ for row in rows]

    def get_messages(self, session_id: str, after_id: int = 0, limit: int = 100) -> list[StoredMessage]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, session_id, role, content, reasoning_content, tool_calls_json, report_links_json, sources_json, citations_json, attachments_json, created_at
                from chat_messages
                where session_id=? and message_id>?
                order by message_id asc
                limit ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def recent_context(self, session_id: str, limit: int = 16) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, role, content, reasoning_content, tool_calls_json
                from chat_messages
                where session_id=?
                order by message_id desc
                limit ?
                """,
                (session_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            role = row["role"]
            content = row["content"]
            tool_calls = json.loads(row["tool_calls_json"] or "[]")

            if role == "assistant" and tool_calls:
                openai_tool_calls = []
                tool_result_messages = []
                for i, tc in enumerate(tool_calls):
                    tool_name = tc.get("tool", "unknown")
                    result_data = tc.get("result", {})
                    fake_id = f"hist_{row['message_id']}_{i}"
                    openai_tool_calls.append({
                        "id": fake_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": "{}",
                        },
                    })
                    tool_result_messages.append({
                        "role": "tool",
                        "tool_call_id": fake_id,
                        "name": tool_name,
                        "content": json.dumps(result_data, ensure_ascii=False, default=str)[:2000],
                    })
                assistant_tool_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": openai_tool_calls,
                }
                reasoning_content = row["reasoning_content"] or ""
                if settings.llm_thinking_payload is not None:
                    assistant_tool_message["reasoning_content"] = reasoning_content
                result.append(assistant_tool_message)
                result.extend(tool_result_messages)
                if content:
                    item: dict[str, Any] = {"role": "assistant", "content": content}
                    if settings.llm_thinking_payload is not None:
                        item["reasoning_content"] = reasoning_content
                    result.append(item)
            else:
                item: dict[str, Any] = {"role": role, "content": content}
                if role == "assistant":
                    reasoning_content = row["reasoning_content"] or ""
                    if settings.llm_thinking_payload is not None:
                        item["reasoning_content"] = reasoning_content
                result.append(item)
        return result

    def maybe_compact(self, session_id: str, keep_last: int = 20) -> None:
        from backend.services.memory import memory_manager

        memory_manager.maybe_compact(session_id, reason="post_run")

    def get_memory_state(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                """
                select memory_json, memory_summary, memory_version, last_compacted_message_id, compact_error
                from chat_sessions where session_id=?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        data = {}
        if row["memory_json"]:
            try:
                data = json.loads(row["memory_json"])
            except Exception:
                data = {}
        return {
            "memory": data,
            "memory_version": row["memory_version"] or 0,
            "last_compacted_message_id": row["last_compacted_message_id"] or 0,
            "compact_error": row["compact_error"] or "",
        }

    def get_memory_hook_state(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                """
                select
                    last_memory_hook_message_id,
                    last_memory_hook_at,
                    memory_hook_error,
                    memory_hook_scan_message_id,
                    memory_hook_buffer_json,
                    memory_hook_buffer_count
                from chat_sessions
                where session_id=?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return {
                "last_memory_hook_message_id": 0,
                "last_memory_hook_at": "",
                "memory_hook_error": "",
                "memory_hook_scan_message_id": 0,
                "memory_hook_buffer": [],
                "memory_hook_buffer_count": 0,
            }
        buffer_items = _safe_json_any(row["memory_hook_buffer_json"] or "[]")
        if not isinstance(buffer_items, list):
            buffer_items = []
        return {
            "last_memory_hook_message_id": int(row["last_memory_hook_message_id"] or 0),
            "last_memory_hook_at": row["last_memory_hook_at"] or "",
            "memory_hook_error": row["memory_hook_error"] or "",
            "memory_hook_scan_message_id": int(row["memory_hook_scan_message_id"] or 0),
            "memory_hook_buffer": buffer_items,
            "memory_hook_buffer_count": int(row["memory_hook_buffer_count"] or 0),
        }

    def mark_memory_hook(self, session_id: str, message_id: int, error: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                """
                update chat_sessions
                set last_memory_hook_message_id=?,
                    memory_hook_scan_message_id=?,
                    last_memory_hook_at=?,
                    memory_hook_error=?,
                    updated_at=?
                where session_id=?
                """,
                (
                    int(message_id),
                    int(message_id),
                    _now(),
                    (error or "")[:2000],
                    _now(),
                    session_id,
                ),
            )

    def set_memory_hook_scan(self, session_id: str, message_id: int, error: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                """
                update chat_sessions
                set memory_hook_scan_message_id=?, memory_hook_error=?, updated_at=?
                where session_id=?
                """,
                (int(message_id), (error or "")[:2000], _now(), session_id),
            )

    def set_memory_hook_buffer(self, session_id: str, buffer_items: list[dict[str, Any]]) -> None:
        safe_items = [item for item in buffer_items if isinstance(item, dict)]
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                """
                update chat_sessions
                set memory_hook_buffer_json=?, memory_hook_buffer_count=?, updated_at=?
                where session_id=?
                """,
                (
                    json.dumps(safe_items, ensure_ascii=False, default=str),
                    len(safe_items),
                    _now(),
                    session_id,
                ),
            )

    def clear_memory_hook_buffer(self, session_id: str) -> None:
        self.set_memory_hook_buffer(session_id, [])

    def get_security_settings(self, session_id: str) -> SessionSecuritySettings:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                "select approval_policy, allowed_tool_groups_json from chat_sessions where session_id=?",
                (session_id,),
            ).fetchone()
        groups = None
        if row and row["allowed_tool_groups_json"]:
            try:
                groups = json.loads(row["allowed_tool_groups_json"])
            except Exception:
                groups = None
        return SessionSecuritySettings(
            approval_policy=ApprovalPolicy(row["approval_policy"] or ApprovalPolicy.BALANCED.value),
            allowed_tool_groups=_ensure_runtime_tool_groups(groups),
        )

    def get_approval_queue(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                """
                select active_approval_action_id, approval_queue_json
                from chat_sessions
                where session_id=?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return {"active_action_id": None, "queued_action_ids": []}
        return {
            "active_action_id": (row["active_approval_action_id"] or "").strip() or None,
            "queued_action_ids": _safe_json_list(row["approval_queue_json"]),
        }

    def enqueue_approval_action(self, session_id: str, action_id: str) -> dict[str, Any]:
        action_id = str(action_id or "").strip()
        if not action_id:
            return self.get_approval_queue(session_id)
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                """
                select active_approval_action_id, approval_queue_json
                from chat_sessions
                where session_id=?
                """,
                (session_id,),
            ).fetchone()
            active = (row["active_approval_action_id"] or "").strip() if row else ""
            queue = _safe_json_list(row["approval_queue_json"] if row else "[]")
            if active:
                if action_id != active and action_id not in queue:
                    queue.append(action_id)
            else:
                active = action_id
            conn.execute(
                """
                update chat_sessions
                set active_approval_action_id=?, approval_queue_json=?, updated_at=?
                where session_id=?
                """,
                (active, json.dumps(queue, ensure_ascii=False), _now(), session_id),
            )
        return self.get_approval_queue(session_id)

    def advance_approval_queue(self, session_id: str, action_id: str) -> dict[str, Any]:
        action_id = str(action_id or "").strip()
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            row = conn.execute(
                """
                select active_approval_action_id, approval_queue_json
                from chat_sessions
                where session_id=?
                """,
                (session_id,),
            ).fetchone()
            active = (row["active_approval_action_id"] or "").strip() if row else ""
            queue = _safe_json_list(row["approval_queue_json"] if row else "[]")
            if active == action_id:
                active = queue.pop(0) if queue else ""
            elif action_id in queue:
                queue = [item for item in queue if item != action_id]
            conn.execute(
                """
                update chat_sessions
                set active_approval_action_id=?, approval_queue_json=?, updated_at=?
                where session_id=?
                """,
                (active, json.dumps(queue, ensure_ascii=False), _now(), session_id),
            )
        return self.get_approval_queue(session_id)

    def update_security_settings(
        self,
        session_id: str,
        approval_policy: str | None = None,
        allowed_tool_groups: list[str] | None = None,
    ) -> SessionSecuritySettings:
        current = self.get_security_settings(session_id)
        policy = ApprovalPolicy(approval_policy) if approval_policy else current.approval_policy
        groups = allowed_tool_groups if allowed_tool_groups is not None else current.allowed_tool_groups
        groups = _ensure_runtime_tool_groups(groups)
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                """
                update chat_sessions
                set approval_policy=?, allowed_tool_groups_json=?, updated_at=?
                where session_id=?
                """,
                (policy.value, json.dumps(groups, ensure_ascii=False), _now(), session_id),
            )
        return SessionSecuritySettings(approval_policy=policy, allowed_tool_groups=groups)

    def save_memory_state(
        self,
        session_id: str,
        memory: dict[str, Any],
        last_compacted_message_id: int,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                """
                update chat_sessions
                set memory_json=?, memory_summary=?, memory_version=memory_version+1,
                    last_compacted_message_id=?, compact_error=?, updated_at=?
                where session_id=?
                """,
                (
                    json.dumps(memory, ensure_ascii=False, indent=2, default=str),
                    "",
                    last_compacted_message_id,
                    error or "",
                    _now(),
                    session_id,
                ),
            )

    def save_compact_error(self, session_id: str, error: str) -> None:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            conn.execute(
                "update chat_sessions set compact_error=?, updated_at=? where session_id=?",
                (error, _now(), session_id),
            )

    def messages_for_compaction(self, session_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, role, content, reasoning_content, tool_calls_json, report_links_json, sources_json, citations_json, created_at
                from chat_messages
                where session_id=? and message_id>?
                order by message_id asc
                limit ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            tool_calls = json.loads(row["tool_calls_json"] or "[]")
            entry: dict[str, Any] = {
                "message_id": int(row["message_id"]),
                "role": row["role"],
                "content": row["content"],
                "reasoning_content": row["reasoning_content"] or "",
                "tool_calls": tool_calls,
                "report_links": json.loads(row["report_links_json"] or "[]"),
                "sources": json.loads(row["sources_json"] or "[]"),
                "citations": json.loads(row["citations_json"] or "[]"),
                "created_at": row["created_at"],
            }
            # Inline tool results so the compactor sees actual outputs
            if tool_calls:
                entry["tool_results"] = [
                    {"tool": tc.get("tool"), "result": tc.get("result")}
                    for tc in tool_calls
                    if tc.get("result") is not None
                ]
            result.append(entry)
        return result

    def latest_message_id(self, session_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(message_id), 0) as mid from chat_messages where session_id=?",
                (session_id,),
            ).fetchone()
        return int(row["mid"] if row else 0)

    def acquire_run(self, session_id: str, trigger_type: str, payload: dict[str, Any] | None = None) -> str | None:
        now = _now()
        run_id = str(uuid4())
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            current = conn.execute(
                "select active_run_id from chat_sessions where session_id=?",
                (session_id,),
            ).fetchone()
            if current and current["active_run_id"]:
                if self._is_blocking_active_run(conn, current["active_run_id"]):
                    return None
                self._clear_active_run(conn, session_id, current["active_run_id"], status="timed_out")
            conn.execute(
                """
                insert into agent_runs(run_id, session_id, trigger_type, status, input_event_json, created_at, started_at)
                values (?, ?, ?, 'running', ?, ?, ?)
                """,
                (run_id, session_id, trigger_type, json.dumps(payload or {}, ensure_ascii=False, default=str), now, now),
            )
            conn.execute(
                "update chat_sessions set active_run_id=?, updated_at=? where session_id=?",
                (run_id, now, session_id),
            )
        return run_id

    def finish_run(self, session_id: str, run_id: str, status: str = "completed") -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "update agent_runs set status=?, completed_at=? where run_id=?",
                (status, now, run_id),
            )
            conn.execute(
                "update chat_sessions set active_run_id=null, updated_at=? where session_id=? and active_run_id=?",
                (now, session_id, run_id),
            )

    def add_event(self, session_id: str, event_type: str, payload: dict[str, Any], priority: int = 100) -> int:
        with self._lock, self._connect() as conn:
            self._ensure_session(conn, session_id)
            cursor = conn.execute(
                """
                insert into session_events(session_id, event_type, payload_json, status, priority, created_at)
                values (?, ?, ?, 'pending', ?, ?)
                """,
                (session_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), priority, _now()),
            )
            return int(cursor.lastrowid)

    def next_pending_event(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                select event_id, session_id, event_type, payload_json
                from session_events
                where status='pending'
                order by priority asc, event_id asc
                limit 1
                """
            ).fetchone()
            if not row:
                return None
            active = conn.execute(
                "select active_run_id from chat_sessions where session_id=?",
                (row["session_id"],),
            ).fetchone()
            if active and active["active_run_id"] and self._is_blocking_active_run(conn, active["active_run_id"]):
                return None
            if active and active["active_run_id"]:
                self._clear_active_run(conn, row["session_id"], active["active_run_id"], status="timed_out")
            conn.execute("update session_events set status='processing' where event_id=?", (row["event_id"],))
            return {
                "event_id": row["event_id"],
                "session_id": row["session_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }

    def mark_event(self, event_id: int, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "update session_events set status=?, processed_at=? where event_id=?",
                (status, _now(), event_id),
            )

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists chat_sessions (
                    session_id text primary key,
                    title text,
                    memory_summary text default '',
                    memory_json text default '',
                    memory_version integer default 0,
                    last_compacted_message_id integer default 0,
                    compact_error text default '',
                    last_memory_hook_message_id integer default 0,
                    last_memory_hook_at text default '',
                    memory_hook_error text default '',
                    memory_hook_scan_message_id integer default 0,
                    memory_hook_buffer_json text default '[]',
                    memory_hook_buffer_count integer default 0,
                    active_approval_action_id text default '',
                    approval_queue_json text default '[]',
                    active_run_id text,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists chat_messages (
                    message_id integer primary key autoincrement,
                    session_id text not null,
                    role text not null,
                    content text not null,
                    reasoning_content text not null default '',
                    tool_calls_json text not null default '[]',
                    report_links_json text not null default '[]',
                    sources_json text not null default '[]',
                    citations_json text not null default '[]',
                    attachments_json text not null default '[]',
                    created_at text not null
                );
                create table if not exists agent_runs (
                    run_id text primary key,
                    session_id text not null,
                    trigger_type text not null,
                    status text not null,
                    input_event_json text not null default '{}',
                    created_at text not null,
                    started_at text,
                    completed_at text
                );
                create table if not exists session_events (
                    event_id integer primary key autoincrement,
                    session_id text not null,
                    event_type text not null,
                    payload_json text not null,
                    status text not null,
                    priority integer not null default 100,
                    created_at text not null,
                    processed_at text
                );
                """
            )
            self._ensure_columns(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        session_rows = conn.execute("pragma table_info(chat_sessions)").fetchall()
        existing = {row["name"] for row in session_rows}
        session_migrations = {
            "memory_json": "alter table chat_sessions add column memory_json text default ''",
            "last_compacted_message_id": "alter table chat_sessions add column last_compacted_message_id integer default 0",
            "compact_error": "alter table chat_sessions add column compact_error text default ''",
            "last_memory_hook_message_id": "alter table chat_sessions add column last_memory_hook_message_id integer default 0",
            "last_memory_hook_at": "alter table chat_sessions add column last_memory_hook_at text default ''",
            "memory_hook_error": "alter table chat_sessions add column memory_hook_error text default ''",
            "memory_hook_scan_message_id": "alter table chat_sessions add column memory_hook_scan_message_id integer default 0",
            "memory_hook_buffer_json": "alter table chat_sessions add column memory_hook_buffer_json text default '[]'",
            "memory_hook_buffer_count": "alter table chat_sessions add column memory_hook_buffer_count integer default 0",
            "active_approval_action_id": "alter table chat_sessions add column active_approval_action_id text default ''",
            "approval_queue_json": "alter table chat_sessions add column approval_queue_json text default '[]'",
            "approval_policy": "alter table chat_sessions add column approval_policy text default 'balanced'",
            "allowed_tool_groups_json": "alter table chat_sessions add column allowed_tool_groups_json text default ''",
        }
        for name, sql in session_migrations.items():
            if name not in existing:
                conn.execute(sql)
        message_rows = conn.execute("pragma table_info(chat_messages)").fetchall()
        message_existing = {row["name"] for row in message_rows}
        message_migrations = {
            "reasoning_content": "alter table chat_messages add column reasoning_content text not null default ''",
            "sources_json": "alter table chat_messages add column sources_json text not null default '[]'",
            "citations_json": "alter table chat_messages add column citations_json text not null default '[]'",
            "attachments_json": "alter table chat_messages add column attachments_json text not null default '[]'",
        }
        for name, sql in message_migrations.items():
            if name not in message_existing:
                conn.execute(sql)

    def _ensure_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        now = _now()
        conn.execute(
            """
            insert into chat_sessions(session_id, title, created_at, updated_at)
            values (?, ?, ?, ?)
            on conflict(session_id) do nothing
            """,
            (session_id, session_id, now, now),
        )

    def _ensure_session_exists(self, conn: sqlite3.Connection, session_id: str) -> None:
        row = conn.execute("select session_id from chat_sessions where session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)

    def _row_to_message(self, row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            message_id=int(row["message_id"]),
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            reasoning_content=row["reasoning_content"] or "",
            tool_calls=json.loads(row["tool_calls_json"] or "[]"),
            report_links=json.loads(row["report_links_json"] or "[]"),
            sources=json.loads(row["sources_json"] or "[]"),
            citations=json.loads(row["citations_json"] or "[]"),
            attachments=json.loads(row["attachments_json"] or "[]"),
            created_at=row["created_at"],
        )

    def _is_blocking_active_run(self, conn: sqlite3.Connection, run_id: str) -> bool:
        row = conn.execute(
            "select status, started_at, created_at from agent_runs where run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        status = str(row["status"] or "").strip().lower()
        if status and status != "running":
            return False
        started_at = row["started_at"] or row["created_at"]
        started = _parse_iso(started_at)
        if started is None:
            return True
        return datetime.now() - started < timedelta(seconds=settings.run_lock_ttl_seconds)

    def _clear_active_run(self, conn: sqlite3.Connection, session_id: str, run_id: str, status: str = "timed_out") -> None:
        now = _now()
        conn.execute(
            "update agent_runs set status=?, completed_at=? where run_id=? and status='running'",
            (status, now, run_id),
        )
        conn.execute(
            "update chat_sessions set active_run_id=null, updated_at=? where session_id=? and active_run_id=?",
            (now, session_id, run_id),
        )

    def _session_row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "session_id": row["session_id"],
            "title": row["title"] or row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "active_run_id": row["active_run_id"] or None,
            "active_approval_action_id": row["active_approval_action_id"] or None,
            "message_count": int(row["message_count"] or 0),
            "last_role": row["last_role"],
            "last_content": row["last_content"],
            "last_message_at": row["last_message_at"],
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _memory_to_summary(memory: dict[str, Any]) -> str:
    sections = []
    for key in ("stable_memory", "working_state", "evidence", "risk_notes"):
        value = memory.get(key)
        if value:
            sections.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)[:4000]}")
    return "\n".join(sections)[:12000]


def _safe_json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _safe_json_any(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _derive_session_title(content: str, limit: int = 24) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return f"新会话 {datetime.now().strftime('%m-%d %H:%M')}"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。,.!?！？ ") + "…"


chat_session_store = ChatSessionStore()
