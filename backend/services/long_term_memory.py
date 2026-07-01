from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.core.openai_stream import openai_stream_client


MEMORY_DIR = DATA_DIR / "memory"
PROMPTS_DIR = DATA_DIR.parent / "prompts"
ARCHIVE_DIR = MEMORY_DIR / "archive"
CANDIDATE_DIR = MEMORY_DIR / "candidates"
CONFLICT_DIR = MEMORY_DIR / "conflicts"
EVENT_DIR = MEMORY_DIR / "events"
INDEX_FILE = MEMORY_DIR / "index.md"
CORE_METADATA_FILE = MEMORY_DIR / "core_metadata.json"
EVENT_FILE = EVENT_DIR / "memory_events.jsonl"
PENDING_CONFLICTS_FILE = CONFLICT_DIR / "pending_conflicts.jsonl"
RESOLVED_CONFLICTS_FILE = CONFLICT_DIR / "resolved_conflicts.jsonl"
DECISIONS_DIR = MEMORY_DIR / "decisions"
PROFILE_LOG_THRESHOLD = 8
PROFILE_COMPRESSION_PROMPT_PATH = PROMPTS_DIR / "core" / "compression_prompt.md"
LEVELS_DIR = PROMPTS_DIR / "levels"

CORE_FILES = {
    "profile": MEMORY_DIR / "profile.md",
    "playbook": MEMORY_DIR / "playbook.md",
    "convictions": MEMORY_DIR / "convictions.md",
}

ARCHIVE_FILES = {
    "profile": ARCHIVE_DIR / "profile_archive.md",
    "playbook": ARCHIVE_DIR / "playbook_archive.md",
    "convictions": ARCHIVE_DIR / "convictions_archive.md",
}

CANDIDATE_FILES = {
    "profile": CANDIDATE_DIR / "profile_candidates.jsonl",
    "playbook": CANDIDATE_DIR / "playbook_candidates.jsonl",
    "convictions": CANDIDATE_DIR / "convictions_candidates.jsonl",
}

NEGATIVE_TERMS = ("不再", "不是", "差不多了", "结束", "弱化", "失效", "看空", "降低", "退出", "清仓")
POSITIVE_TERMS = ("核心", "配置", "看好", "长期", "主线", "继续", "增强", "买入", "加仓")


class LongTermMemoryService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def get_core(self, file_type: str) -> dict[str, Any]:
        file_type = _validate_file_type(file_type)
        self._ensure_dirs()
        path = CORE_FILES[file_type]
        if not path.exists():
            return {"success": False, "content": "", "metadata": self._metadata(file_type, "")}
        content = path.read_text(encoding="utf-8")
        return {"success": True, "content": content, "metadata": self._metadata(file_type, content)}

    def get_index(self) -> dict[str, Any]:
        with self._lock:
            self.rebuild_index()
            content = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""
            return {"success": True, "content": content, "metadata": {"file_size": len(content)}}

    def read_profile_context(self) -> dict[str, Any]:
        """Return the small profile context allowed in normal chat prompts."""
        self._ensure_dirs()
        path = CORE_FILES["profile"]
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        parsed = self._parse_profile(content)
        snapshot = parsed.get("snapshot", "").strip()
        current_level = parsed.get("current_level") or "Level 1"
        return {
            "success": bool(content),
            "current_level": current_level,
            "snapshot": snapshot,
            "log_count": parsed.get("log_count", 0),
            "window_no": parsed.get("window_no", 1),
            "last_updated": self._metadata("profile", content).get("last_updated", "未知") if content else "未知",
            "content": self._render_profile_prompt_context(current_level, snapshot),
        }

    def update_core(
        self,
        file_type: str,
        content: str,
        reason: str = "用户手动编辑",
        source: str = "manual_edit",
    ) -> dict[str, Any]:
        file_type = _validate_file_type(file_type)
        with self._lock:
            self._ensure_dirs()
            path = CORE_FILES[file_type]
            old_content = path.read_text(encoding="utf-8") if path.exists() else ""
            if old_content:
                self._archive_core(file_type, old_content, reason)
            path.write_text(str(content or ""), encoding="utf-8")
            event = self._event(
                "core_updated",
                {
                    "file": file_type,
                    "source": source,
                    "reason": reason,
                    "old_length": len(old_content),
                    "new_length": len(str(content or "")),
                },
            )
            self._touch_core_metadata(file_type, source=source, reason=reason, event_id=event.get("event_id"))
            conflicts = self.detect_conflicts(file_type, str(content or ""), source=source)
            self.rebuild_index()
            return {
                "success": True,
                "message": f"成功更新 {file_type}，旧版本已归档",
                "event": event,
                "conflicts": conflicts,
            }

    def create_candidate(
        self,
        target: str,
        content: str,
        evidence: str = "",
        confidence: float = 0.5,
        operation: str = "ADD",
        reason: str = "",
        source_session_id: str | None = None,
        source_message_id: int | None = None,
        related_refs: list[Any] | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        target = _validate_file_type(target)
        operation = _normalize_operation(operation)
        if target == "playbook":
            self._validate_playbook_candidate(str(content or ""), operation)
        if target == "convictions":
            self._validate_conviction_candidate(str(content or ""), operation)
        now = _now()
        candidate = {
            "candidate_id": f"mem_cand_{uuid4().hex[:12]}",
            "target": target,
            "content": str(content or "").strip(),
            "evidence": str(evidence or "").strip(),
            "source_session_id": source_session_id,
            "source_message_id": source_message_id,
            "confidence": _clamp_confidence(confidence),
            "operation": operation,
            "status": status,
            "reason": str(reason or ""),
            "related_refs": related_refs or [],
            "created_at": now,
            "updated_at": now,
        }
        if not candidate["content"]:
            raise ValueError("candidate content is required")
        with self._lock:
            self._ensure_dirs()
            rows = self._read_jsonl(CANDIDATE_FILES[target])
            rows.append(candidate)
            self._write_jsonl(CANDIDATE_FILES[target], rows)
            if operation == "CONFLICT":
                self._record_conflict(
                    source="candidate",
                    changed_file=target,
                    changed_content=candidate["content"],
                    conflicts_with=[],
                    reason=candidate["reason"] or "candidate marked as conflict",
                )
            self._event("candidate_created", {"candidate_id": candidate["candidate_id"], "target": target, "operation": operation})
            self._touch_candidate_metadata(target, candidate)
            self.rebuild_index()
        return candidate

    def apply_agent_profile_entry(
        self,
        content: str,
        evidence: str = "",
        confidence: float = 0.86,
        operation: str = "ADD",
        reason: str = "agent profile update",
        source_session_id: str | None = None,
        source_message_id: int | None = None,
        related_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            target = "profile"
            path = CORE_FILES[target]
            self._ensure_dirs()
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            entry_id = f"profile_auto_{uuid4().hex[:12]}"
            entry = {
                "candidate_id": entry_id,
                "target": target,
                "content": str(content or "").strip(),
                "evidence": str(evidence or ""),
                "confidence": float(confidence),
                "operation": str(operation or "ADD").upper(),
                "reason": str(reason or ""),
                "source_session_id": source_session_id,
                "source_message_id": source_message_id,
                "related_refs": related_refs or [],
            }
            if not entry["content"]:
                raise ValueError("profile 自动写入内容不能为空")
            if not existing.strip():
                existing = self._default_profile_content()
            new_content = self._append_profile_log_entry(existing, entry)
            path.write_text(new_content, encoding="utf-8")
            parsed = self._parse_profile(new_content)
            conflicts: list[dict[str, Any]] = []
            event = self._event(
                "profile_log_appended",
                {
                    "entry_id": entry_id,
                    "target": target,
                    "reason": reason,
                    "log_count": parsed.get("log_count", 0),
                    "window_no": parsed.get("window_no", 1),
                },
            )
            self._touch_core_metadata(
                target,
                source="agent",
                reason=reason,
                event_id=event.get("event_id"),
                candidate_id=entry_id,
            )
            compression_event_id = self._maybe_enqueue_profile_compression(parsed, source_session_id)
            self.rebuild_index()
            return {
                "success": True,
                "entry": entry,
                "conflicts": conflicts,
                "log_count": parsed.get("log_count", 0),
                "compression_event_id": compression_event_id,
            }

    def compress_profile_window(self, session_id: str | None = None) -> dict[str, Any]:
        """Compress the current profile LOG window into SNAPSHOT and clear LOG on success."""
        with self._lock:
            self._ensure_dirs()
            path = CORE_FILES["profile"]
            existing = path.read_text(encoding="utf-8") if path.exists() else self._default_profile_content()
            parsed = self._parse_profile(existing)
            if int(parsed.get("log_count") or 0) <= 0:
                self._clear_profile_compression_pending()
                return {"success": True, "skipped": True, "message": "profile LOG 为空，无需压缩"}
            prompt = PROFILE_COMPRESSION_PROMPT_PATH.read_text(encoding="utf-8") if PROFILE_COMPRESSION_PROMPT_PATH.exists() else ""

        payload = self._build_profile_compression_payload(parsed)

        if not openai_stream_client.configured:
            self._event("profile_compression_skipped", {"reason": "llm_not_configured", "session_id": session_id})
            return {"success": False, "message": "LLM 未配置，profile LOG 未清空"}

        llm_messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": "请根据以下 JSON 输入执行人物志压缩和晋级评估，只返回严格 JSON。\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            },
        ]
        chunks: list[str] = []
        for chunk in openai_stream_client.stream_chat(llm_messages, tools=[]):
            if chunk.content:
                chunks.append(chunk.content)
        raw_output = "".join(chunks).strip()
        result_json = self._parse_llm_json(raw_output)
        rendered = self._render_compressed_profile(parsed, result_json)

        with self._lock:
            current = path.read_text(encoding="utf-8") if path.exists() else existing
            self._archive_core("profile", current, "profile log compression")
            path.write_text(rendered, encoding="utf-8")
            event = self._event(
                "profile_compressed",
                {
                    "session_id": session_id,
                    "window_no": parsed.get("window_no"),
                    "old_log_count": parsed.get("log_count"),
                    "evaluation": result_json.get("window_evaluation"),
                    "new_level": result_json.get("new_level"),
                },
            )
            self._touch_core_metadata("profile", source="profile_compression", reason="profile LOG threshold reached", event_id=event.get("event_id"))
            self._clear_profile_compression_pending()
            self.rebuild_index()
        return {
            "success": True,
            "message": "profile LOG 已压缩到 SNAPSHOT",
            "event": event,
            "raw_output_preview": raw_output[:1200],
            "result": result_json,
        }

    def list_candidates(self, status: str | None = None, target: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_dirs()
            rows = self._all_candidates()
            if target:
                target = _validate_file_type(target)
                rows = [row for row in rows if row.get("target") == target]
            if status:
                rows = [row for row in rows if str(row.get("status") or "") == status]
            rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
            return {"success": True, "candidates": rows}

    def update_candidate(self, candidate_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            target, rows, idx = self._find_candidate(candidate_id)
            candidate = rows[idx]
            for key in ("content", "evidence", "reason"):
                if key in updates:
                    candidate[key] = str(updates.get(key) or "")
            if "confidence" in updates:
                candidate["confidence"] = _clamp_confidence(updates.get("confidence"))
            if "operation" in updates:
                candidate["operation"] = _normalize_operation(updates.get("operation"))
            if target == "playbook":
                self._validate_playbook_candidate(str(candidate.get("content") or ""), str(candidate.get("operation") or "ADD").upper())
            if target == "convictions":
                self._validate_conviction_candidate(str(candidate.get("content") or ""), str(candidate.get("operation") or "ADD").upper())
            candidate["updated_at"] = _now()
            rows[idx] = candidate
            self._write_jsonl(CANDIDATE_FILES[target], rows)
            self._event("candidate_updated", {"candidate_id": candidate_id, "target": target})
            self.rebuild_index()
            return {"success": True, "candidate": candidate}

    def approve_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            target, rows, idx = self._find_candidate(candidate_id)
            candidate = rows[idx]
            if candidate.get("status") in {"approved", "applied"}:
                return {"success": True, "candidate": candidate, "message": "candidate already approved"}
            operation = str(candidate.get("operation") or "").upper()
            if target == "playbook":
                self._validate_playbook_candidate(str(candidate.get("content") or ""), operation)
            if target == "convictions":
                self._validate_conviction_candidate(str(candidate.get("content") or ""), operation)
            path = CORE_FILES[target]
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if target == "playbook" and operation not in {"ARCHIVE", "CONFLICT"}:
                self._archive_core(target, existing, f"candidate update: {candidate_id}")
                new_content = self._apply_playbook_candidate(existing, candidate, operation)
                archived_entry = ""
            elif operation in {"ARCHIVE", "CONFLICT"}:
                archive_result = self._apply_archive_candidate(target, existing, candidate)
                new_content = archive_result["content"]
                archived_entry = archive_result.get("archived_entry", "")
            elif operation in {"UPDATE", "WEAKEN"} and existing:
                self._archive_core(target, existing, f"candidate update: {candidate_id}")
                evidence = str(candidate.get("evidence") or "").strip()
                rendered = self._render_core_entry(candidate)
                if evidence and evidence in existing:
                    new_content = existing.replace(evidence, rendered, 1)
                elif target == "playbook":
                    raise ValueError("Playbook 更新失败：未找到要替换的研究架构片段，不能回退为追加")
                else:
                    new_content = self._append_core_entry(existing, candidate)
            else:
                new_content = self._append_core_entry(existing, candidate)
            path.write_text(new_content, encoding="utf-8")
            candidate["status"] = "applied"
            candidate["approved_at"] = _now()
            candidate["updated_at"] = _now()
            rows[idx] = candidate
            self._write_jsonl(CANDIDATE_FILES[target], rows)
            conflicts = [] if operation in {"ARCHIVE", "CONFLICT"} else self.detect_conflicts(target, str(candidate.get("content") or ""), source="candidate")
            event = self._event("candidate_applied", {
                "candidate_id": candidate_id,
                "target": target,
                "operation": operation,
                "archived_length": len(archived_entry) if operation in {"ARCHIVE", "CONFLICT"} else 0,
            })
            self._touch_core_metadata(
                target,
                source="candidate",
                reason=f"candidate applied: {candidate_id}",
                event_id=event.get("event_id"),
                candidate_id=candidate_id,
            )
            self.rebuild_index()
            return {"success": True, "candidate": candidate, "conflicts": conflicts}

    def reject_candidate(self, candidate_id: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            target, rows, idx = self._find_candidate(candidate_id)
            candidate = rows[idx]
            candidate["status"] = "rejected"
            candidate["rejected_reason"] = str(reason or "")
            candidate["updated_at"] = _now()
            rows[idx] = candidate
            self._write_jsonl(CANDIDATE_FILES[target], rows)
            self._event("candidate_rejected", {"candidate_id": candidate_id, "target": target})
            self.rebuild_index()
            return {"success": True, "candidate": candidate}

    def list_conflicts(self, status: str | None = "pending") -> dict[str, Any]:
        with self._lock:
            self._ensure_dirs()
            rows = self._read_jsonl(PENDING_CONFLICTS_FILE) + self._read_jsonl(RESOLVED_CONFLICTS_FILE)
            if status:
                rows = [row for row in rows if str(row.get("status") or "") == status]
            rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            return {"success": True, "conflicts": rows}

    def resolve_conflict(self, conflict_id: str, resolution: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            pending = self._read_jsonl(PENDING_CONFLICTS_FILE)
            for idx, conflict in enumerate(pending):
                if conflict.get("conflict_id") != conflict_id:
                    continue
                conflict["status"] = "resolved" if resolution != "ignored" else "ignored"
                conflict["resolution"] = resolution
                conflict["resolution_note"] = note
                conflict["resolved_at"] = _now()
                pending.pop(idx)
                resolved = self._read_jsonl(RESOLVED_CONFLICTS_FILE)
                resolved.append(conflict)
                self._write_jsonl(PENDING_CONFLICTS_FILE, pending)
                self._write_jsonl(RESOLVED_CONFLICTS_FILE, resolved)
                self._event("conflict_resolved", {"conflict_id": conflict_id, "resolution": resolution})
                self.rebuild_index()
                return {"success": True, "conflict": conflict}
        raise KeyError(f"unknown conflict: {conflict_id}")

    def list_events(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            rows = self._read_jsonl(EVENT_FILE)
            rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            return {"success": True, "events": rows[: max(1, limit)]}

    def detect_conflicts(self, changed_file: str, changed_content: str, source: str) -> list[dict[str, Any]]:
        changed_file = _validate_file_type(changed_file)
        changed_text = str(changed_content or "")
        conflicts: list[dict[str, Any]] = []
        for file_type, path in CORE_FILES.items():
            if file_type == changed_file or not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            candidate_segments = self._find_related_segments(changed_text, file_type, content)
            if not candidate_segments:
                continue
            llm_result = self._llm_conflict_check(changed_file, changed_text, candidate_segments)
            if llm_result.get("checked") and llm_result.get("has_conflict"):
                conflicts.append(
                    self._record_conflict(
                        source=source,
                        changed_file=changed_file,
                        changed_content=changed_text[:2000],
                        conflicts_with=llm_result.get("conflicting_segments") or candidate_segments[:3],
                        reason=str(llm_result.get("reason") or "LLM detected memory conflict"),
                        severity=str(llm_result.get("severity") or "medium"),
                        conflict_type=str(llm_result.get("conflict_type") or "semantic_conflict"),
                        llm_reason=str(llm_result.get("reason") or ""),
                    )
                )
                continue
            if llm_result.get("checked"):
                continue
            conflict_refs = self._heuristic_conflicting_segments(changed_text, candidate_segments)
            if conflict_refs:
                conflicts.append(
                    self._record_conflict(
                        source=source,
                        changed_file=changed_file,
                        changed_content=changed_text[:2000],
                        conflicts_with=conflict_refs,
                        reason="heuristic fallback detected directional memory conflict",
                        severity="medium",
                        conflict_type="directional_change",
                        llm_reason="",
                    )
                )
        return conflicts

    def rebuild_index(self) -> None:
        self._ensure_dirs()
        core_summary = []
        for file_type, path in CORE_FILES.items():
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            meta = self._metadata(file_type, content)
            if file_type == "convictions":
                core_summary.append(
                    f"- convictions: {meta.get('active_count', 0)} active, "
                    f"{meta.get('watching_count', 0)} watching, updated {meta.get('last_updated', '未知')}"
                )
            elif file_type == "playbook":
                core_summary.append(f"- playbook: {meta.get('dimension_count', 0)} dimensions, updated {meta.get('last_updated', '未知')}")
            else:
                core_summary.append(f"- {file_type}: {meta.get('count', 0)} entries/sections, updated {meta.get('last_updated', '未知')}")
        candidates = self.list_candidates(status="pending").get("candidates", [])
        conflicts = self.list_conflicts(status="pending").get("conflicts", [])
        recent_events = self.list_events(limit=8).get("events", [])
        decisions = self._read_decision_rows()
        active_decisions = [row for row in decisions if row.get("status") in {"confirmed", "tracking"}]
        closed_decisions = [row for row in decisions if row.get("status") == "closed"]
        lines = [
            "# FinClaw Memory Index",
            "",
            "> Auto-generated. Core memory files are user-confirmed; candidates and conflicts require review.",
            "",
            f"- Last updated: {_now()}",
            f"- Pending candidates: {len(candidates)}",
            f"- Pending conflicts: {len(conflicts)}",
            "",
            "## Core Memory Summary",
            *core_summary,
            "",
            "## Pending Candidates",
        ]
        if candidates:
            for item in candidates[:12]:
                lines.append(f"- [{item.get('target')}/{item.get('operation')}] {str(item.get('content') or '')[:90]} ({item.get('candidate_id')})")
        else:
            lines.append("- None")
        lines.extend(["", "## Pending Conflicts"])
        if conflicts:
            for item in conflicts[:12]:
                lines.append(f"- {item.get('changed_file')} {item.get('conflict_id')}: {item.get('reason')}")
        else:
            lines.append("- None")
        lines.extend(["", "## Active Decision Threads"])
        if active_decisions:
            for item in active_decisions[:12]:
                lines.append(f"- [{item.get('status')}] {item.get('ticker')} {item.get('side')} {item.get('decision_id')}")
        else:
            lines.append("- None")
        lines.extend(["", "## Pending Decision Reviews"])
        if closed_decisions:
            for item in closed_decisions[:12]:
                lines.append(f"- {item.get('ticker')} {item.get('decision_id')}: review required")
        else:
            lines.append("- None")
        lines.extend(["", "## Recent Memory Events"])
        if recent_events:
            for item in recent_events:
                lines.append(f"- {item.get('created_at')} {item.get('event_type')}: {item.get('summary')}")
        else:
            lines.append("- None")
        INDEX_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _read_decision_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for folder in ("drafts", "active", "closed"):
            path = DECISIONS_DIR / folder
            if not path.exists():
                continue
            for item_path in path.glob("*.json"):
                try:
                    payload = json.loads(item_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        rows.append(payload)
                except Exception:
                    continue
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows

    def _parse_profile(self, content: str) -> dict[str, Any]:
        text = str(content or "")
        level_section = self._extract_profile_section(text, "[LEVEL] 当前阶段")
        snapshot = self._extract_profile_section(text, "[SNAPSHOT] 人物志")
        milestones = self._extract_profile_section(text, "[MILESTONES] 晋级记录")
        log = self._extract_profile_section(text, "[LOG]")
        current_level_match = re.search(r"current_level\s*[:：]\s*([^\n]+)", level_section or text, re.IGNORECASE)
        current_level = current_level_match.group(1).strip() if current_level_match else "Level 1"
        window_match = re.search(r"当前评估窗口\s*#?(\d+)|窗口计数\s*[:：]\s*#?(\d+)", text)
        window_no = 1
        if window_match:
            window_no = int(next(group for group in window_match.groups() if group))
        log_count = self._count_profile_log_entries(log)
        return {
            "current_level": current_level,
            "level_section": level_section,
            "snapshot": self._strip_section_comments(snapshot).strip(),
            "milestones": milestones.strip(),
            "log": log.strip(),
            "log_count": log_count,
            "window_no": window_no,
        }

    def _extract_profile_section(self, content: str, heading_fragment: str) -> str:
        lines = str(content or "").splitlines()
        start = None
        for idx, line in enumerate(lines):
            if line.startswith("## ") and heading_fragment in line:
                start = idx + 1
                break
        if start is None:
            return ""
        end = len(lines)
        for idx in range(start, len(lines)):
            if lines[idx].startswith("## "):
                end = idx
                break
        return "\n".join(lines[start:end]).strip()

    def _strip_section_comments(self, content: str) -> str:
        return "\n".join(line for line in str(content or "").splitlines() if not line.strip().startswith("<!--")).strip()

    def _render_profile_prompt_context(self, current_level: str, snapshot: str) -> str:
        if not snapshot:
            return ""
        return f"current_level: {current_level}\n\n## [SNAPSHOT] 人物志\n{snapshot.strip()}"

    def _default_profile_content(self) -> str:
        return (
            "# 用户画像\n\n"
            "## [LEVEL] 当前阶段\n"
            "current_level: Level 1\n"
            "窗口计数：#1\n"
            "本窗口晋级评估：未达成\n\n"
            "## [SNAPSHOT] 人物志\n"
            "暂无稳定人物志。\n\n"
            "## [MILESTONES] 晋级记录\n"
            "<!-- 永久保留 -->\n\n"
            "## [LOG] 当前评估窗口 #1\n"
            "<!-- 积累到8条后触发压缩，压缩后清空本节 -->\n"
            f"<!-- 条数：0/{PROFILE_LOG_THRESHOLD} -->\n"
        )

    def _append_profile_log_entry(self, existing: str, entry: dict[str, Any]) -> str:
        text = existing
        if "## [LOG]" not in text:
            text = (
                text.rstrip()
                + "\n\n## [LOG] 当前评估窗口 #1\n"
                + "<!-- 积累到8条后触发压缩，压缩后清空本节 -->\n"
                + f"<!-- 条数：0/{PROFILE_LOG_THRESHOLD} -->\n"
            )
        today = datetime.now().strftime("%Y-%m-%d")
        content = self._normalize_profile_log_content(str(entry.get("content") or ""), today)
        reason = str(entry.get("reason") or "").strip()
        confidence = _clamp_confidence(entry.get("confidence"))
        source = str(entry.get("source_session_id") or "agent")
        meta = f"<!-- source:{source} | confidence:{confidence:.2f} | operation:{entry.get('operation')} | reason:{reason[:160]} -->"
        block = f"{content}\n{meta}".strip()
        lines = text.splitlines()
        log_heading_idx = self._find_profile_log_heading(lines)
        if log_heading_idx is None:
            lines.extend(["", "## [LOG] 当前评估窗口 #1", f"<!-- 条数：0/{PROFILE_LOG_THRESHOLD} -->"])
            log_heading_idx = self._find_profile_log_heading(lines)
        assert log_heading_idx is not None
        insert_idx = len(lines)
        for idx in range(log_heading_idx + 1, len(lines)):
            if lines[idx].startswith("## "):
                insert_idx = idx
                break
        if insert_idx > log_heading_idx + 1 and lines[insert_idx - 1].strip():
            lines.insert(insert_idx, "")
            insert_idx += 1
        lines.insert(insert_idx, block)
        updated = "\n".join(lines).rstrip() + "\n"
        return self._refresh_profile_log_count_comment(updated)

    def _normalize_profile_log_content(self, content: str, today: str) -> str:
        body = str(content or "").strip()
        body = re.sub(r"^\s*[-*]\s+", "", body)
        if re.match(r"^\d{4}-\d{2}-\d{2}\s*\|", body):
            return body
        return f"{today} | {body}"

    def _find_profile_log_heading(self, lines: list[str]) -> int | None:
        for idx, line in enumerate(lines):
            if line.startswith("## ") and "[LOG]" in line:
                return idx
        return None

    def _count_profile_log_entries(self, log_text: str) -> int:
        return len(re.findall(r"(?m)^\s*\d{4}-\d{2}-\d{2}\s*\|", str(log_text or "")))

    def _refresh_profile_log_count_comment(self, content: str) -> str:
        parsed_log = self._extract_profile_section(content, "[LOG]")
        count = self._count_profile_log_entries(parsed_log)
        replacement = f"<!-- 条数：{count}/{PROFILE_LOG_THRESHOLD} -->"
        if re.search(r"<!--\s*条数\s*[:：].*?-->", content):
            return re.sub(r"<!--\s*条数\s*[:：].*?-->", replacement, content, count=1)
        lines = content.splitlines()
        log_idx = self._find_profile_log_heading(lines)
        if log_idx is not None:
            lines.insert(log_idx + 1, replacement)
        return "\n".join(lines).rstrip() + "\n"

    def _maybe_enqueue_profile_compression(self, parsed: dict[str, Any], session_id: str | None) -> int | None:
        if int(parsed.get("log_count") or 0) < PROFILE_LOG_THRESHOLD:
            return None
        metadata = self._core_metadata()
        profile_meta = metadata.get("profile") if isinstance(metadata.get("profile"), dict) else {}
        window_no = int(parsed.get("window_no") or 1)
        if profile_meta.get("profile_compression_pending_window") == window_no:
            return None
        try:
            from backend.services.continuation import continuation_service
            from backend.services.sessions import chat_session_store

            event_id = chat_session_store.add_event(
                session_id or "default",
                "memory.profile_compress",
                {"window_no": window_no, "log_count": parsed.get("log_count")},
                priority=40,
            )
            profile_meta["profile_compression_pending_window"] = window_no
            profile_meta["profile_compression_pending_event_id"] = event_id
            metadata["profile"] = profile_meta
            self._write_core_metadata(metadata)
            continuation_service.kick()
            return event_id
        except Exception as exc:
            self._event("profile_compression_enqueue_failed", {"error": str(exc), "window_no": window_no})
            return None

    def _clear_profile_compression_pending(self) -> None:
        metadata = self._core_metadata()
        profile_meta = metadata.get("profile") if isinstance(metadata.get("profile"), dict) else {}
        profile_meta.pop("profile_compression_pending_window", None)
        profile_meta.pop("profile_compression_pending_event_id", None)
        metadata["profile"] = profile_meta
        self._write_core_metadata(metadata)

    def _build_profile_compression_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "profile": {
                "current_level": parsed.get("current_level"),
                "window_no": parsed.get("window_no"),
                "snapshot": parsed.get("snapshot"),
                "milestones": parsed.get("milestones"),
                "log_count": parsed.get("log_count"),
                "log": parsed.get("log"),
            },
            "level_definition": self._read_level_definition(str(parsed.get("current_level") or "Level 1")),
            "execution_evidence": self._profile_execution_evidence(),
            "output_contract": {
                "window_evaluation": "未达成|认知达成行为未跟上|晋级",
                "evaluation_reason": "一到两句话",
                "new_level": "Level 1",
                "snapshot": "覆盖后的完整人物志自然语言段落",
                "milestone_append": "无则为空字符串",
                "clear_log": True,
            },
        }

    def _read_level_definition(self, current_level: str) -> str:
        match = re.search(r"(\d+)", current_level)
        filename = f"level_{match.group(1)}.md" if match else "level_1.md"
        path = LEVELS_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")[:8000]
        return ""

    def _profile_execution_evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "transactions": [],
            "decisions": [],
            "current_positions": [],
            "behavior_classification": "passive_no_trade",
            "notes": [],
        }
        try:
            from backend.services.portfolio_ledger import portfolio_ledger_service

            tx_result = portfolio_ledger_service.list_transactions(limit=20)
            dec_result = portfolio_ledger_service.list_decisions(limit=20)
            evidence["transactions"] = tx_result.get("transactions", [])[:20] if isinstance(tx_result, dict) else []
            evidence["decisions"] = dec_result.get("decisions", [])[:20] if isinstance(dec_result, dict) else []
        except Exception as exc:
            evidence["notes"].append(f"ledger_unavailable: {exc}")
        try:
            from backend.tools.datahub import datahub_client

            positions = datahub_client.get_positions(timeout=5, use_cache=True)
            evidence["current_positions"] = positions[:20] if isinstance(positions, list) else positions
        except Exception as exc:
            evidence["notes"].append(f"positions_unavailable: {exc}")

        transactions = evidence.get("transactions") if isinstance(evidence.get("transactions"), list) else []
        decisions = evidence.get("decisions") if isinstance(evidence.get("decisions"), list) else []
        if transactions:
            evidence["behavior_classification"] = "transaction_evidence"
        elif any(str(row.get("status") or "") in {"draft", "confirmed", "tracking", "rejected", "expired"} for row in decisions if isinstance(row, dict)):
            evidence["behavior_classification"] = "active_hold_evidence"
        else:
            evidence["notes"].append("没有交易不自动扣分；若 LOG 显示用户有意识地选择持有/不冲动操作，应视为 active_hold_evidence。")
        return evidence

    def _parse_llm_json(self, raw_output: str) -> dict[str, Any]:
        text = str(raw_output or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError(f"profile compression did not return JSON: {text[:300]}")
            payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("profile compression JSON must be an object")
        return payload

    def _render_compressed_profile(self, previous: dict[str, Any], result: dict[str, Any]) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        old_window = int(previous.get("window_no") or 1)
        next_window = old_window + 1
        evaluation = str(result.get("window_evaluation") or "未达成").strip()
        reason = str(result.get("evaluation_reason") or "").strip()
        current_level = str(previous.get("current_level") or "Level 1").strip()
        new_level = str(result.get("new_level") or current_level).strip()
        snapshot = str(result.get("snapshot") or previous.get("snapshot") or "暂无稳定人物志。").strip()
        milestones = str(previous.get("milestones") or "<!-- 永久保留 -->").strip()
        milestone_append = str(result.get("milestone_append") or "").strip()
        if milestone_append and milestone_append not in milestones:
            milestones = milestones.rstrip() + "\n" + milestone_append
        evaluation_line = evaluation if not reason else f"{evaluation}（{reason}）"
        return (
            "# 用户画像\n\n"
            "## [LEVEL] 当前阶段\n"
            f"current_level: {new_level}\n"
            f"窗口计数：#{next_window}\n"
            f"本窗口晋级评估：{evaluation_line}\n\n"
            "## [SNAPSHOT] 人物志\n"
            f"<!-- 上次压缩：{today} | 窗口：#{old_window} -->\n"
            f"{snapshot}\n\n"
            "## [MILESTONES] 晋级记录\n"
            f"{milestones}\n\n"
            f"## [LOG] 当前评估窗口 #{next_window}\n"
            "<!-- 积累到8条后触发压缩，压缩后清空本节 -->\n"
            f"<!-- 条数：0/{PROFILE_LOG_THRESHOLD} -->\n"
        )

    def _metadata(self, file_type: str, content: str) -> dict[str, Any]:
        lines = content.splitlines()
        explicit = self._core_metadata().get(file_type, {})
        fallback_updated = self._file_modified_at(file_type) or self._legacy_last_updated(file_type, content)
        pending_count = len(
            [
                row
                for row in self._read_jsonl(CANDIDATE_FILES[file_type])
                if str(row.get("status") or "") == "pending"
            ]
        )
        base = {
            "last_updated": explicit.get("updated_at") or fallback_updated,
            "core_updated_at": explicit.get("updated_at") or fallback_updated,
            "created_at": explicit.get("created_at"),
            "updated_by": explicit.get("updated_by") or explicit.get("source"),
            "update_source": explicit.get("source"),
            "update_reason": explicit.get("reason"),
            "last_event_id": explicit.get("event_id"),
            "last_applied_candidate_id": explicit.get("last_applied_candidate_id"),
            "last_candidate_created_at": explicit.get("last_candidate_created_at"),
            "pending_candidate_count": pending_count,
            "file_size": len(content),
        }
        if file_type == "profile":
            parsed = self._parse_profile(content)
            count = 1 if parsed.get("snapshot") else 0
            return {
                "entry_count": count,
                "count": count,
                "log_count": parsed.get("log_count", 0),
                "current_level": parsed.get("current_level", "Level 1"),
                "window_no": parsed.get("window_no", 1),
                **base,
            }
        if file_type == "playbook":
            architecture = self._extract_playbook_architecture(content)
            count = len([line for line in architecture.splitlines() if re.match(r"^\s*维度\S*[：:]", line)])
            return {"chapter_count": count, "dimension_count": count, "count": count, **base}
        active_count = len([line for line in lines if re.match(r"^###\s+\[active\]", line, re.IGNORECASE)])
        watching_count = len([line for line in lines if re.match(r"^###\s+\[watching\]", line, re.IGNORECASE)])
        stale_count = len([line for line in lines if re.match(r"^###\s+\[stale\]", line, re.IGNORECASE)])
        return {
            "active_count": active_count,
            "watching_count": watching_count,
            "stale_count": stale_count,
            "count": active_count + watching_count,
            **base,
        }

    def _legacy_last_updated(self, file_type: str, content: str) -> str:
        if file_type == "profile":
            match = re.search(r"最后更新：(.+)", content)
            return match.group(1).strip() if match else "未知"
        if file_type == "playbook":
            match = re.search(r"(\d{4}-\d{2}-\d{2})：", content)
            return match.group(1) if match else "未知"
        match = re.search(r"创建.*?(\d{4}-\d{2}-\d{2})", content)
        return match.group(1) if match else "未知"

    def _file_modified_at(self, file_type: str) -> str | None:
        path = CORE_FILES.get(file_type)
        if not path or not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")

    def _append_core_entry(self, existing: str, candidate: dict[str, Any]) -> str:
        entry = self._render_core_entry(candidate)
        return existing.rstrip() + ("\n\n" if existing.strip() else "") + entry + "\n"

    def _render_core_entry(self, candidate: dict[str, Any]) -> str:
        metadata = {
            "memory_id": f"mem_{uuid4().hex[:12]}",
            "category": candidate.get("target"),
            "source": f"candidate:{candidate.get('candidate_id')}",
            "confidence": candidate.get("confidence"),
            "created_at": _now(),
            "updated_at": _now(),
            "operation": candidate.get("operation"),
            "source_session_id": candidate.get("source_session_id"),
            "source_message_id": candidate.get("source_message_id"),
        }
        header = f"<!-- finclaw-memory: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)} -->"
        return f"{header}\n{str(candidate.get('content') or '').strip()}"

    def _archive_core(self, file_type: str, old_content: str, reason: str) -> None:
        archive_path = ARCHIVE_FILES[file_type]
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_entry = f"\n\n---\n**归档时间**: {_now()}\n**原因**: {reason}\n\n{old_content}\n"
        with open(archive_path, "a", encoding="utf-8") as f:
            f.write(archive_entry)

    def _apply_archive_candidate(self, file_type: str, existing: str, candidate: dict[str, Any]) -> dict[str, str]:
        target = str(candidate.get("evidence") or candidate.get("content") or "").strip()
        if not target:
            raise ValueError("归档候选缺少目标内容")
        new_content, archived_entry = _remove_markdown_entry(existing, target)
        if not archived_entry:
            raise ValueError("归档失败：未在活跃记忆中找到匹配内容")
        self._archive_memory_entry(file_type, archived_entry, str(candidate.get("reason") or "candidate archive"), candidate)
        return {"content": new_content, "archived_entry": archived_entry}

    def _archive_memory_entry(self, file_type: str, entry: str, reason: str, candidate: dict[str, Any]) -> None:
        archive_path = ARCHIVE_FILES[file_type]
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if file_type == "convictions":
            rendered = _render_conviction_archive_entry(entry, reason, candidate)
        else:
            rendered = f"\n\n---\n**归档时间**: {_now()}\n**原因**: {reason}\n\n{entry.strip()}\n"
        with open(archive_path, "a", encoding="utf-8") as f:
            f.write(rendered)

    def _validate_conviction_candidate(self, content: str, operation: str) -> None:
        text = str(content or "").strip()
        if not text:
            raise ValueError("Convictions 候选内容不能为空")
        if operation in {"ARCHIVE", "CONFLICT", "NOOP"}:
            return
        required_labels = ("判断", "失效条件", "来源")
        missing = [label for label in required_labels if f"**{label}**" not in text and f"{label}：" not in text and f"{label}:" not in text]
        if missing:
            raise ValueError(f"写入 Convictions 必须包含字段：{', '.join(missing)}")
        action_pattern = re.compile(r"(建议|操作|策略|仓位|止损价|止盈|买入|卖出|清仓|减仓|加仓)")
        directive_pattern = re.compile(r"(建议|应该|应当|可以|不建议|需要).{0,12}(买入|卖出|清仓|减仓|加仓|止损|止盈)")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or "失效条件" in line or "复核触发" in line:
                continue
            if re.match(r"^[-*]\s*\*\*(建议|操作|策略|仓位|止损|止盈)", line):
                raise ValueError("Convictions 只记录投资判断，不记录具体买卖、仓位或止损操作")
            if action_pattern.search(line) and directive_pattern.search(line):
                raise ValueError("Convictions 只记录投资判断，不记录具体买卖、仓位或止损操作")

    def _validate_playbook_candidate(self, content: str, operation: str) -> None:
        text = str(content or "").strip()
        if not text:
            raise ValueError("Playbook 候选内容不能为空")
        if operation in {"ARCHIVE", "CONFLICT", "NOOP"}:
            return
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line.startswith("<!-- finclaw-memory:"):
            raise ValueError("Playbook 只保存干净的研究架构正文，不接受记忆元数据")
        if re.match(r"^\[?\d{4}-\d{2}-\d{2}\]?\s*[|：:]", first_line):
            raise ValueError("Playbook 不接受日期日志；请改写为当前研究架构的维度或问题")
        forbidden = (
            "研究流程提醒",
            "起源案例",
            "本轮研究",
            "质量等级",
            "关键 claim",
            "产出可复用分析框架",
            "报告摘要",
            "工具失败",
        )
        if any(term in text for term in forbidden):
            raise ValueError("Playbook 不保存案例、报告摘要、claim 校验或工具失败记录；只能保存研究架构改写")
        architecture_terms = ("研究架构", "研究框架", "维度", "检查", "问题", "关注", "删除", "合并", "改写", "重命名")
        if not any(term in text for term in architecture_terms):
            raise ValueError("Playbook 候选必须是研究架构修改：增加、删除、合并、重命名或改写维度/问题")

    def _apply_playbook_candidate(self, existing: str, candidate: dict[str, Any], operation: str) -> str:
        content = str(candidate.get("content") or "").strip()
        evidence = str(candidate.get("evidence") or "").strip()
        if not existing.strip():
            return self._normalize_playbook_document(content)
        if operation in {"UPDATE", "WEAKEN"}:
            if evidence and evidence in existing:
                return self._normalize_playbook_document(existing.replace(evidence, content, 1))
            raise ValueError("Playbook 更新失败：未找到要替换的研究架构片段，不能回退为追加")
        if "# 研究框架" in content or "## 当前研究架构" in content:
            return self._normalize_playbook_document(content)
        return self._append_to_playbook_architecture(existing, content)

    def _normalize_playbook_document(self, content: str) -> str:
        body = str(content or "").strip()
        body = re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", body).strip()
        if not body.startswith("# 研究框架"):
            if body.startswith("## 当前研究架构"):
                body = "# 研究框架\n\n" + body
            else:
                body = "# 研究框架\n\n## 当前研究架构\n\n" + body
        if "## 当前研究架构" not in body:
            body = body.rstrip() + "\n\n## 当前研究架构\n"
        return body.rstrip() + "\n"

    def _append_to_playbook_architecture(self, existing: str, addition: str) -> str:
        document = self._normalize_playbook_document(existing)
        lines = document.rstrip().splitlines()
        start = None
        for idx, line in enumerate(lines):
            if line.strip() == "## 当前研究架构":
                start = idx
                break
        if start is None:
            return self._normalize_playbook_document(addition)
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            if lines[idx].startswith("## "):
                end = idx
                break
        insert = ["", addition.strip(), ""]
        updated = lines[:end]
        if updated and updated[-1].strip():
            updated.append("")
        updated.extend(insert)
        updated.extend(lines[end:])
        return "\n".join(updated).rstrip() + "\n"

    def _extract_playbook_architecture(self, content: str) -> str:
        text = str(content or "")
        match = re.search(r"(?ms)^##\s+当前研究架构\s*$", text)
        if not match:
            return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", text).strip()
        start = match.start()
        next_heading = re.search(r"(?m)^##\s+", text[match.end() :])
        end = match.end() + next_heading.start() if next_heading else len(text)
        return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", text[start:end]).strip()

    def _find_related_segments(self, changed_text: str, file_type: str, content: str) -> list[dict[str, Any]]:
        changed_keywords = set(_keywords(changed_text))
        force_check = file_type in {"playbook", "convictions"} and _direction(changed_text) != "neutral"
        if not changed_keywords and not force_check:
            return []
        refs = []
        for segment in _segments(content):
            if not segment.strip():
                continue
            overlap = len(changed_keywords.intersection(_keywords(segment)))
            if overlap < 1 and not force_check:
                continue
            refs.append({"file": file_type, "content": segment[:1000], "reason": "同主题候选片段", "overlap": overlap})
            if len(refs) >= 8:
                break
        return refs

    def _heuristic_conflicting_segments(self, changed_text: str, candidate_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        changed_sentiment = _direction(changed_text)
        if changed_sentiment == "neutral":
            return []
        refs = []
        for item in candidate_segments:
            segment = str(item.get("content") or "")
            segment_sentiment = _direction(segment)
            if segment_sentiment != "neutral" and segment_sentiment != changed_sentiment:
                refs.append({**item, "reason": "同主题方向相反"})
            if len(refs) >= 5:
                break
        return refs

    def _llm_conflict_check(self, changed_file: str, changed_text: str, candidate_segments: list[dict[str, Any]]) -> dict[str, Any]:
        if not openai_stream_client.configured:
            return {"checked": False, "reason": "llm_not_configured"}
        payload = {
            "changed_file": changed_file,
            "changed_content": changed_text[:2200],
            "candidate_segments": [
                {
                    "file": item.get("file"),
                    "content": str(item.get("content") or "")[:1000],
                    "reason": item.get("reason"),
                }
                for item in candidate_segments[:8]
            ],
            "allowed_conflict_types": [
                "directional_change",
                "scope_change",
                "priority_change",
                "risk_boundary_change",
                "method_change",
                "factual_conflict",
                "no_conflict",
            ],
        }
        system_prompt = (
            "你是 FinClaw 的长期记忆冲突检测器。只判断新记忆是否与已有记忆存在语义冲突。\n"
            "规则：\n"
            "- 只输出严格 JSON，不要 Markdown。\n"
            "- 不要因为主题相同就判冲突；必须存在方向、范围、优先级、风险边界、方法论或事实的不一致。\n"
            "- 如果只是补充、细化、时间范围不同但不矛盾，has_conflict=false。\n"
            "- 不自动解决冲突，只给出判断理由。\n"
            "输出 schema：\n"
            "{\"has_conflict\": boolean, \"severity\": \"high|medium|low|none\", "
            "\"conflict_type\": \"directional_change|scope_change|priority_change|risk_boundary_change|method_change|factual_conflict|no_conflict\", "
            "\"reason\": string, \"conflicting_segments\": [{\"file\": string, \"content\": string, \"reason\": string}]}"
        )
        try:
            chunks: list[str] = []
            for chunk in openai_stream_client.stream_chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
                tools=[],
            ):
                if chunk.content:
                    chunks.append(chunk.content)
            result = _extract_json_object("".join(chunks))
            if not isinstance(result, dict):
                return {"checked": False, "reason": "invalid_llm_json"}
            has_conflict = bool(result.get("has_conflict"))
            severity = str(result.get("severity") or ("medium" if has_conflict else "none"))
            conflict_type = str(result.get("conflict_type") or ("semantic_conflict" if has_conflict else "no_conflict"))
            refs = result.get("conflicting_segments")
            if not isinstance(refs, list):
                refs = []
            return {
                "checked": True,
                "has_conflict": has_conflict,
                "severity": severity,
                "conflict_type": conflict_type,
                "reason": str(result.get("reason") or ""),
                "conflicting_segments": [item for item in refs if isinstance(item, dict)][:5],
            }
        except Exception as exc:
            return {"checked": False, "reason": str(exc)[:500]}

    def _record_conflict(
        self,
        source: str,
        changed_file: str,
        changed_content: str,
        conflicts_with: list[dict[str, Any]],
        reason: str,
        severity: str = "medium",
        conflict_type: str = "semantic_conflict",
        llm_reason: str = "",
    ) -> dict[str, Any]:
        conflict = {
            "conflict_id": f"mem_conflict_{uuid4().hex[:12]}",
            "source": source,
            "changed_file": changed_file,
            "changed_content": changed_content,
            "conflicts_with": conflicts_with,
            "status": "pending",
            "reason": reason,
            "severity": severity,
            "conflict_type": conflict_type,
            "llm_reason": llm_reason,
            "created_at": _now(),
            "resolved_at": None,
        }
        rows = self._read_jsonl(PENDING_CONFLICTS_FILE)
        rows.append(conflict)
        self._write_jsonl(PENDING_CONFLICTS_FILE, rows)
        self._event("conflict_created", {"conflict_id": conflict["conflict_id"], "changed_file": changed_file})
        return conflict

    def _find_candidate(self, candidate_id: str) -> tuple[str, list[dict[str, Any]], int]:
        for target, path in CANDIDATE_FILES.items():
            rows = self._read_jsonl(path)
            for idx, row in enumerate(rows):
                if row.get("candidate_id") == candidate_id:
                    return target, rows, idx
        raise KeyError(f"unknown candidate: {candidate_id}")

    def _all_candidates(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in CANDIDATE_FILES.values():
            rows.extend(self._read_jsonl(path))
        return rows

    def _event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": f"mem_evt_{uuid4().hex[:12]}",
            "event_type": event_type,
            "summary": _summarize_payload(payload),
            "payload": payload,
            "created_at": _now(),
        }
        rows = self._read_jsonl(EVENT_FILE)
        rows.append(event)
        self._write_jsonl(EVENT_FILE, rows[-500:])
        return event

    def _core_metadata(self) -> dict[str, Any]:
        if not CORE_METADATA_FILE.exists():
            return {}
        try:
            payload = json.loads(CORE_METADATA_FILE.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_core_metadata(self, payload: dict[str, Any]) -> None:
        CORE_METADATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _touch_core_metadata(
        self,
        file_type: str,
        source: str,
        reason: str,
        event_id: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        now = _now()
        payload = self._core_metadata()
        current = payload.get(file_type) if isinstance(payload.get(file_type), dict) else {}
        current = {
            **current,
            "created_at": current.get("created_at") or now,
            "updated_at": now,
            "updated_by": source,
            "source": source,
            "reason": reason,
            "event_id": event_id,
        }
        if candidate_id:
            current["last_applied_candidate_id"] = candidate_id
        payload[file_type] = current
        self._write_core_metadata(payload)

    def _touch_candidate_metadata(self, file_type: str, candidate: dict[str, Any]) -> None:
        payload = self._core_metadata()
        current = payload.get(file_type) if isinstance(payload.get(file_type), dict) else {}
        current = {
            **current,
            "created_at": current.get("created_at") or _now(),
            "last_candidate_created_at": candidate.get("created_at") or _now(),
            "last_candidate_id": candidate.get("candidate_id"),
        }
        payload[file_type] = current
        self._write_core_metadata(payload)

    def _ensure_dirs(self) -> None:
        for path in (MEMORY_DIR, ARCHIVE_DIR, CANDIDATE_DIR, CONFLICT_DIR, EVENT_DIR):
            path.mkdir(parents=True, exist_ok=True)
        for path in CANDIDATE_FILES.values():
            if not path.exists():
                path.write_text("", encoding="utf-8")
        for path in (PENDING_CONFLICTS_FILE, RESOLVED_CONFLICTS_FILE, EVENT_FILE):
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except Exception:
                continue
        return rows

    def _write_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in rows) + ("\n" if rows else ""), encoding="utf-8")
        tmp.replace(path)


def _validate_file_type(value: str) -> str:
    file_type = str(value or "").strip()
    if file_type not in CORE_FILES:
        raise ValueError("无效的文件类型")
    return file_type


def _normalize_operation(value: Any) -> str:
    operation = str(value or "ADD").strip().upper()
    return operation if operation in {"ADD", "UPDATE", "WEAKEN", "ARCHIVE", "CONFLICT", "NOOP"} else "ADD"


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        number = 0.5
    return min(1.0, max(0.0, number))


def _segments(content: str) -> list[str]:
    parts = re.split(r"\n(?=#{1,4}\s)|\n\s*[-*]\s+", content)
    return [part.strip() for part in parts if part.strip()]


def _remove_markdown_entry(content: str, fragment: str) -> tuple[str, str]:
    text = str(content or "")
    needle = str(fragment or "").strip()
    if not needle:
        return text, ""
    pos = text.find(needle)
    if pos < 0:
        compact_needle = re.sub(r"\s+", " ", needle)
        for match in re.finditer(r"(?ms)(?:<!-- finclaw-memory:.*?-->\s*)?###\s+\[[^\]]+\].*?(?=\n(?:<!-- finclaw-memory:|###\s+\[)|\Z)", text):
            block = match.group(0).strip()
            if compact_needle and compact_needle[:160] in re.sub(r"\s+", " ", block):
                start, end = match.span()
                return (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).rstrip() + "\n", block
        return text, ""

    start = text.rfind("\n<!-- finclaw-memory:", 0, pos)
    if start < 0:
        start = text.rfind("\n### ", 0, pos)
    if start < 0:
        start = text.rfind("\n## ", 0, pos)
    start = 0 if start < 0 else start + 1

    next_meta = text.find("\n<!-- finclaw-memory:", pos + len(needle))
    next_heading = text.find("\n### ", pos + len(needle))
    candidates = [idx for idx in (next_meta, next_heading) if idx >= 0]
    end = min(candidates) if candidates else len(text)
    archived = text[start:end].strip()
    remaining = (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).rstrip()
    return (remaining + "\n" if remaining else ""), archived


def _extract_markdown_field(content: str, label: str) -> str:
    pattern = re.compile(rf"(?ms)^-\s+\*\*{re.escape(label)}\*\*\s*[：:]\s*(.*?)(?=^\-\s+\*\*|\Z)")
    match = pattern.search(str(content or ""))
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1).strip())[:600]


def _render_conviction_archive_entry(entry: str, reason: str, candidate: dict[str, Any]) -> str:
    body = str(entry or "").strip()
    title_match = re.search(r"^###\s+\[[^\]]+\]\s*(.+)$", body, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "未命名投资判断"
    judgement = _extract_markdown_field(body, "判断")
    source = _extract_markdown_field(body, "来源")
    invalidation = _extract_markdown_field(body, "失效条件")
    lines = [
        "",
        "",
        "---",
        f"### [invalidated] {title}",
        "",
        f"- **原判断**：{judgement or title}",
        f"- **失效原因**：{str(reason or '').strip() or '用户确认归档'}",
        f"- **触发证据**：{str(candidate.get('evidence') or candidate.get('content') or '').strip()[:800]}",
        f"- **原失效条件**：{invalidation or '未提取'}",
        f"- **来源**：{source or '未提取'}",
        f"- **归档日期**：{_now()}",
        "",
    ]
    return "\n".join(lines)


def _keywords(text: str) -> list[str]:
    candidates = re.findall(r"[A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}", text)
    stop = {"用户", "主线", "判断", "原则", "当前", "长期", "这个", "一个", "进行", "已经", "需要"}
    return [word for word in candidates if word not in stop][:40]


def _direction(text: str) -> str:
    negative = sum(1 for term in NEGATIVE_TERMS if term in text)
    positive = sum(1 for term in POSITIVE_TERMS if term in text)
    if negative > positive:
        return "negative"
    if positive > negative:
        return "positive"
    return "neutral"


def _summarize_payload(payload: dict[str, Any]) -> str:
    if "candidate_id" in payload:
        return f"candidate {payload.get('candidate_id')}"
    if "conflict_id" in payload:
        return f"conflict {payload.get('conflict_id')}"
    if "file" in payload:
        return f"core {payload.get('file')} updated"
    return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM conflict output must be a JSON object")
    return data


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


long_term_memory_service = LongTermMemoryService()
