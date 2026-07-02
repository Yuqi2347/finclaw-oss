"""
会话结束钩子 - 自动扫描会话并更新记忆
"""

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Any

from backend.core.config import DATA_DIR
from backend.core.openai_stream import openai_stream_client
from backend.services.sessions import chat_session_store
from backend.tools import memory_tools
from backend.services.long_term_memory import long_term_memory_service


HOOK_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "memory" / "session_hook_prompt.md"
HOOK_LOG_FILE = DATA_DIR / "memory" / "memory_hook_log.jsonl"
HOOK_MESSAGE_LIMIT = 100
HOOK_SCAN_LIMIT = 200
HOOK_BUFFER_TARGET_TURNS = 5
HOOK_TOTAL_CONTENT_BUDGET = 16_000
HOOK_TOOL_RESULT_KEY_BUDGET = 4_000
EXPLICIT_MEMORY_KEYWORDS = (
    "记住",
    "帮我记录",
    "记录下来",
    "记录到记忆",
    "加入记忆",
    "加入长期记忆",
    "写入记忆",
    "以后参考",
    "以后提醒",
    "下次提醒",
    "下次验证",
    "下周验证",
    "长期记忆",
)
INVESTMENT_CONTEXT_KEYWORDS = (
    "投资",
    "股票",
    "标的",
    "持仓",
    "仓位",
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "止损",
    "止盈",
    "风险",
    "收益",
    "估值",
    "基本面",
    "财务",
    "主线",
    "行业",
    "产业链",
    "交易",
    "复盘",
    "策略",
    "框架",
    "playbook",
    "共识",
    "认知",
    "原则",
)
NON_MEMORY_CONTEXT_KEYWORDS = (
    "前端",
    "后端",
    "接口",
    "报错",
    "日志",
    "按钮",
    "样式",
    "卡片",
    "进程",
    "重启",
    "npm",
    "python",
    "代码",
    "实现",
    "修改",
    "删除",
    "清理",
    "测试",
)
MEMORY_TOOL_ALIASES = {
    "memory.read": "memory_read",
    "memory.write": "memory_write",
    "memory.update": "memory_update",
    "memory.archive": "memory_archive",
}
MEMORY_TOOL_NAMES = set(MEMORY_TOOL_ALIASES.values())
WRITE_MEMORY_TOOL_NAMES = {"memory_write", "memory_update", "memory_archive"}


def _normalize_memory_tool_name(tool_name: str | None) -> str:
    name = str(tool_name or "")
    return MEMORY_TOOL_ALIASES.get(name, name)


def _log_hook_run(event: dict[str, Any]) -> None:
    try:
        payload = {
            "timestamp": _now(),
            **event,
        }
        HOOK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HOOK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _has_explicit_memory_intent(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        if any(keyword in content for keyword in EXPLICIT_MEMORY_KEYWORDS):
            return True
    return False


def _has_complete_user_assistant_turn(messages: list[dict[str, Any]]) -> bool:
    has_user_turn = any(msg.get("role") == "user" and str(msg.get("content") or "").strip() for msg in messages)
    has_assistant_turn = any(msg.get("role") == "assistant" for msg in messages)
    return has_user_turn and has_assistant_turn


def _has_investment_context(messages: list[dict[str, Any]]) -> bool:
    text = "\n".join(
        str(msg.get("content") or "")
        for msg in messages
        if msg.get("role") in {"user", "assistant"}
    )
    return any(keyword in text for keyword in INVESTMENT_CONTEXT_KEYWORDS)


def _is_non_memory_context(messages: list[dict[str, Any]]) -> bool:
    user_text = "\n".join(
        str(msg.get("content") or "")
        for msg in messages
        if msg.get("role") == "user"
    )
    if not user_text:
        return False
    has_non_memory = any(keyword in user_text for keyword in NON_MEMORY_CONTEXT_KEYWORDS)
    return has_non_memory and not _has_investment_context(messages)


def _memory_write_status(messages: list[dict[str, Any]]) -> tuple[bool, bool]:
    has_success = False
    has_failure = False
    for msg in messages:
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_name = _normalize_memory_tool_name(str(tool_call.get("tool") or ""))
            if tool_name not in WRITE_MEMORY_TOOL_NAMES:
                continue
            result = tool_call.get("result")
            if isinstance(result, dict):
                if result.get("success") is False:
                    has_failure = True
                else:
                    has_success = True
            elif result is None:
                has_failure = True
            else:
                has_success = True
    return has_success, has_failure


def _classify_memory_hook_increment(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        return {"decision": "skip", "reason": "no_new_messages"}
    if not _has_complete_user_assistant_turn(messages):
        return {"decision": "skip", "reason": "no_complete_user_assistant_turn"}

    memory_write_success, memory_write_failure = _memory_write_status(messages)
    if memory_write_success:
        return {"decision": "skip", "reason": "memory_tool_already_succeeded"}
    if memory_write_failure:
        return {"decision": "strong", "reason": "memory_tool_failed"}
    if _has_explicit_memory_intent(messages):
        return {"decision": "strong", "reason": "explicit_memory_intent"}
    if _is_non_memory_context(messages):
        return {"decision": "skip", "reason": "non_memory_context"}
    if _has_investment_context(messages):
        return {"decision": "weak", "reason": "investment_context_buffered"}
    return {"decision": "skip", "reason": "no_memory_signal"}


def should_trigger_memory_hook(session_id: str) -> bool:
    """
    判断是否应该触发记忆钩子（二次筛选）

    兼容旧入口：只有强触发或弱触发 buffer 满 5 轮才会实际调用 LLM。
    """
    try:
        hook_state = chat_session_store.get_memory_hook_state(session_id)
        last_hook_message_id = int(hook_state.get("last_memory_hook_message_id") or 0)
        scan_message_id = int(hook_state.get("memory_hook_scan_message_id") or 0)
        baseline_message_id = max(last_hook_message_id, scan_message_id)
        latest_message_id = chat_session_store.latest_message_id(session_id)
        if latest_message_id <= baseline_message_id:
            return False

        messages = chat_session_store.list_messages(session_id, after_id=baseline_message_id, limit=HOOK_SCAN_LIMIT)
        if not messages:
            return False
        decision = _classify_memory_hook_increment(messages)
        if decision.get("decision") == "strong":
            return True
        if decision.get("decision") == "weak":
            buffer_count = int(hook_state.get("memory_hook_buffer_count") or 0)
            return buffer_count + 1 >= HOOK_BUFFER_TARGET_TURNS
        return False
    except Exception:
        # 出错时不触发
        return False


def trigger_memory_hook(session_id: str, after_message_id: int | None = None, trigger_reason: str = "session_end") -> dict[str, Any]:
    """
    触发会话结束钩子，审视会话并更新记忆

    Returns:
        {"success": bool, "actions_taken": int, "message": str}
    """
    try:
        if not openai_stream_client.configured:
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "status": "skipped",
                "reason": "llm_not_configured",
            })
            return {
                "success": False,
                "actions_taken": 0,
                "message": "记忆钩子未执行：LLM 未配置",
            }

        hook_state = chat_session_store.get_memory_hook_state(session_id)
        last_hook_message_id = int(hook_state.get("last_memory_hook_message_id") or 0)
        scan_message_id = int(hook_state.get("memory_hook_scan_message_id") or 0)
        baseline_message_id = max(last_hook_message_id, scan_message_id)
        explicit_after_id = _coerce_int(after_message_id)
        if explicit_after_id is not None and explicit_after_id < baseline_message_id:
            baseline_message_id = explicit_after_id

        latest_message_id = chat_session_store.latest_message_id(session_id)
        if latest_message_id <= baseline_message_id:
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "baseline_message_id": baseline_message_id,
                "latest_message_id": latest_message_id,
                "status": "skipped",
                "reason": "no_new_messages",
            })
            return {
                "success": True,
                "actions_taken": 0,
                "message": "没有新的会话增量需要提取",
            }

        # 加载本次尚未扫描的消息；扫描位点独立于已完成提取位点，避免 weak buffer 丢消息。
        messages = chat_session_store.list_messages(session_id, after_id=baseline_message_id, limit=HOOK_SCAN_LIMIT)
        if not messages:
            chat_session_store.set_memory_hook_scan(session_id, latest_message_id)
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "baseline_message_id": baseline_message_id,
                "latest_message_id": latest_message_id,
                "status": "skipped",
                "reason": "no_new_messages",
            })
            return {
                "success": True,
                "actions_taken": 0,
                "message": "没有新的会话增量需要提取"
            }

        first_message_id = int(messages[0].get("message_id") or 0)
        current_range = {
            "start_message_id": first_message_id,
            "end_message_id": latest_message_id,
            "reason": "",
            "created_at": _now(),
        }
        decision = _classify_memory_hook_increment(messages)
        decision_type = str(decision.get("decision") or "skip")
        current_range["reason"] = str(decision.get("reason") or decision_type)
        buffer_items = _normalize_buffer_items(hook_state.get("memory_hook_buffer") or [])

        if decision_type == "skip":
            chat_session_store.set_memory_hook_scan(session_id, latest_message_id)
            if not buffer_items:
                chat_session_store.mark_memory_hook(session_id, latest_message_id)
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "baseline_message_id": baseline_message_id,
                "latest_message_id": latest_message_id,
                "status": "skipped",
                "reason": current_range["reason"],
                "buffer_count": len(buffer_items),
            })
            return {
                "success": True,
                "actions_taken": 0,
                "message": "本次会话增量未达到长期记忆提取条件，跳过记忆更新",
            }

        ranges_to_extract: list[dict[str, Any]]
        extraction_reason = trigger_reason
        if decision_type == "weak":
            buffer_items.append(current_range)
            chat_session_store.set_memory_hook_buffer(session_id, buffer_items)
            chat_session_store.set_memory_hook_scan(session_id, latest_message_id)
            if len(buffer_items) < HOOK_BUFFER_TARGET_TURNS:
                _log_hook_run({
                    "session_id": session_id,
                    "trigger_reason": trigger_reason,
                    "baseline_message_id": baseline_message_id,
                    "latest_message_id": latest_message_id,
                    "status": "buffered",
                    "reason": current_range["reason"],
                    "buffer_count": len(buffer_items),
                    "buffer_target": HOOK_BUFFER_TARGET_TURNS,
                })
                return {
                    "success": True,
                    "actions_taken": 0,
                    "message": f"长期记忆弱信号已暂存（{len(buffer_items)}/{HOOK_BUFFER_TARGET_TURNS}）",
                }
            ranges_to_extract = buffer_items
            extraction_reason = f"{trigger_reason}:weak_buffer_{len(buffer_items)}"
        else:
            ranges_to_extract = [*buffer_items, current_range] if buffer_items else [current_range]
            extraction_reason = f"{trigger_reason}:{current_range['reason']}"

        extraction_messages = _load_messages_for_ranges(session_id, ranges_to_extract)
        if not extraction_messages:
            chat_session_store.clear_memory_hook_buffer(session_id)
            chat_session_store.mark_memory_hook(session_id, latest_message_id)
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "baseline_message_id": baseline_message_id,
                "latest_message_id": latest_message_id,
                "status": "skipped",
                "reason": "buffer_ranges_empty",
            })
            return {
                "success": True,
                "actions_taken": 0,
                "message": "没有可提取的 buffered messages",
            }

        result = _run_memory_extraction(
            session_id=session_id,
            messages=extraction_messages,
            baseline_message_id=min(int(item.get("start_message_id") or latest_message_id) for item in ranges_to_extract) - 1,
            latest_message_id=max(int(item.get("end_message_id") or latest_message_id) for item in ranges_to_extract),
            trigger_reason=extraction_reason,
            buffer_count=len(ranges_to_extract) if decision_type == "weak" else len(buffer_items),
        )
        if result.get("success"):
            chat_session_store.clear_memory_hook_buffer(session_id)
        return result

    except Exception as e:
        try:
            chat_session_store.set_memory_hook_scan(session_id, chat_session_store.latest_message_id(session_id), error=str(e))
        except Exception:
            pass
        _log_hook_run({
            "session_id": session_id,
            "trigger_reason": trigger_reason,
            "status": "failed",
            "error": str(e)[:2000],
        })
        return {
            "success": False,
            "actions_taken": 0,
            "message": f"记忆钩子执行失败: {str(e)}"
        }


def _run_memory_extraction(
    session_id: str,
    messages: list[dict[str, Any]],
    baseline_message_id: int,
    latest_message_id: int,
    trigger_reason: str,
    buffer_count: int = 0,
) -> dict[str, Any]:
    try:
        session_memory_state = chat_session_store.get_memory_state(session_id)
        explicit_memory_intent = _has_explicit_memory_intent(messages)
        hook_payload = {
            "session_id": session_id,
            "trigger_reason": trigger_reason,
            "latest_message_id": latest_message_id,
            "baseline_message_id": baseline_message_id,
            "explicit_memory_intent": explicit_memory_intent,
            "buffer_count": buffer_count,
            "compacted_session_memory": session_memory_state.get("memory") or {},
            "new_messages": _trim_messages_for_hook(messages),
        }

        # 加载钩子 prompt
        hook_prompt = HOOK_PROMPT_PATH.read_text(encoding="utf-8")

        # 调用独立 LLM 审视会话
        llm_messages = [
            {"role": "system", "content": hook_prompt},
            {
                "role": "user",
                "content": (
                    "请审视以下结构化会话增量，决定是否需要更新长期记忆。只依据给定数据，不要脑补。\n"
                    + ("注意：本轮包含用户显式记忆请求；如果内容满足规则，应优先写入或更新长期记忆。\n\n" if explicit_memory_intent else "\n")
                )
                + json.dumps(hook_payload, ensure_ascii=False, indent=2, default=str),
            },
        ]

        chunks: list[str] = []
        for chunk in openai_stream_client.stream_chat(llm_messages, tools=[]):
            if chunk.content:
                chunks.append(chunk.content)
        llm_output = "".join(chunks).strip()

        # 解析 LLM 输出的工具调用序列
        tool_calls = _parse_tool_calls(llm_output)

        if not tool_calls:
            chat_session_store.mark_memory_hook(session_id, latest_message_id)
            _log_hook_run({
                "session_id": session_id,
                "trigger_reason": trigger_reason,
                "baseline_message_id": baseline_message_id,
                "latest_message_id": latest_message_id,
                "explicit_memory_intent": explicit_memory_intent,
                "status": "completed",
                "actions_taken": 0,
                "reason": "no_tool_calls",
                "llm_output_preview": llm_output[:2000],
            })
            return {
                "success": True,
                "actions_taken": 0,
                "message": "LLM 判断本次会话无需更新记忆"
            }

        # 执行工具调用。写入类操作不再直接修改核心记忆，只生成候选，等待右栏确认。
        actions_taken = 0
        action_results = []
        for tool_call in tool_calls:
            tool_name = tool_call.get("tool")
            params = tool_call.get("params", {})
            params = _inject_memory_metadata(session_id, tool_name, params, latest_message_id, trigger_reason)
            normalized_tool_name = _normalize_memory_tool_name(tool_name)

            try:
                if normalized_tool_name == "memory_read":
                    result = memory_tools.memory_read(**params)
                elif normalized_tool_name == "memory_write":
                    result = _create_candidate_from_memory_tool(
                        normalized_tool_name,
                        params,
                        session_id=session_id,
                        latest_message_id=latest_message_id,
                    )
                    actions_taken += 1
                elif normalized_tool_name == "memory_update":
                    result = _create_candidate_from_memory_tool(
                        normalized_tool_name,
                        params,
                        session_id=session_id,
                        latest_message_id=latest_message_id,
                    )
                    actions_taken += 1
                elif normalized_tool_name == "memory_archive":
                    result = _create_candidate_from_memory_tool(
                        normalized_tool_name,
                        params,
                        session_id=session_id,
                        latest_message_id=latest_message_id,
                    )
                    actions_taken += 1
                else:
                    result = {"success": False, "message": f"未知记忆工具: {tool_name}"}
                action_results.append({
                    "tool": normalized_tool_name,
                    "success": bool(result.get("success")),
                    "message": str(result.get("message") or "")[:500],
                })
            except Exception as e:
                # 单个工具调用失败不影响其他调用
                print(f"Memory hook tool call failed: {tool_name}, error: {e}")
                action_results.append({
                    "tool": normalized_tool_name,
                    "success": False,
                    "message": str(e)[:500],
                })
                continue

        chat_session_store.mark_memory_hook(session_id, latest_message_id)
        _log_hook_run({
            "session_id": session_id,
            "trigger_reason": trigger_reason,
            "baseline_message_id": baseline_message_id,
            "latest_message_id": latest_message_id,
            "explicit_memory_intent": explicit_memory_intent,
            "status": "completed",
            "actions_taken": actions_taken,
            "llm_output_preview": llm_output[:2000],
            "tool_calls": tool_calls[:12],
            "action_results": action_results,
        })

        return {
            "success": True,
            "actions_taken": actions_taken,
            "message": f"成功执行 {actions_taken} 个记忆更新操作"
        }

    except Exception as e:
        try:
            chat_session_store.set_memory_hook_scan(session_id, chat_session_store.latest_message_id(session_id), error=str(e))
        except Exception:
            pass
        _log_hook_run({
            "session_id": session_id,
            "trigger_reason": trigger_reason,
            "status": "failed",
            "error": str(e)[:2000],
        })
        return {
            "success": False,
            "actions_taken": 0,
            "message": f"记忆钩子执行失败: {str(e)}"
        }


def _parse_tool_calls(llm_output: str) -> list[dict[str, Any]]:
    """
    从 LLM 输出中解析工具调用序列

    期望格式：
    ```json
    [
      {"tool": "memory_read", "params": {...}},
      {"tool": "memory_write", "params": {...}}
    ]
    ```
    """
    try:
        # 尝试提取 JSON 代码块
        json_match = re.search(r"```json\s*(\[.*?\])\s*```", llm_output, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            tool_calls = json.loads(json_str)
            return tool_calls if isinstance(tool_calls, list) else []

        # 尝试直接解析整个输出
        tool_calls = json.loads(llm_output)
        return tool_calls if isinstance(tool_calls, list) else []
    except Exception:
        # 解析失败返回空列表
        return []


def _inject_memory_metadata(
    session_id: str,
    tool_name: str | None,
    params: dict[str, Any],
    latest_message_id: int,
    trigger_reason: str,
) -> dict[str, Any]:
    if not tool_name or not isinstance(params, dict):
        return params
    normalized_tool_name = _normalize_memory_tool_name(tool_name)
    if normalized_tool_name not in {"memory_write", "memory_update", "memory_archive"}:
        return params
    merged = dict(params)
    existing_metadata = merged.get("metadata")
    metadata = existing_metadata if isinstance(existing_metadata, dict) else {}

    file_type = str(merged.get("file") or "session")
    now = _now()
    defaults = {
        "source": f"session:{session_id}",
        "trigger": trigger_reason,
        "category": file_type,
        "confidence": 0.82 if normalized_tool_name == "memory_write" else 0.88 if normalized_tool_name == "memory_update" else 0.9,
        "ttl_days": 3650 if file_type in {"profile", "playbook"} else 180,
        "decay_weight": 0.08 if file_type in {"profile", "playbook"} else 0.25,
        "created_at": now,
        "updated_at": now,
        "source_message_id": latest_message_id,
        "session_id": session_id,
    }
    merged["metadata"] = {**defaults, **metadata}
    return merged


def _create_candidate_from_memory_tool(
    normalized_tool_name: str,
    params: dict[str, Any],
    session_id: str,
    latest_message_id: int,
) -> dict[str, Any]:
    file_type = str(params.get("file") or "").strip()
    content = str(params.get("content") or params.get("new_content") or "").strip()
    target = str(params.get("target") or "").strip()
    reason = str(params.get("reason") or "")
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    if not content and target:
        content = target
    if normalized_tool_name == "memory_update":
        operation = "UPDATE"
        evidence = target[:1200]
    elif normalized_tool_name == "memory_archive":
        operation = "CONFLICT"
        evidence = target[:1200]
    else:
        operation = "ADD"
        evidence = str(params.get("evidence") or "")[:1200]
    confidence = metadata.get("confidence", 0.82)
    if file_type == "profile":
        result = long_term_memory_service.apply_agent_profile_entry(
            content=content,
            evidence=evidence,
            confidence=confidence,
            operation=operation,
            reason=reason or f"{normalized_tool_name} from session hook",
            source_session_id=session_id,
            source_message_id=latest_message_id,
            related_refs=[{"tool": normalized_tool_name}],
        )
        return {
            "success": True,
            "message": "已自动更新用户画像",
            "entry": result.get("entry"),
            "conflicts": result.get("conflicts", []),
        }
    candidate = long_term_memory_service.create_candidate(
        target=file_type,
        content=content,
        evidence=evidence,
        confidence=confidence,
        operation=operation,
        reason=reason,
        source_session_id=session_id,
        source_message_id=latest_message_id,
        related_refs=[{"tool": normalized_tool_name}],
    )
    return {
        "success": True,
        "message": f"已生成记忆候选，等待用户确认: {candidate.get('candidate_id')}",
        "candidate": candidate,
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _normalize_buffer_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start_id = _coerce_int(item.get("start_message_id"))
        end_id = _coerce_int(item.get("end_message_id"))
        if start_id is None or end_id is None or start_id <= 0 or end_id < start_id:
            continue
        normalized.append({
            "start_message_id": start_id,
            "end_message_id": end_id,
            "reason": str(item.get("reason") or ""),
            "created_at": str(item.get("created_at") or ""),
        })
    normalized.sort(key=lambda item: (item["start_message_id"], item["end_message_id"]))
    return normalized


def _load_messages_for_ranges(session_id: str, ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _normalize_buffer_items(ranges)
    if not normalized:
        return []
    min_start = min(item["start_message_id"] for item in normalized)
    max_end = max(item["end_message_id"] for item in normalized)
    messages = chat_session_store.list_messages(session_id, after_id=min_start - 1, limit=HOOK_SCAN_LIMIT * 2)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for msg in messages:
        message_id = int(msg.get("message_id") or 0)
        if message_id <= 0 or message_id in seen or message_id > max_end:
            continue
        if any(item["start_message_id"] <= message_id <= item["end_message_id"] for item in normalized):
            selected.append(msg)
            seen.add(message_id)
    return selected[:HOOK_MESSAGE_LIMIT]


def _trim_messages_for_hook(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    budget = HOOK_TOTAL_CONTENT_BUDGET
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        used = min(len(content), budget)
        msg["_hook_content_trimmed"] = content[:used]
        budget -= used
        if budget <= 0:
            break

    trimmed: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "")
        raw_content = str(msg.get("content") or "")
        if role == "user":
            content = str(msg.get("_hook_content_trimmed") or "")
        else:
            used = min(len(raw_content), max(budget, 0))
            content = raw_content[:used]
            budget -= used
        item: dict[str, Any] = {
            "message_id": int(msg.get("message_id") or 0),
            "role": role,
            "created_at": str(msg.get("created_at") or ""),
            "content": content,
        }
        if len(content) < len(raw_content):
            item["content_truncated"] = True
        report_links = msg.get("report_links") or []
        if isinstance(report_links, list) and report_links:
            item["report_links"] = report_links[:5]
        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            compacted_tool_calls = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                compacted, used = _trim_tool_call_for_hook(tool_call, max(budget, 0))
                compacted_tool_calls.append(compacted)
                budget -= used
            item["tool_calls"] = compacted_tool_calls
        trimmed.append(item)
    return trimmed


def _trim_tool_call_for_hook(tool_call: dict[str, Any], budget: int) -> tuple[dict[str, Any], int]:
    item = {"tool": str(tool_call.get("tool") or "")}
    result = tool_call.get("result")
    if result is not None:
        compacted_result = _compact_tool_result_for_hook(result)
        result_text = json.dumps(compacted_result, ensure_ascii=False, default=str)
        used = min(len(result_text), budget, HOOK_TOOL_RESULT_KEY_BUDGET)
        item["result_preview"] = result_text[:used]
        if used < len(result_text):
            item["result_truncated"] = True
        return item, used
    return item, 0


def _compact_tool_result_for_hook(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    keys = (
        "success",
        "status",
        "message",
        "error",
        "summary",
        "report_id",
        "job_id",
        "output_report_id",
        "sources",
        "citations",
    )
    compacted = {key: result.get(key) for key in keys if key in result}
    if compacted:
        return compacted
    return result
