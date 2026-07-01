from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.core.models import PendingAction, Permission, RiskLevel


DB_PATH = DATA_DIR / "approvals.sqlite"


class ApprovalStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def create(
        self,
        tool_name: str,
        arguments: dict,
        permission: Permission,
        reason: str,
        session_id: str = "default",
        run_id: str | None = None,
        risk: RiskLevel = RiskLevel.DANGEROUS,
        risk_reason: str = "",
        idempotency_key: str | None = None,
        ttl_seconds: int = 600,
    ) -> PendingAction:
        now = _now()
        expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        normalized_arguments = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
        if idempotency_key:
            existing = self._get_by_idempotency_key(idempotency_key)
            if existing and existing.status == "pending":
                return existing
        action = PendingAction(
            action_id=str(uuid4()),
            tool_name=tool_name,
            arguments=json.loads(normalized_arguments),
            permission=permission,
            risk=risk,
            risk_reason=risk_reason,
            reason=reason,
            session_id=session_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            created_at=now,
            expires_at=expires_at,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into approval_actions(
                    action_id, tool_name, arguments_json, permission, risk, risk_reason, reason,
                    status, session_id, run_id, idempotency_key, created_at, expires_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.tool_name,
                    normalized_arguments,
                    action.permission.value,
                    action.risk.value,
                    action.risk_reason,
                    action.reason,
                    action.status,
                    action.session_id,
                    action.run_id,
                    action.idempotency_key,
                    action.created_at,
                    action.expires_at,
                ),
            )
        return action

    def list_pending(self) -> list[PendingAction]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "select * from approval_actions where status='pending' order by created_at asc"
            ).fetchall()
        return [self._row_to_action(row) for row in rows]

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from approval_actions where action_id=?", (action_id,)).fetchone()
        return self._row_to_action(row) if row else None

    def mark(self, action_id: str, status: str) -> PendingAction | None:
        updates = {"status": status}
        if status in {"approved", "rejected", "denied", "executed", "failed"}:
            updates["decided_at"] = _now()
        if status in {"executed", "failed"}:
            updates["executed_at"] = _now()
        return self.update(action_id, **updates)

    def update(
        self,
        action_id: str,
        *,
        status: str | None = None,
        arguments: dict[str, Any] | None = None,
        result: Any | None = None,
        error: str | None = None,
        decided_at: str | None = None,
        executed_at: str | None = None,
    ) -> PendingAction | None:
        existing = self.get(action_id)
        if not existing:
            return None
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if arguments is not None:
            values["arguments_json"] = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        if result is not None:
            values["result_json"] = json.dumps(result, ensure_ascii=False, default=str)
        if error is not None:
            values["error"] = error
        if decided_at is not None:
            values["decided_at"] = decided_at
        if executed_at is not None:
            values["executed_at"] = executed_at
        if not values:
            return existing
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"update approval_actions set {assignments} where action_id=?",
                (*values.values(), action_id),
            )
        return self.get(action_id)

    def delete_by_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("delete from approval_actions where session_id=?", (session_id,))

    def _get_by_idempotency_key(self, key: str) -> PendingAction | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from approval_actions where idempotency_key=?", (key,)).fetchone()
        return self._row_to_action(row) if row else None

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists approval_actions (
                    action_id text primary key,
                    tool_name text not null,
                    arguments_json text not null,
                    permission text not null,
                    risk text not null,
                    risk_reason text not null default '',
                    reason text not null,
                    status text not null,
                    session_id text not null,
                    run_id text,
                    idempotency_key text unique,
                    created_at text not null,
                    expires_at text,
                    decided_at text,
                    executed_at text,
                    result_json text,
                    error text
                );
                create index if not exists idx_approval_status on approval_actions(status);
                create index if not exists idx_approval_session on approval_actions(session_id);
                """
            )
            self._ensure_columns(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("pragma table_info(approval_actions)").fetchall()
        existing = {row["name"] for row in rows}
        if "expires_at" not in existing:
            conn.execute("alter table approval_actions add column expires_at text")

    def _row_to_action(self, row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            action_id=row["action_id"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"] or "{}"),
            permission=Permission(row["permission"]),
            risk=RiskLevel(row["risk"]),
            risk_reason=row["risk_reason"] or "",
            reason=row["reason"],
            status=row["status"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            executed_at=row["executed_at"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


approval_store = ApprovalStore()
