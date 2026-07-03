"""
记忆系统工具实现
提供 4 个核心工具：memory_read, memory_write, memory_update, memory_archive
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.services.long_term_memory import long_term_memory_service

# 记忆文件路径
MEMORY_DIR = DATA_DIR / "memory"
ARCHIVE_DIR = MEMORY_DIR / "archive"
LOG_FILE = MEMORY_DIR / "memory_tool_log.jsonl"
METADATA_PREFIX = "<!-- finclaw-memory:"
METADATA_SUFFIX = "-->"
DEFAULT_TTL_DAYS = {
    "profile": 3650,
    "playbook": 3650,
    "convictions": 180,
}
DEFAULT_DECAY_WEIGHT = {
    "profile": 0.05,
    "playbook": 0.05,
    "convictions": 0.25,
}
DEFAULT_CONFIDENCE = {
    "profile": 0.86,
    "playbook": 0.82,
    "convictions": 0.9,
}

# 文件映射
FILE_MAP = {
    "profile": MEMORY_DIR / "profile.md",
    "playbook": MEMORY_DIR / "playbook.md",
    "convictions": MEMORY_DIR / "convictions.md",
}

FILE_LABELS = {
    "profile": "用户画像",
    "playbook": "研究框架",
    "convictions": "当前投资判断",
}

ARCHIVE_MAP = {
    "profile": ARCHIVE_DIR / "profile_archive.md",
    "playbook": ARCHIVE_DIR / "playbook_archive.md",
    "convictions": ARCHIVE_DIR / "convictions_archive.md",
}


def _log_tool_call(tool: str, params: dict, caller: str, reason: str, result: str):
    """记录工具调用日志"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "tool": tool,
        "params": params,
        "caller": caller,
        "reason": reason,
        "result": result,
    }

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _coerce_float(value: Any, default: float) -> float:
    try:
        number = float(value)
        if 0.0 <= number <= 1.0:
            return number
    except Exception:
        pass
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except Exception:
        pass
    return default


def _extract_memory_metadata(content: str) -> dict[str, Any] | None:
    text = str(content or "").lstrip()
    if not text.startswith(METADATA_PREFIX):
        return None
    head, _, _ = text.partition(METADATA_SUFFIX)
    payload = head[len(METADATA_PREFIX):].strip()
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _normalize_metadata(
    file: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
    operation: str = "write",
) -> dict[str, Any]:
    payload = dict(metadata or {})
    now = _now()
    existing_created_at = payload.get("created_at")
    result = {
        "memory_id": str(payload.get("memory_id") or f"mem_{uuid4().hex[:12]}"),
        "category": str(payload.get("category") or file),
        "source": str(payload.get("source") or "agent"),
        "operation": operation,
        "reason": str(payload.get("reason") or reason),
        "created_at": str(existing_created_at or now),
        "updated_at": now,
        "confidence": _coerce_float(payload.get("confidence"), DEFAULT_CONFIDENCE.get(file, 0.8)),
        "ttl_days": _coerce_int(payload.get("ttl_days"), DEFAULT_TTL_DAYS.get(file, 180)),
        "decay_weight": _coerce_float(payload.get("decay_weight"), DEFAULT_DECAY_WEIGHT.get(file, 0.2)),
    }
    for key in ("session_id", "source_message_id", "trigger", "file", "note"):
        if key in payload and payload.get(key) is not None:
            result[key] = payload.get(key)
    return result


def _render_memory_entry(content: str, metadata: dict[str, Any]) -> str:
    body = str(content or "").strip()
    if not body:
        return ""
    if body.lstrip().startswith(METADATA_PREFIX):
        return body
    header = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    return f"{METADATA_PREFIX} {header} {METADATA_SUFFIX}\n{body}"


def _merge_memory_metadata(existing: dict[str, Any] | None, override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    merged.update({k: v for k, v in override.items() if v is not None})
    return merged


def _extract_section(content: str, section: str) -> Optional[str]:
    """从 markdown 内容中提取指定章节"""
    # 匹配章节标题（支持多级标题）
    pattern = rf"^(#+)\s+{re.escape(section)}\s*$"
    lines = content.split("\n")

    start_idx = None
    start_level = None

    for i, line in enumerate(lines):
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            start_idx = i
            start_level = len(match.group(1))
            break

    if start_idx is None:
        return None

    # 找到下一个同级或更高级标题
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.match(r"^#{1," + str(start_level) + r"}\s+", lines[i]):
            end_idx = i
            break

    return "\n".join(lines[start_idx:end_idx])


def _extract_current_research_architecture(content: str) -> str:
    text = str(content or "")
    match = re.search(r"(?ms)^##\s+当前研究架构\s*$", text)
    if not match:
        return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", text).strip()
    next_heading = re.search(r"(?m)^##\s+", text[match.end():])
    end = match.end() + next_heading.start() if next_heading else len(text)
    section_text = text[match.start():end]
    return re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", section_text).strip()


def memory_read(
    file: Literal["profile", "playbook", "convictions"],
    section: Optional[str] = None
) -> dict:
    """
    读取记忆文件

    Args:
        file: 记忆文件类型
        section: 可选的章节名称，如 "1.1 产业链分析"

    Returns:
        {"content": str, "success": bool, "message": str}
    """
    try:
        if file == "profile" and not section:
            profile_context = long_term_memory_service.read_profile_context()
            content = str(profile_context.get("content") or "")
            _log_tool_call(
                tool="memory_read",
                params={"file": file, "section": section, "mode": "snapshot_only"},
                caller="agent",
                reason="读取用户画像快照",
                result="success",
            )
            return {
                "success": True,
                "message": "成功读取 profile 快照（不含 LOG）",
                "content": content,
            }

        file_path = FILE_MAP[file]

        if not file_path.exists():
            return {
                "success": False,
                "message": f"记忆文件 {file} 不存在",
                "content": ""
            }

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        if section:
            section_content = _extract_section(content, section)
            if section_content is None:
                return {
                    "success": False,
                    "message": f"章节 '{section}' 未找到",
                    "content": ""
                }
            content = section_content
        elif file == "playbook":
            content = _extract_current_research_architecture(content)

        _log_tool_call(
            tool="memory_read",
            params={"file": file, "section": section},
            caller="agent",
            reason="读取记忆",
            result="success"
        )

        return {
            "success": True,
            "message": f"成功读取 {file}" + (f" 章节 {section}" if section else ""),
            "content": content
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"读取失败：{str(e)}",
            "content": ""
        }


def _apply_profile_without_confirmation(
    *,
    content: str,
    evidence: str,
    confidence: float,
    operation: str,
    reason: str,
    metadata: Optional[dict[str, Any]],
    tool_name: str,
    related_refs: list[dict[str, Any]],
) -> dict:
    result = long_term_memory_service.apply_agent_profile_entry(
        content=content,
        evidence=evidence,
        confidence=confidence,
        operation=operation,
        reason=reason,
        source_session_id=(metadata or {}).get("session_id"),
        source_message_id=(metadata or {}).get("source_message_id"),
        related_refs=related_refs,
    )
    entry = result.get("entry") or {}
    _log_tool_call(
        tool=tool_name,
        params={
            "file": "profile",
            "content_length": len(content),
            "entry_id": entry.get("candidate_id"),
        },
        caller="agent",
        reason=reason,
        result="memory_applied",
    )
    return {
        "success": True,
        "status": "memory_applied",
        "message": "已自动更新「用户画像」",
        "entry_id": entry.get("candidate_id"),
        "target": "profile",
        "entry": entry,
        "conflicts": result.get("conflicts", []),
    }


def memory_write(
    file: Literal["profile", "playbook", "convictions"],
    content: str,
    reason: str,
    position: str = "append",
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """
    写入记忆文件

    Args:
        file: 记忆文件类型
        content: 要写入的内容
        reason: 写入原因
        position: 写入位置，"append" 或 "section:章节标题"

    Returns:
        {"success": bool, "message": str}
    """
    try:
        if file == "profile":
            return _apply_profile_without_confirmation(
                content=content,
                evidence="",
                confidence=(metadata or {}).get("confidence", DEFAULT_CONFIDENCE.get(file, 0.8)),
                operation="ADD",
                reason=reason,
                metadata=metadata,
                tool_name="memory_write",
                related_refs=[{"tool": "memory_write", "position": position}],
            )
        candidate = long_term_memory_service.create_candidate(
            target=file,
            content=content,
            evidence="",
            confidence=(metadata or {}).get("confidence", DEFAULT_CONFIDENCE.get(file, 0.8)),
            operation="ADD",
            reason=reason,
            source_session_id=(metadata or {}).get("session_id"),
            source_message_id=(metadata or {}).get("source_message_id"),
            related_refs=[{"tool": "memory_write", "position": position}],
        )
        _log_tool_call(
            tool="memory_write",
            params={
                "file": file,
                "position": position,
                "content_length": len(content),
                "candidate_id": candidate.get("candidate_id"),
            },
            caller="agent",
            reason=reason,
            result="candidate_created"
        )

        return {
            "success": True,
            "status": "memory_candidate_created",
            "message": f"已生成「{FILE_LABELS.get(file, file)}」记忆候选，等待用户在右栏确认",
            "candidate_id": candidate.get("candidate_id"),
            "target": file,
            "candidate": candidate,
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"写入失败：{str(e)}"
        }


def memory_update(
    file: Literal["profile", "playbook", "convictions"],
    target: str,
    new_content: str,
    reason: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """
    更新记忆文件中的某段内容

    Args:
        file: 记忆文件类型
        target: 要替换的原文片段
        new_content: 新内容
        reason: 修改原因

    Returns:
        {"success": bool, "message": str}
    """
    try:
        if file == "profile":
            return _apply_profile_without_confirmation(
                content=new_content,
                evidence=target[:1200],
                confidence=(metadata or {}).get("confidence", 0.88),
                operation="UPDATE",
                reason=reason,
                metadata=metadata,
                tool_name="memory_update",
                related_refs=[{"tool": "memory_update"}],
            )
        candidate = long_term_memory_service.create_candidate(
            target=file,
            content=new_content,
            evidence=target[:1200],
            confidence=(metadata or {}).get("confidence", 0.88),
            operation="UPDATE",
            reason=reason,
            source_session_id=(metadata or {}).get("session_id"),
            source_message_id=(metadata or {}).get("source_message_id"),
            related_refs=[{"tool": "memory_update"}],
        )
        _log_tool_call(
            tool="memory_update",
            params={
                "file": file,
                "target_length": len(target),
                "new_length": len(new_content),
                "candidate_id": candidate.get("candidate_id"),
            },
            caller="agent",
            reason=reason,
            result="candidate_created"
        )

        return {
            "success": True,
            "status": "memory_candidate_created",
            "message": f"已生成「{FILE_LABELS.get(file, file)}」更新候选，等待用户在右栏确认",
            "candidate_id": candidate.get("candidate_id"),
            "target": file,
            "candidate": candidate,
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"更新失败：{str(e)}"
        }


def memory_archive(
    file: Literal["profile", "playbook", "convictions"],
    target: str,
    reason: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """
    将内容从活跃文件移到归档文件

    Args:
        file: 记忆文件类型
        target: 要归档的原文片段
        reason: 归档原因

    Returns:
        {"success": bool, "message": str}
    """
    try:
        if file == "profile":
            return _apply_profile_without_confirmation(
                content=target,
                evidence=target[:1200],
                confidence=(metadata or {}).get("confidence", 0.9),
                operation="CONFLICT",
                reason=reason,
                metadata=metadata,
                tool_name="memory_archive",
                related_refs=[{"tool": "memory_archive"}],
            )
        candidate = long_term_memory_service.create_candidate(
            target=file,
            content=target,
            evidence=target[:1200],
            confidence=(metadata or {}).get("confidence", 0.9),
            operation="ARCHIVE",
            reason=reason,
            source_session_id=(metadata or {}).get("session_id"),
            source_message_id=(metadata or {}).get("source_message_id"),
            related_refs=[{"tool": "memory_archive"}],
        )
        _log_tool_call(
            tool="memory_archive",
            params={
                "file": file,
                "target_length": len(target),
                "candidate_id": candidate.get("candidate_id"),
            },
            caller="agent",
            reason=reason,
            result="candidate_created"
        )

        return {
            "success": True,
            "status": "memory_candidate_created",
            "message": f"已生成「{FILE_LABELS.get(file, file)}」归档/冲突候选，等待用户在右栏确认",
            "candidate_id": candidate.get("candidate_id"),
            "target": file,
            "candidate": candidate,
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"归档失败：{str(e)}"
        }
