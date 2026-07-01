from __future__ import annotations

from typing import Any

from backend.services.research_threads import research_thread_service


def _thread_next_actions(thread: dict[str, Any]) -> list[dict[str, Any]]:
    thread_id = str(thread.get("thread_id") or "")
    status = str(thread.get("status") or "")
    record_id = str(thread.get("record_id") or research_thread_service.record_id_for_thread(thread))
    if status in {"pending", "in_progress", "paused", "waiting_approval"}:
        return [
            {
                "tool": "get_research_thread",
                "arguments": {"thread_id": thread_id},
                "reason": "查看后台研究进度；不要重复启动同一研究线程。",
            }
        ]
    if status == "failed":
        return [
            {
                "tool": "get_research_thread",
                "arguments": {"thread_id": thread_id},
                "reason": "读取失败原因和已完成证据，再决定是否恢复。",
            },
            {
                "tool": "control_research_thread",
                "arguments": {"thread_id": thread_id, "action": "resume"},
                "reason": "用户明确要求继续时恢复失败步骤。",
            },
        ]
    if status == "completed":
        return [
            {
                "tool": "read_research_record",
                "arguments": {"record_id": record_id},
                "reason": "读取研究档案默认视图：研究摘要、待验证判断和目录。",
            },
        ]
    return []


def start_research_thread(
    subject: str,
    subject_type: str = "unknown",
    depth: str = "standard",
    user_goal: str = "",
    research_goal: str = "",
    subject_hint: str = "",
    scope_hint: str = "",
    budget_profile: str = "",
    allowed_tools: list[str] | None = None,
    blocked_tools: list[str] | None = None,
    constraints: str = "",
    session_id: str = "default",
    force_new: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    thread = research_thread_service.start_thread(
        subject=subject,
        subject_type=subject_type,
        depth=depth,
        session_id=session_id,
        user_goal=user_goal,
        research_goal=research_goal,
        subject_hint=subject_hint,
        scope_hint=scope_hint,
        budget_profile=budget_profile,
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
        constraints=constraints,
        auto_start=True,
        force_new=force_new,
    )
    reused = bool(thread.get("reused_existing"))
    compact = research_thread_service.compact_thread(thread)
    compact_status = str(compact.get("status") or "")
    if reused and compact_status == "completed":
        message = "已复用同一会话中的已完成研究线程；不要说仍在进行。下一步读取 next_actions 中的研究档案。"
    elif reused:
        message = f"已复用同一会话中的既有研究线程（{thread.get('reuse_reason') or 'matched'}），后续用 get_research_thread 查看进度；如需重跑请明确 force_new=true。"
    else:
        message = "研究线程已创建并在后台执行。后续用 get_research_thread 查看进度，不要重复启动同一研究。"
    return {
        "status": "research_thread_reused" if reused else "research_thread_started",
        "thread": compact,
        "detail": "summary",
        "next_actions": _thread_next_actions(compact),
        "message": message,
    }


def get_research_thread(
    thread_id: str = "",
    session_id: str = "default",
    status: str = "",
    subject: str = "",
    limit: int = 10,
    detail: str = "summary",
) -> dict[str, Any]:
    if thread_id:
        thread = research_thread_service.get_thread(thread_id)
        compact = research_thread_service.compact_thread(thread)
        return {
            "status": "research_thread",
            "thread": compact,
            "detail": "summary",
            "next_actions": _thread_next_actions(compact),
            "message": "线程已完成；读取 next_actions 中的研究档案。" if str(compact.get("status") or "") == "completed" else "默认返回紧凑线程；需要正文级研究内容时优先读取研究档案章节，避免请求完整线程。",
        }
    payload = research_thread_service.list_threads(
        session_id=session_id,
        status=status or None,
        subject=subject or None,
        limit=limit,
        detail="summary",
    )
    return {
        "status": "research_thread_list",
        **payload,
        "detail": "summary",
        "message": "选择 thread_id 后查看进度；completed 线程应读取对应研究档案章节而非请求完整线程。",
    }


def control_research_thread(thread_id: str, action: str) -> dict[str, Any]:
    payload = {
        "status": "research_thread_controlled",
        **research_thread_service.control_thread(thread_id, action),
    }
    if isinstance(payload.get("thread"), dict):
        payload["thread"] = research_thread_service.compact_thread(payload["thread"])
        payload["next_actions"] = _thread_next_actions(payload["thread"])
    payload["detail"] = "summary"
    return payload


def read_research_record(
    record_id: str = "",
    subject_type: str = "",
    query: str = "",
    section: str = "",
    offset: int = 0,
    max_chars: int = 6000,
    limit: int = 20,
) -> dict[str, Any]:
    if not record_id:
        return {
            "status": "research_record_list",
            **research_thread_service.list_records(subject_type=subject_type or None, query=query or None, limit=limit),
            "message": "选择 record_id 后，可按 section + offset 分页读取研究档案；query 可用于启动新研究前检索相关旧研究。",
        }
    return {
        "status": "research_record_detail",
        **research_thread_service.get_record(
            record_id=record_id,
            section=section or None,
            offset=offset,
            max_chars=max_chars,
        ),
    }
