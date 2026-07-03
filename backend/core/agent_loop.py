from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

from backend.core.env import settings
from backend.core.function_schema import registry_to_openai_tools
from backend.core.llm_client import LLMNotConfiguredError
from backend.core.openai_stream import openai_stream_client
from backend.core.streaming import sse_event
from backend.core.tool_gateway import tool_gateway
from backend.services.cancellation import RunCancelled, cancellation_store
from backend.services.attachments import attachment_service
from backend.services.capabilities import capability_service
from backend.services.memory import memory_manager
from backend.services.observability import trace_store
from backend.services.sessions import chat_session_store


MAX_AGENT_STEPS = 8
MAX_REPEATED_TOOL_CALLS = 3
FINAL_RESPONSE_TOOL = "final_response"
ACTION_RUNTIME_PROTOCOL = """<action_runtime_protocol>
你必须通过 tool calling 表达下一步动作：
- 需要真实系统能力时，调用对应工具，不要用文字声称已经调用或已经生成确认卡片。
- 已经可以直接回复用户时，调用 final_response，并把最终中文回复写入 content。
- final_response 是结束本轮回答的伪工具；不要同时再请求用户确认。
</action_runtime_protocol>"""
logger = logging.getLogger(__name__)


class AgentLoop:
    def stream(
        self,
        message: str,
        session_id: str = "default",
        mode: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        referenced_attachment_ids: list[str] | None = None,
    ) -> Iterator[str]:
        if not openai_stream_client.configured:
            yield sse_event("error", {"message": "FINCLAW_LLM_API_KEY is not configured"})
            return
        try:
            current_attachment_ids = self._attachment_ids_from_meta(attachments or [])
            current_attachments = attachment_service.list_for_session(session_id, current_attachment_ids, referenced=False)
            referenced_attachments = attachment_service.list_for_session(session_id, referenced_attachment_ids or [], referenced=True)
        except (KeyError, PermissionError, ValueError) as exc:
            yield sse_event("error", {"message": f"图片附件无效：{exc}"})
            return
        message_attachments = current_attachments + referenced_attachments
        effective_message = message if message.strip() else (
            "请理解用户上传或引用的图片，并根据上下文回答。" if message_attachments else ""
        )
        normalized_mode = self._normalize_mode(mode)
        run_id = chat_session_store.acquire_run(session_id, "user_message", {
            "message": message,
            "mode": normalized_mode,
            "attachment_ids": [item.get("attachment_id") for item in message_attachments],
        })
        if run_id is None:
            yield sse_event("error", {"message": "当前会话正在生成回答，请稍后再试。"})
            return
        cancellation_store.clear(session_id)
        chat_session_store.append_message(session_id, "user", message, attachments=message_attachments)
        cancellation_store.clear(session_id, run_id)
        trace_id = trace_store.start_trace(
            session_id,
            run_id,
            "user_message",
            {
                "message": message,
                "mode": normalized_mode,
                "attachment_ids": [item.get("attachment_id") for item in message_attachments],
            },
        )

        messages: list[dict[str, Any]] = [
            *memory_manager.build_context(session_id, extra_system=ACTION_RUNTIME_PROTOCOL),
        ]
        if message_attachments:
            try:
                self._attach_images_to_latest_user_message(
                    messages,
                    text=effective_message,
                    current_attachments=current_attachments,
                    referenced_attachments=referenced_attachments,
                )
            except Exception as exc:
                chat_session_store.finish_run(session_id, run_id, "failed")
                trace_store.finish_trace(trace_id, status="failed", error=str(exc))
                yield sse_event("error", {"message": f"图片处理失败：{exc}"})
                return
        tools = self._openai_tools_for_mode(normalized_mode)
        tool_choice = "required"
        trace_store.event(trace_id, "agent.protocol", {"tool_choice": tool_choice, "final_response_tool": FINAL_RESPONSE_TOOL})
        trace_store.event(trace_id, "agent.turn.started", {"session_id": session_id, "run_id": run_id})
        yield sse_event("message_start", {"session_id": session_id})
        yield sse_event("status_delta", {"phase": "context", "message": "正在整理上下文"})
        final_text_parts: list[str] = []
        final_reasoning_parts: list[str] = []
        tool_records: list[dict[str, Any]] = []
        tool_call_fingerprints: dict[str, int] = {}
        run_finished = False

        try:
            for step_index in range(MAX_AGENT_STEPS):
                cancellation_store.raise_if_cancelled(session_id, run_id)

                # P0: 每轮更新 runtime facts（除第一轮）
                if step_index > 0:
                    # 移除旧的 runtime_facts 系统消息
                    messages = [msg for msg in messages if not self._is_runtime_facts_message(msg)]

                    # 重新注入最新 runtime facts
                    from backend.core.prompt_builder import prompt_builder
                    latest_runtime = prompt_builder.build_runtime_facts(session_id)
                    # 插入到系统消息之后，用户消息之前
                    insert_pos = self._find_runtime_facts_position(messages)
                    messages.insert(insert_pos, {
                        "role": "system",
                        "content": latest_runtime
                    })

                    trace_store.event(trace_id, "runtime_facts.updated", {
                        "step": step_index,
                        "message_count": len(messages)
                    })

                yield sse_event("status_delta", {"phase": "llm", "message": "正在思考"})

                assistant_message, finish_reason = yield from self._run_one_llm_turn(
                    messages,
                    tools,
                    final_text_parts,
                    final_reasoning_parts,
                    trace_id=trace_id,
                    tool_choice=tool_choice,
                )
                messages.append(assistant_message)

                tool_calls = assistant_message.get("tool_calls") or []
                if finish_reason == "tool_calls" or tool_calls:
                    real_tool_calls = [call for call in tool_calls if not self._is_final_response_call(call)]
                    if tool_calls and not real_tool_calls:
                        content = self._final_response_content(tool_calls[0])
                        if content:
                            final_text_parts.append(content)
                            yield sse_event("text_delta", {"text": content})
                        self._persist_assistant(
                            session_id,
                            "".join(final_text_parts),
                            tool_records,
                            self._collapse_reasoning(final_reasoning_parts),
                        )
                        trace_store.event(trace_id, "agent.turn.completed", {"finish_reason": "final_response", "tool_count": len(tool_records)})
                        trace_store.finish_trace(trace_id)
                        yield sse_event("message_done", {})
                        chat_session_store.finish_run(session_id, run_id)
                        run_finished = True
                        self._kick_continuation_worker()
                        self._fire_memory_hook(session_id, run_id)
                        return
                    waiting_approvals: list[dict[str, Any]] = []
                    for call in real_tool_calls:
                        if self._is_repeated_tool_call(call, tool_call_fingerprints):
                            payload = self._loop_break_payload(call)
                            tool_records.append({"tool": call["function"]["name"], "result": payload})
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "name": call["function"]["name"],
                                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                                }
                            )
                            trace_store.event(trace_id, "agent.loop_detected", payload, level="warn")
                            continue
                        result_payload = yield from self._execute_tool_call(call, session_id=session_id, run_id=run_id, trace_id=trace_id, mode=normalized_mode)
                        tool_records.append({"tool": call["function"]["name"], "result": result_payload})

                        # 原有的 tool 消息
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["function"]["name"],
                            "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                        }
                        messages.append(tool_message)

                        if result_payload.get("status") == "approval_required":
                            action = result_payload.get("action") or {}
                            waiting_approvals.append(action)
                            trace_store.event(trace_id, "approval.queued", {
                                "tool": call["function"]["name"],
                                "action_id": action.get("action_id")
                            })
                    if waiting_approvals:
                        self._persist_assistant(
                            session_id,
                            "".join(final_text_parts),
                            tool_records,
                            self._collapse_reasoning(final_reasoning_parts),
                        )
                        trace_store.event(trace_id, "agent.turn.waiting_approval", {"count": len(waiting_approvals)})
                        trace_store.finish_trace(trace_id, status="waiting_approval")
                        yield sse_event("message_done", {"waiting_approval": True})
                        chat_session_store.finish_run(session_id, run_id, "waiting_approval")
                        run_finished = True
                        return
                    continue

                self._persist_assistant(
                    session_id,
                    "".join(final_text_parts),
                    tool_records,
                    self._collapse_reasoning(final_reasoning_parts),
                )
                trace_store.event(trace_id, "agent.turn.completed", {"finish_reason": finish_reason, "tool_count": len(tool_records)})
                trace_store.finish_trace(trace_id)
                yield sse_event("message_done", {})
                chat_session_store.finish_run(session_id, run_id)
                run_finished = True
                self._kick_continuation_worker()
                self._fire_memory_hook(session_id, run_id)
                return
            fallback = "本轮分析达到系统步数上限，已停止继续调用工具。请你缩小问题范围，或让我基于目前已获得的信息继续总结。"
            final_text_parts.append(fallback)
            self._persist_assistant(
                session_id,
                "".join(final_text_parts),
                tool_records,
                self._collapse_reasoning(final_reasoning_parts),
            )
            yield sse_event("text_delta", {"text": fallback})
            yield sse_event("message_done", {"degraded": True, "reason": "max_steps"})
            trace_store.event(trace_id, "agent.turn.max_steps", {"max_steps": MAX_AGENT_STEPS}, level="warn")
            trace_store.finish_trace(trace_id, status="degraded", error="Agent loop reached max steps")
            chat_session_store.finish_run(session_id, run_id, "degraded")
            run_finished = True
        except LLMNotConfiguredError as exc:
            yield sse_event("error", {"message": str(exc)})
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
            chat_session_store.finish_run(session_id, run_id, "failed")
            run_finished = True
        except RunCancelled as exc:
            yield sse_event("status_delta", {"phase": "cancelled", "message": "已停止"})
            if "".join(final_text_parts).strip():
                self._persist_assistant(
                    session_id,
                    "".join(final_text_parts),
                    tool_records,
                    self._collapse_reasoning(final_reasoning_parts),
                )
            yield sse_event("message_done", {"cancelled": True})
            trace_store.event(trace_id, "agent.turn.cancelled", {"run_id": run_id}, level="warn")
            trace_store.finish_trace(trace_id, status="cancelled", error=str(exc))
            chat_session_store.finish_run(session_id, run_id, "cancelled")
            run_finished = True
            self._kick_continuation_worker()
        except Exception as exc:
            logger.exception("Agent loop stream failed for session_id=%s run_id=%s", session_id, run_id)
            yield sse_event("error", {"message": str(exc)})
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
            chat_session_store.finish_run(session_id, run_id, "failed")
            run_finished = True
        finally:
            if not run_finished:
                trace_store.event(trace_id, "agent.turn.disconnected", {"run_id": run_id}, level="warn")
                trace_store.finish_trace(trace_id, status="cancelled", error="stream disconnected before run cleanup")
                chat_session_store.finish_run(session_id, run_id, "cancelled")

    def _attachment_ids_from_meta(self, attachments: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            attachment_id = str(item.get("attachment_id") or "").strip()
            if attachment_id and attachment_id not in ids:
                ids.append(attachment_id)
        if len(ids) > 4:
            raise ValueError("单轮最多上传 4 张图片")
        return ids

    def _attach_images_to_latest_user_message(
        self,
        messages: list[dict[str, Any]],
        *,
        text: str,
        current_attachments: list[dict[str, Any]],
        referenced_attachments: list[dict[str, Any]],
    ) -> None:
        total = len(current_attachments) + len(referenced_attachments)
        if total > 8:
            raise ValueError("单轮最多处理 8 张图片（含引用图片）")
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": text.strip() or "请理解用户上传或引用的图片，并根据上下文回答。"}
        ]
        if current_attachments:
            parts.append({"type": "text", "text": "用户本轮上传的图片如下。"})
            parts.extend(self._image_parts(current_attachments))
        if referenced_attachments:
            parts.append({"type": "text", "text": "用户显式引用的历史图片如下。"})
            parts.extend(self._image_parts(referenced_attachments))

        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                messages[index]["content"] = parts
                return
        messages.append({"role": "user", "content": parts})

    def _image_parts(self, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for item in attachments:
            attachment_id = str(item.get("attachment_id") or "")
            if not attachment_id:
                continue
            data_url = attachment_service.read_as_data_url(attachment_id)
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                    "detail": "auto",
                },
            })
        return parts

    def stream_after_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        session_id: str = "default",
    ) -> Iterator[str]:
        if not openai_stream_client.configured:
            yield sse_event("error", {"message": "FINCLAW_LLM_API_KEY is not configured"})
            return
        run_id = chat_session_store.acquire_run(
            session_id,
            "approval_result",
            {"tool_name": tool_name, "arguments": arguments, "result": result},
        )
        if run_id is None:
            chat_session_store.add_event(
                session_id,
                "approval.executed",
                {"tool_name": tool_name, "arguments": arguments, "result": result},
                priority=20,
            )
            yield sse_event("text_delta", {"text": "操作已执行，当前会话正在处理其他回复；我会在随后接续说明结果。"})
            yield sse_event("message_done", {})
            self._kick_continuation_worker()
            return
        cancellation_store.clear(session_id)
        cancellation_store.clear(session_id, run_id)
        trace_id = trace_store.start_trace(
            session_id,
            run_id,
            "approval_result",
            {"tool_name": tool_name, "arguments": arguments},
        )

        messages: list[dict[str, Any]] = [
            *memory_manager.build_context(session_id, extra_system=ACTION_RUNTIME_PROTOCOL),
            {
                "role": "user",
                "content": (
                    "用户已经确认执行一个需要确认的工具。"
                    "请基于工具执行结果继续完成用户任务，给出清晰、格式化的中文回复。"
                ),
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "approved_action",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "approved_action",
                "name": tool_name,
                "content": json.dumps({"status": "ok", "result": result}, ensure_ascii=False, default=str),
            },
        ]
        if settings.llm_thinking_payload is not None:
            messages[-2]["reasoning_content"] = ""
        tools = self._openai_tools_for_mode(None)
        yield sse_event("message_start", {"session_id": session_id})
        yield sse_event("status_delta", {"phase": "approval", "message": "正在执行已确认操作"})
        yield sse_event("tool_call_result", {"name": tool_name, "result": result})
        trace_store.event(trace_id, "approval.result.received", {"tool_name": tool_name, "arguments": arguments})
        recommended_action = tool_gateway.create_pending_recommended_action(
            result.get("result") if isinstance(result, dict) and "result" in result else result,
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
        )
        if recommended_action:
            trace_store.event(trace_id, "approval.required", {"action": recommended_action})
            yield sse_event("approval_required", {"action": recommended_action})
            self._persist_assistant(session_id, "", [{"tool": "recommended_action_pending", "result": recommended_action}])
            trace_store.finish_trace(trace_id, status="waiting_approval")
            yield sse_event("message_done", {"waiting_approval": True})
            chat_session_store.finish_run(session_id, run_id, "waiting_approval")
            return
        final_text_parts: list[str] = []
        final_reasoning_parts: list[str] = []
        tool_records: list[dict[str, Any]] = [{"tool": tool_name, "result": result}]
        tool_call_fingerprints: dict[str, int] = {}
        if recommended_action:
            tool_records.append({"tool": "recommended_action_pending", "result": recommended_action})

        try:
            for _ in range(MAX_AGENT_STEPS):
                cancellation_store.raise_if_cancelled(session_id, run_id)
                yield sse_event("status_delta", {"phase": "llm", "message": "正在整理执行结果"})
                assistant_message, finish_reason = yield from self._run_one_llm_turn(
                    messages,
                    tools,
                    final_text_parts,
                    final_reasoning_parts,
                    trace_id=trace_id,
                    tool_choice="required",
                )
                messages.append(assistant_message)

                tool_calls = assistant_message.get("tool_calls") or []
                if finish_reason == "tool_calls" or tool_calls:
                    real_tool_calls = [call for call in tool_calls if not self._is_final_response_call(call)]
                    if tool_calls and not real_tool_calls:
                        content = self._final_response_content(tool_calls[0])
                        if content:
                            final_text_parts.append(content)
                            yield sse_event("text_delta", {"text": content})
                        self._persist_assistant(
                            session_id,
                            "".join(final_text_parts),
                            tool_records,
                            self._collapse_reasoning(final_reasoning_parts),
                        )
                        trace_store.event(trace_id, "agent.turn.completed", {"finish_reason": "final_response", "tool_count": len(tool_records)})
                        trace_store.finish_trace(trace_id)
                        yield sse_event("message_done", {})
                        chat_session_store.finish_run(session_id, run_id)
                        self._kick_continuation_worker()
                        self._fire_memory_hook(session_id, run_id)
                        return
                    waiting_approvals: list[dict[str, Any]] = []
                    for call in real_tool_calls:
                        if self._is_repeated_tool_call(call, tool_call_fingerprints):
                            payload = self._loop_break_payload(call)
                            tool_records.append({"tool": call["function"]["name"], "result": payload})
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "name": call["function"]["name"],
                                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                                }
                            )
                            trace_store.event(trace_id, "agent.loop_detected", payload, level="warn")
                            continue
                        result_payload = yield from self._execute_tool_call(call, session_id=session_id, run_id=run_id, trace_id=trace_id, mode=None)
                        tool_records.append({"tool": call["function"]["name"], "result": result_payload})

                        # 原有的 tool 消息
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["function"]["name"],
                            "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                        }
                        messages.append(tool_message)

                        if result_payload.get("status") == "approval_required":
                            action = result_payload.get("action") or {}
                            waiting_approvals.append(action)
                            trace_store.event(trace_id, "approval.queued", {
                                "tool": call["function"]["name"],
                                "action_id": action.get("action_id")
                            })
                    if waiting_approvals:
                        self._persist_assistant(
                            session_id,
                            "".join(final_text_parts),
                            tool_records,
                            self._collapse_reasoning(final_reasoning_parts),
                        )
                        trace_store.event(trace_id, "agent.turn.waiting_approval", {"count": len(waiting_approvals)})
                        trace_store.finish_trace(trace_id, status="waiting_approval")
                        yield sse_event("message_done", {"waiting_approval": True})
                        chat_session_store.finish_run(session_id, run_id, "waiting_approval")
                        return
                    continue

                self._persist_assistant(
                    session_id,
                    "".join(final_text_parts),
                    tool_records,
                    self._collapse_reasoning(final_reasoning_parts),
                )
                trace_store.event(trace_id, "agent.turn.completed", {"finish_reason": finish_reason, "tool_count": len(tool_records)})
                trace_store.finish_trace(trace_id)
                yield sse_event("message_done", {})
                chat_session_store.finish_run(session_id, run_id)
                self._kick_continuation_worker()
                self._fire_memory_hook(session_id, run_id)
                return
            fallback = "已确认操作执行后，后续分析达到系统步数上限。我已停止继续调用工具，避免循环执行。"
            final_text_parts.append(fallback)
            self._persist_assistant(
                session_id,
                "".join(final_text_parts),
                tool_records,
                self._collapse_reasoning(final_reasoning_parts),
            )
            yield sse_event("text_delta", {"text": fallback})
            yield sse_event("message_done", {"degraded": True, "reason": "max_steps"})
            trace_store.event(trace_id, "agent.turn.max_steps", {"max_steps": MAX_AGENT_STEPS}, level="warn")
            trace_store.finish_trace(trace_id, status="degraded", error="Agent loop reached max steps")
            chat_session_store.finish_run(session_id, run_id, "degraded")
        except Exception as exc:
            logger.exception("Agent loop approval stream failed for session_id=%s run_id=%s", session_id, run_id)
            yield sse_event("error", {"message": str(exc)})
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
            chat_session_store.finish_run(session_id, run_id, "failed")

    def stream_after_denial(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str = "default",
    ) -> Iterator[str]:
        fallback = "已取消该操作。"
        if not openai_stream_client.configured:
            chat_session_store.append_message(session_id, "assistant", fallback)
            yield sse_event("text_delta", {"text": fallback})
            yield sse_event("message_done", {"cancelled_action": True})
            return

        run_id = chat_session_store.acquire_run(
            session_id,
            "approval_denied",
            {"tool_name": tool_name, "arguments": arguments},
        )
        if run_id is None:
            chat_session_store.append_message(session_id, "assistant", fallback)
            chat_session_store.add_event(
                session_id,
                "approval.denied",
                {"tool_name": tool_name, "arguments": arguments},
                priority=10,
            )
            yield sse_event("text_delta", {"text": fallback})
            yield sse_event("message_done", {"cancelled_action": True, "busy": True})
            return

        cancellation_store.clear(session_id)
        cancellation_store.clear(session_id, run_id)
        trace_id = trace_store.start_trace(
            session_id,
            run_id,
            "approval_denied",
            {"tool_name": tool_name, "arguments": arguments},
        )
        messages: list[dict[str, Any]] = [
            *memory_manager.build_context(session_id),
            {
                "role": "user",
                "content": (
                    "用户刚刚取消了一个需要确认的工具调用。\n"
                    f"被取消工具：{tool_name}\n"
                    f"被取消参数：{json.dumps(arguments, ensure_ascii=False, default=str)}\n\n"
                    "请用中文继续回复用户：\n"
                    "1. 明确说明该操作已取消，不会执行。\n"
                    "2. 不要再次调用同一个工具，不要再次创建确认卡片。\n"
                    "3. 如果仍能在不执行该工具的情况下提供帮助，给出简短替代说明；否则说明需要用户重新明确下一步。"
                ),
            },
        ]
        yield sse_event("message_start", {"session_id": session_id})
        yield sse_event("status_delta", {"phase": "approval", "message": "正在处理取消操作"})
        final_text_parts: list[str] = []
        final_reasoning_parts: list[str] = []
        try:
            assistant_message, _ = yield from self._run_one_llm_turn(
                messages,
                [],
                final_text_parts,
                final_reasoning_parts,
                trace_id=trace_id,
                tool_choice="none",
            )
            content = "".join(final_text_parts).strip() or str(assistant_message.get("content") or "").strip() or fallback
            if not "".join(final_text_parts).strip():
                yield sse_event("text_delta", {"text": content})
            self._persist_assistant(
                session_id,
                content,
                [],
                self._collapse_reasoning(final_reasoning_parts),
            )
            trace_store.finish_trace(trace_id)
            chat_session_store.finish_run(session_id, run_id)
            yield sse_event("message_done", {"cancelled_action": True})
        except Exception as exc:
            logger.exception("Agent loop denial stream failed for session_id=%s run_id=%s", session_id, run_id)
            chat_session_store.append_message(session_id, "assistant", fallback)
            yield sse_event("text_delta", {"text": fallback})
            yield sse_event("message_done", {"cancelled_action": True, "degraded": True})
            trace_store.finish_trace(trace_id, status="failed", error=str(exc))
            chat_session_store.finish_run(session_id, run_id, "failed")

    def run_continuation(self, session_id: str, event_type: str, payload: dict[str, Any]) -> str:
        if not openai_stream_client.configured:
            raise LLMNotConfiguredError("FINCLAW_LLM_API_KEY is not configured")
        run_id = chat_session_store.acquire_run(session_id, event_type, payload)
        if run_id is None:
            return "busy"
        cancellation_store.clear(session_id)
        trace_id = trace_store.start_trace(session_id, run_id, event_type, {"event": payload})
        messages: list[dict[str, Any]] = [
            *memory_manager.build_context(session_id, extra_system=ACTION_RUNTIME_PROTOCOL),
            {
                "role": "user",
                "content": self._continuation_prompt(event_type, payload),
            },
        ]
        tools = self._openai_tools_for_mode(None)
        final_text_parts: list[str] = []
        final_reasoning_parts: list[str] = []
        tool_records: list[dict[str, Any]] = []
        tool_call_fingerprints: dict[str, int] = {}
        try:
            for _ in range(MAX_AGENT_STEPS):
                cancellation_store.raise_if_cancelled(session_id, run_id)
                trace_store.event(
                    trace_id,
                    "continuation.turn.started",
                    {"phase": "llm", "message": "正在接续分析"},
                )
                assistant_message, finish_reason = self._run_one_llm_turn_sync(
                    messages,
                    tools,
                    final_text_parts,
                    final_reasoning_parts,
                    trace_id=trace_id,
                    tool_choice="required",
                )
                messages.append(assistant_message)
                tool_calls = assistant_message.get("tool_calls") or []
                if finish_reason == "tool_calls" or tool_calls:
                    real_tool_calls = [call for call in tool_calls if not self._is_final_response_call(call)]
                    if tool_calls and not real_tool_calls:
                        content = self._final_response_content(tool_calls[0])
                        if content:
                            final_text_parts.append(content)
                        self._persist_assistant(
                            session_id,
                            "".join(final_text_parts),
                            tool_records,
                            self._collapse_reasoning(final_reasoning_parts),
                        )
                        chat_session_store.finish_run(session_id, run_id)
                        trace_store.finish_trace(trace_id)
                        self._fire_memory_hook(session_id)
                        return "completed"
                    for call in real_tool_calls:
                        if self._is_repeated_tool_call(call, tool_call_fingerprints):
                            result_payload = self._loop_break_payload(call)
                            tool_records.append({"tool": call["function"]["name"], "result": result_payload})
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "name": call["function"]["name"],
                                    "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                                }
                            )
                            continue
                        result_payload = self._execute_tool_call_sync(call, session_id=session_id, run_id=run_id, trace_id=trace_id, mode=None)
                        tool_records.append({"tool": call["function"]["name"], "result": result_payload})
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "name": call["function"]["name"],
                                "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                            }
                        )
                    continue
                self._persist_assistant(
                    session_id,
                    "".join(final_text_parts),
                    tool_records,
                    self._collapse_reasoning(final_reasoning_parts),
                )
                trace_store.finish_trace(trace_id)
                chat_session_store.finish_run(session_id, run_id)
                self._fire_memory_hook(session_id)
                return "completed"
            fallback = self._continuation_limit_fallback(event_type, tool_records)
            final_text_parts.append(fallback)
            self._persist_assistant(
                session_id,
                "".join(final_text_parts),
                tool_records,
                self._collapse_reasoning(final_reasoning_parts),
            )
            chat_session_store.finish_run(session_id, run_id, "degraded")
            trace_store.finish_trace(trace_id, status="degraded", error="Agent loop reached max steps")
            return "degraded"
        except Exception:
            logger.exception("Continuation run failed for session_id=%s run_id=%s event_type=%s", session_id, run_id, event_type)
            chat_session_store.finish_run(session_id, run_id, "failed")
            trace_store.finish_trace(trace_id, status="failed")

    def _continuation_limit_fallback(self, event_type: str, tool_records: list[dict[str, Any]]) -> str:
        report_records = [
            record for record in tool_records
            if record.get("tool") in {"query_report", "read_report_section", "get_report_detail"}
        ]
        if not report_records:
            return "后台接续分析达到系统步数上限，已停止继续调用工具，避免循环执行。当前已获得的信息不足以完整总结，请你缩小问题范围或指定要读取的报告章节。"

        lines = [
            "后台接续分析达到系统步数上限，我已停止继续调用工具，避免循环执行。",
            "",
            "已读取到的报告材料如下，可先基于这些内容做阶段性判断：",
        ]
        for record in report_records[-4:]:
            tool = record.get("tool")
            result = record.get("result")
            if not isinstance(result, dict):
                continue
            if tool == "query_report":
                report = result.get("report") if isinstance(result.get("report"), dict) else {}
                coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
                sections = result.get("sections") if isinstance(result.get("sections"), list) else []
                title = report.get("title") or report.get("report_id") or "报告"
                lines.append(f"- `query_report`: {title}，已抽取 {coverage.get('selected_sections', len(sections))} 个章节，返回约 {coverage.get('returned_chars', 0)} 字。")
                for section in sections[:4]:
                    if isinstance(section, dict):
                        lines.append(f"  - {section.get('section_id')} {section.get('title')}，摘录 {section.get('excerpt_chars', 0)} 字")
            elif tool == "read_report_section":
                section = result.get("section") if isinstance(result.get("section"), dict) else {}
                window = result.get("read_window") if isinstance(result.get("read_window"), dict) else {}
                lines.append(
                    f"- `read_report_section`: {section.get('section_id')} {section.get('title')}，"
                    f"offset={window.get('offset', 0)}，has_more={window.get('has_more', False)}。"
                )
            elif tool == "get_report_detail":
                report = result.get("report") if isinstance(result.get("report"), dict) else {}
                manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
                lines.append(f"- `get_report_detail`: {report.get('title') or report.get('report_id') or '报告'}，目录章节数 {len(manifest.get('sections') or [])}。")
        lines.extend(
            [
                "",
                "这不是报告全文结论。要继续精读，请指定报告章节或具体问题；系统会从当前已定位的章节继续读取，而不是重新翻目录。",
            ]
        )
        return "\n".join(lines)

    def _run_one_llm_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        content_sink: list[str] | None = None,
        reasoning_sink: list[str] | None = None,
        trace_id: str | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> Iterator[str | tuple[dict[str, Any], str | None]]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None

        for chunk in openai_stream_client.stream_chat(messages, tools, trace_id=trace_id, tool_choice=tool_choice):
            if chunk.content:
                content_parts.append(chunk.content)
                if content_sink is not None:
                    content_sink.append(chunk.content)
                yield sse_event("text_delta", {"text": chunk.content})
            if chunk.reasoning_content:
                reasoning_parts.append(chunk.reasoning_content)
                if reasoning_sink is not None:
                    reasoning_sink.append(chunk.reasoning_content)
            if chunk.tool_calls:
                for delta in chunk.tool_calls:
                    index = int(delta.get("index", 0))
                    part = tool_call_parts.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if delta.get("id"):
                        part["id"] += delta["id"]
                    fn = delta.get("function") or {}
                    if fn.get("name"):
                        part["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        part["function"]["arguments"] += fn["arguments"]
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_call_parts:
            assistant_message["tool_calls"] = [tool_call_parts[idx] for idx in sorted(tool_call_parts)]
        reasoning_content = self._collapse_reasoning(reasoning_parts, has_tool_calls=bool(tool_call_parts))
        if reasoning_content is not None:
            assistant_message["reasoning_content"] = reasoning_content
        return assistant_message, finish_reason

    def _run_one_llm_turn_sync(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        content_sink: list[str] | None = None,
        reasoning_sink: list[str] | None = None,
        trace_id: str | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> tuple[dict[str, Any], str | None]:
        generator = self._run_one_llm_turn(
            messages,
            tools,
            content_sink,
            reasoning_sink,
            trace_id=trace_id,
            tool_choice=tool_choice,
        )
        result: tuple[dict[str, Any], str | None] | None = None
        while True:
            try:
                next(generator)
            except StopIteration as stop:
                result = stop.value
                break
        if result is None:
            raise RuntimeError("LLM turn returned no result")
        return result

    def _execute_tool_call(
        self,
        call: dict[str, Any],
        session_id: str,
        run_id: str,
        trace_id: str | None = None,
        mode: str | None = None,
    ) -> Iterator[str | dict[str, Any]]:
        function = call["function"]
        name = function["name"]
        arguments = function.get("arguments") or "{}"
        if name == FINAL_RESPONSE_TOOL:
            return {"status": "ok", "result": {"content": self._final_response_content(call)}}
        if not self._tool_allowed_in_mode(name, mode):
            payload = {
                "status": "error",
                "message": "Deep Research 只能通过前端“开启研究”按钮启动；普通对话请使用个股深研、DataHub、Web Search 或报告工具完成研究。",
            }
            if trace_id:
                trace_store.event(trace_id, "tool.call.blocked_by_mode", {"name": name, "mode": mode}, level="warn")
            yield sse_event("tool_call_result", {"name": name, "error": payload["message"]})
            return payload
        cancellation_store.raise_if_cancelled(session_id, run_id)
        if trace_id:
            trace_store.event(trace_id, "tool.call.started", {"name": name, "arguments": arguments})
        yield sse_event("status_delta", {"phase": "tool", "message": f"正在调用工具 {name}"})
        yield sse_event("tool_call_start", {"name": name, "arguments": arguments})
        try:
            outcome = tool_gateway.invoke_tool_call(name, arguments, session_id=session_id, run_id=run_id, trace_id=trace_id)
            if outcome.get("pending"):
                payload = {"status": "approval_required", "action": outcome["action"]}
                if trace_id:
                    trace_store.event(trace_id, "approval.required", {"action": outcome["action"]})
                yield sse_event("approval_required", payload)
                return payload
            result_payload = outcome.get("result")
            payload = {"status": "ok", "result": result_payload}
            yield sse_event("status_delta", {"phase": "tool", "message": f"工具 {name} 已完成"})
            yield sse_event("tool_call_result", {"name": name, "result": result_payload})
            if isinstance(result_payload, dict) and result_payload.get("status") == "memory_candidate_created":
                yield sse_event(
                    "memory_candidate_created",
                    {
                        "candidate_id": result_payload.get("candidate_id"),
                        "target": result_payload.get("target"),
                        "message": result_payload.get("message") or "已生成记忆候选，请在右栏确认",
                    },
                )
            recommended_action = tool_gateway.create_pending_recommended_action(
                result_payload,
                session_id=session_id,
                run_id=run_id,
                trace_id=trace_id,
            )
            if recommended_action:
                if trace_id:
                    trace_store.event(trace_id, "approval.required", {"action": recommended_action})
                yield sse_event("approval_required", {"action": recommended_action})
                payload["recommended_action_pending"] = recommended_action
            return payload
        except Exception as exc:
            logger.exception("Tool stream execution failed for tool=%s session_id=%s run_id=%s", name, session_id, run_id)
            payload = {"status": "error", "message": str(exc)}
            if trace_id:
                trace_store.event(trace_id, "tool.call.failed", {"name": name, "error": str(exc)}, level="error")
            yield sse_event("tool_call_result", {"name": name, "error": str(exc)})
            return payload

    def _execute_tool_call_sync(self, call: dict[str, Any], session_id: str, run_id: str, trace_id: str | None = None, mode: str | None = None) -> dict[str, Any]:
        function = call["function"]
        name = function["name"]
        arguments = function.get("arguments") or "{}"
        if name == FINAL_RESPONSE_TOOL:
            return {"status": "ok", "result": {"content": self._final_response_content(call)}}
        if not self._tool_allowed_in_mode(name, mode):
            if trace_id:
                trace_store.event(trace_id, "tool.call.blocked_by_mode", {"name": name, "mode": mode}, level="warn")
            return {
                "status": "error",
                "message": "Deep Research 只能通过前端“开启研究”按钮启动；普通对话请使用个股深研、DataHub、Web Search 或报告工具完成研究。",
            }
        try:
            outcome = tool_gateway.invoke_tool_call(name, arguments, session_id=session_id, run_id=run_id, trace_id=trace_id)
            if outcome.get("pending"):
                return {"status": "approval_required", "action": outcome["action"]}
            payload = {"status": "ok", "result": outcome.get("result")}
            recommended_action = tool_gateway.create_pending_recommended_action(
                outcome.get("result"),
                session_id=session_id,
                run_id=run_id,
                trace_id=trace_id,
            )
            if recommended_action:
                payload["recommended_action_pending"] = recommended_action
            return payload
        except Exception as exc:
            logger.exception("Tool sync execution failed for tool=%s session_id=%s run_id=%s", name, session_id, run_id)
            return {"status": "error", "message": str(exc)}

    def _normalize_mode(self, mode: str | None) -> str | None:
        value = str(mode or "").strip().lower()
        return value if value == "deep_research" else None

    def _openai_tools_for_mode(self, mode: str | None) -> list[dict[str, Any]]:
        exclude = set() if mode == "deep_research" else {"start_research_thread"}
        exclude.update(capability_service.disabled_external_tools())
        tools = registry_to_openai_tools(tool_gateway.registry, exclude=exclude)
        tools.append(self._final_response_tool_schema())
        return tools

    def _final_response_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": FINAL_RESPONSE_TOOL,
                "description": "结束本轮回答。只有在不需要继续调用真实工具时使用；content 必须是面向用户的最终中文回复。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "最终回复内容。不要描述并未执行的工具调用，不要声称已经生成确认卡片。",
                        }
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            },
        }

    def _is_final_response_call(self, call: dict[str, Any]) -> bool:
        function = call.get("function") or {}
        return str(function.get("name") or "") == FINAL_RESPONSE_TOOL

    def _final_response_content(self, call: dict[str, Any]) -> str:
        raw = ((call.get("function") or {}).get("arguments") or "{}").strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        if isinstance(parsed, dict):
            return str(parsed.get("content") or "").strip()
        return str(parsed or "").strip()

    def _tool_allowed_in_mode(self, tool_name: str, mode: str | None) -> bool:
        if tool_name == FINAL_RESPONSE_TOOL:
            return True
        if tool_name in capability_service.disabled_external_tools():
            return False
        return tool_name != "start_research_thread" or mode == "deep_research"

    def _is_repeated_tool_call(self, call: dict[str, Any], fingerprints: dict[str, int]) -> bool:
        function = call.get("function") or {}
        key = json.dumps(
            {
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "{}",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprints[key] = fingerprints.get(key, 0) + 1
        return fingerprints[key] > MAX_REPEATED_TOOL_CALLS

    def _loop_break_payload(self, call: dict[str, Any]) -> dict[str, Any]:
        function = call.get("function") or {}
        tool_name = function.get("name") or "unknown"
        if tool_name == "run_stock_research":
            return {
                "status": "error",
                "code": "repeated_tool_call",
                "message": (
                    "run_stock_research 在本轮对话中被重复调用次数过多，系统已阻止继续启动研究任务。"
                    "不要再次调用 run_stock_research；请改用 get_analysis_jobs 查询已有任务状态，"
                    "或读取已完成报告。若已有任务失败，需要用户明确要求重跑后再以 force=true 启动。"
                ),
            }
        return {
            "status": "error",
            "code": "repeated_tool_call",
            "message": (
                f"工具 {tool_name} 被重复调用次数过多。"
                "系统已阻止继续执行，请改用已有工具结果总结，或向用户说明需要更明确的输入。"
            ),
        }

    def _persist_assistant(
        self,
        session_id: str,
        content: str,
        tool_records: list[dict[str, Any]],
        reasoning_content: str | None = None,
    ) -> None:
        report_links = self._collect_report_links(tool_records)
        sources, citations = self._collect_web_sources(tool_records)
        if not content.strip() and not report_links and not sources:
            return
        chat_session_store.append_message(
            session_id,
            "assistant",
            content.strip(),
            reasoning_content=reasoning_content,
            tool_calls=tool_records,
            report_links=report_links,
            sources=sources,
            citations=citations,
        )
        chat_session_store.maybe_compact(session_id)

    def _collapse_reasoning(self, reasoning_parts: list[str], has_tool_calls: bool = False) -> str | None:
        normalized = [part.strip() for part in reasoning_parts if str(part or "").strip()]
        if normalized:
            return "\n\n".join(normalized)
        if has_tool_calls and settings.llm_thinking_payload is not None:
            return ""
        return None

    def _collect_report_links(self, payload: Any) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            if isinstance(value.get("view_url"), str) and isinstance(value.get("download_url"), str):
                links.append(value)
            for item in value.values():
                walk(item)

        walk(payload)
        return links

    def _collect_web_sources(self, payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_source(source: dict[str, Any]) -> None:
            url = str(source.get("url") or "").strip()
            source_id = str(source.get("source_id") or "").strip()
            key = url or source_id
            if not key or key in seen:
                return
            seen.add(key)
            marker = source.get("marker") or len(sources) + 1
            normalized = dict(source)
            normalized["marker"] = marker
            normalized["source_id"] = source_id or f"src_{marker}"
            sources.append(normalized)

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            if value.get("tool") == "web_research" and isinstance(value.get("result"), dict):
                result_payload = value["result"].get("result") if isinstance(value["result"].get("result"), dict) else value["result"]
                result_sources = result_payload.get("sources")
                if isinstance(result_sources, list):
                    for source in result_sources:
                        if isinstance(source, dict):
                            add_source(source)
                return
            if isinstance(value.get("sources"), list) and value.get("query") and value.get("claims"):
                for source in value["sources"]:
                    if isinstance(source, dict):
                        add_source(source)
                return
            for item in value.values():
                walk(item)

        walk(payload)
        citations = [
            {"marker": source.get("marker"), "source_id": source.get("source_id"), "claim_text": ""}
            for source in sources
            if source.get("marker") and source.get("source_id")
        ]
        return sources, citations

    def _kick_continuation_worker(self) -> None:
        from backend.services.continuation import continuation_service

        continuation_service.kick()

    def _fire_memory_hook(self, session_id: str, run_id: str | None = None) -> None:
        def _run() -> None:
            try:
                chat_session_store.add_event(
                    session_id,
                    "memory.extract",
                    {"run_id": run_id},
                    priority=80,
                )
                self._kick_continuation_worker()
            except Exception:
                pass  # hook failure must never affect the main response

        threading.Thread(target=_run, daemon=True).start()

    def _is_runtime_facts_message(self, msg: dict[str, Any]) -> bool:
        """判断是否是 runtime_facts 系统消息"""
        if msg.get("role") != "system":
            return False
        content = msg.get("content") or ""
        return "<runtime_facts>" in content

    def _find_runtime_facts_position(self, messages: list[dict[str, Any]]) -> int:
        """找到插入 runtime_facts 的位置（在静态系统消息之后）"""
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                return i  # 插入到第一条用户消息之前
        return len(messages)  # 如果没有用户消息，插入到末尾

    def _continuation_prompt(self, event_type: str, payload: dict[str, Any]) -> str:
        if event_type == "user.interrupt":
            return (
                "用户在你上一轮生成过程中补充了新指令。"
                "请基于同一会话上下文直接处理这条新指令，不要说这是脚本通知，也不要要求用户重新发送。\n"
                f"新指令：{payload.get('message', '')}"
            )
        return (
            "后台事件已发生，请接续同一会话回复用户。"
            "不要说你是脚本通知；你需要像自主助手一样根据上下文决定是否读取报告、总结结果、给出下一步。\n"
            f"事件类型：{event_type}\n事件数据：{json.dumps(payload, ensure_ascii=False, default=str)}"
        )


agent_loop = AgentLoop()
