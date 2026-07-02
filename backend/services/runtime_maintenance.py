from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.config import CAPABILITY_RUNTIME_DIR, DATA_DIR

logger = logging.getLogger(__name__)


class RuntimeMaintenanceService:
    """Best-effort cleanup for short-lived local runtime artifacts."""

    def run_once(self) -> dict[str, Any]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {}
        for name, handler in (
            ("approvals", self._cleanup_approvals),
            ("cancellations", self._cleanup_cancellations),
            ("resource_limits", self._cleanup_resource_limits),
            ("observability", self._cleanup_observability),
            ("research_threads", self._cleanup_research_threads),
            ("research_runs", self._cleanup_research_runs),
            ("report_trash", self._cleanup_report_trash),
            ("audit_log", self._rotate_audit_log),
            ("memory", self._cleanup_memory),
            ("capabilities", self._cleanup_capability_artifacts),
        ):
            try:
                summary[name] = handler()
            except Exception as exc:
                logger.warning("Runtime maintenance step failed: %s: %s", name, exc)
                summary[name] = {"error": str(exc)}
        logger.info("Runtime maintenance completed: %s", summary)
        return summary

    def _cleanup_approvals(self) -> dict[str, int]:
        db_path = DATA_DIR / "approvals.sqlite"
        if not db_path.exists():
            return {"expired": 0, "deleted": 0}
        now = _now()
        cutoff = _iso_days_ago(1)
        with _connect(db_path) as conn:
            expired = conn.execute(
                """
                update approval_actions
                set status='expired', decided_at=coalesce(decided_at, ?)
                where status='pending' and expires_at is not null and expires_at < ?
                """,
                (now, now),
            ).rowcount
            deleted = conn.execute(
                """
                delete from approval_actions
                where status != 'pending'
                  and coalesce(executed_at, decided_at, created_at) < ?
                """,
                (cutoff,),
            ).rowcount
        return {"expired": int(expired or 0), "deleted": int(deleted or 0)}

    def _cleanup_cancellations(self) -> dict[str, int]:
        return _delete_sqlite_rows(
            DATA_DIR / "cancellations.sqlite",
            "delete from cancellations where requested_at < ?",
            (_iso_days_ago(1),),
        )

    def _cleanup_resource_limits(self) -> dict[str, int]:
        return _delete_sqlite_rows(
            DATA_DIR / "resource_limits.sqlite",
            "delete from resource_events where created_at < ?",
            (_iso_days_ago(2),),
        )

    def _cleanup_observability(self) -> dict[str, int]:
        db_path = DATA_DIR / "observability.sqlite"
        if not db_path.exists():
            return {"deleted_traces": 0, "deleted_llm_logs": 0}
        cutoff = _iso_days_ago(14)
        with _connect(db_path) as conn:
            old_trace_ids = [
                row["trace_id"]
                for row in conn.execute("select trace_id from traces where started_at < ?", (cutoff,)).fetchall()
            ]
            if old_trace_ids:
                conn.executemany("delete from trace_events where trace_id=?", ((trace_id,) for trace_id in old_trace_ids))
                conn.executemany("delete from spans where trace_id=?", ((trace_id,) for trace_id in old_trace_ids))
                conn.executemany("delete from traces where trace_id=?", ((trace_id,) for trace_id in old_trace_ids))
            deleted_llm = conn.execute(
                """
                delete from llm_call_logs
                where id not in (
                    select id from llm_call_logs order by id desc limit 500
                )
                """
            ).rowcount
        return {"deleted_traces": len(old_trace_ids), "deleted_llm_logs": int(deleted_llm or 0)}

    def _cleanup_research_threads(self) -> dict[str, int]:
        db_path = DATA_DIR / "research_threads.sqlite"
        if not db_path.exists():
            return {"deleted": 0}
        cutoff = _iso_days_ago(30)
        terminal = ("completed", "failed", "cancelled", "canceled")
        with _connect(db_path) as conn:
            placeholders = ",".join("?" for _ in terminal)
            deleted = conn.execute(
                f"""
                delete from research_threads
                where status in ({placeholders})
                  and coalesce(completed_at, updated_at, created_at) < ?
                """,
                (*terminal, cutoff),
            ).rowcount
        return {"deleted": int(deleted or 0)}

    def _cleanup_research_runs(self) -> dict[str, int]:
        runs_dir = DATA_DIR / "research_runs"
        if not runs_dir.exists():
            return {"deleted": 0}
        status_by_thread = self._research_thread_statuses()
        deleted = 0
        now = datetime.now()
        for child in runs_dir.iterdir():
            if not child.is_dir():
                continue
            status = status_by_thread.get(child.name)
            age_days = (now - datetime.fromtimestamp(child.stat().st_mtime)).total_seconds() / 86400
            retain_days = 7 if status in {"failed", "cancelled", "canceled"} else 14
            if status in {"pending", "in_progress", "paused", "waiting_approval"}:
                continue
            if age_days > retain_days:
                shutil.rmtree(child, ignore_errors=True)
                deleted += 1
        return {"deleted": deleted}

    def _cleanup_report_trash(self) -> dict[str, int]:
        return {"deleted": _delete_children_older_than(DATA_DIR / "report_trash", days=30)}

    def _rotate_audit_log(self) -> dict[str, int]:
        path = DATA_DIR / "audit.log"
        max_bytes = 10 * 1024 * 1024
        if not path.exists() or path.stat().st_size <= max_bytes:
            return {"rotated": 0, "deleted_archives": 0}
        for index in range(5, 0, -1):
            src = DATA_DIR / f"audit.log.{index}"
            dst = DATA_DIR / f"audit.log.{index + 1}"
            if src.exists():
                if index == 5:
                    src.unlink(missing_ok=True)
                else:
                    src.replace(dst)
        path.replace(DATA_DIR / "audit.log.1")
        path.write_text("", encoding="utf-8")
        return {"rotated": 1, "deleted_archives": 1 if (DATA_DIR / "audit.log.6").exists() else 0}

    def _cleanup_memory(self) -> dict[str, Any]:
        memory_dir = DATA_DIR / "memory"
        if not memory_dir.exists():
            return {}
        return {
            "archive": self._trim_archive_files(memory_dir / "archive"),
            "candidates": self._truncate_old_jsonl_files(memory_dir / "candidates", days=30),
            "conflicts": self._cleanup_conflicts(memory_dir / "conflicts"),
            "events": self._trim_memory_events(memory_dir / "events" / "memory_events.jsonl"),
            "decision_drafts": _delete_children_older_than(memory_dir / "decisions" / "drafts", days=30),
            "pattern_candidates": self._archive_pattern_candidates(memory_dir / "patterns" / "candidates.md", memory_dir / "archive"),
        }

    def _cleanup_capability_artifacts(self) -> dict[str, Any]:
        return {
            "equityscope_reports": self._keep_latest_report_dirs(CAPABILITY_RUNTIME_DIR / "equityscope" / "logs", keep=3),
            "equityscope_cache": _delete_children_older_than(CAPABILITY_RUNTIME_DIR / "equityscope" / "cache", days=30),
            "themeradar_reports": self._keep_latest_report_dirs(CAPABILITY_RUNTIME_DIR / "themeradar" / "market_discovery", keep=3),
        }

    def _research_thread_statuses(self) -> dict[str, str]:
        db_path = DATA_DIR / "research_threads.sqlite"
        if not db_path.exists():
            return {}
        with _connect(db_path) as conn:
            rows = conn.execute("select thread_id, status from research_threads").fetchall()
        return {str(row["thread_id"]): str(row["status"] or "") for row in rows}

    def _trim_archive_files(self, archive_dir: Path) -> dict[str, int]:
        if not archive_dir.exists():
            return {"deleted": 0}
        files = [path for path in archive_dir.iterdir() if path.is_file()]
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        cutoff = datetime.now() - timedelta(days=180)
        deleted = 0
        for index, path in enumerate(files):
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if index >= 20 or modified < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        return {"deleted": deleted}

    def _truncate_old_jsonl_files(self, root: Path, days: int) -> dict[str, int]:
        if not root.exists():
            return {"truncated": 0}
        cutoff = datetime.now() - timedelta(days=days)
        truncated = 0
        for path in root.glob("*.jsonl"):
            if path.stat().st_size <= 0:
                continue
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.write_text("", encoding="utf-8")
                truncated += 1
        return {"truncated": truncated}

    def _cleanup_conflicts(self, root: Path) -> dict[str, int]:
        resolved = root / "resolved_conflicts.jsonl"
        if not resolved.exists() or resolved.stat().st_size <= 0:
            return {"truncated_resolved": 0}
        cutoff = datetime.now() - timedelta(days=90)
        if datetime.fromtimestamp(resolved.stat().st_mtime) >= cutoff:
            return {"truncated_resolved": 0}
        resolved.write_text("", encoding="utf-8")
        return {"truncated_resolved": 1}

    def _trim_memory_events(self, path: Path) -> dict[str, int]:
        if not path.exists() or path.stat().st_size <= 0:
            return {"removed_lines": 0}
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        cutoff = datetime.now() - timedelta(days=90)
        recent_tail = set(range(max(0, len(lines) - 1000), len(lines)))
        kept: list[str] = []
        for index, line in enumerate(lines):
            parsed_at = _jsonl_timestamp(line)
            keep_by_age = parsed_at is None or parsed_at >= cutoff
            if index in recent_tail and keep_by_age:
                kept.append(line)
        if len(kept) != len(lines):
            path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
        return {"removed_lines": len(lines) - len(kept)}

    def _archive_pattern_candidates(self, path: Path, archive_dir: Path) -> dict[str, int]:
        if not path.exists() or path.stat().st_size <= 0:
            return {"archived": 0}
        cutoff = datetime.now() - timedelta(days=90)
        if datetime.fromtimestamp(path.stat().st_mtime) >= cutoff:
            return {"archived": 0}
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return {"archived": 0}
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"pattern_candidates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        archive_path.write_text(content + "\n", encoding="utf-8")
        path.write_text("# Pattern Candidates\n\nPatterns inferred from ledger and decisions stay here until user confirmation.\n", encoding="utf-8")
        return {"archived": 1}

    def _keep_latest_report_dirs(self, root: Path, keep: int) -> dict[str, int]:
        if not root.exists():
            return {"deleted": 0}
        deleted = 0
        for group_dir in [item for item in root.iterdir() if item.is_dir()]:
            report_dirs = [item for item in group_dir.iterdir() if item.is_dir()]
            report_dirs.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)
            for stale in report_dirs[keep:]:
                shutil.rmtree(stale, ignore_errors=True)
                deleted += 1
        return {"deleted": deleted}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _delete_sqlite_rows(db_path: Path, sql: str, args: tuple[Any, ...]) -> dict[str, int]:
    if not db_path.exists():
        return {"deleted": 0}
    with _connect(db_path) as conn:
        deleted = conn.execute(sql, args).rowcount
    return {"deleted": int(deleted or 0)}


def _delete_children_older_than(root: Path, days: int) -> int:
    if not root.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    for child in root.iterdir():
        try:
            if datetime.fromtimestamp(child.stat().st_mtime) >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def _jsonl_timestamp(line: str) -> datetime | None:
    try:
        payload = json.loads(line)
    except Exception:
        return None
    for key in ("created_at", "updated_at", "timestamp", "time"):
        raw = payload.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
    return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


runtime_maintenance_service = RuntimeMaintenanceService()
