from __future__ import annotations

import json
import re
from typing import Any

from backend.core.llm_client import LLMNotConfiguredError
from backend.core.openai_stream import openai_stream_client
from backend.core.prompt_builder import prompt_builder


COMPACT_TRIGGER_TOKENS = 16_000
COMPACT_TRIGGER_MESSAGES = 16
COMPACT_TARGET_MESSAGES = 10
MAX_COMPACT_MESSAGES = 160
RECENT_CONTEXT_MESSAGES = 14


EMPTY_MEMORY: dict[str, Any] = {
    "stable_memory": {
        "user_preferences": [],
        "system_constraints": [],
        "product_decisions": [],
        "communication_style": [],
    },
    "working_state": {
        "current_goals": [],
        "open_threads": [],
        "completed_steps": [],
        "next_actions": [],
        "active_questions": [],
    },
    "evidence": {
        "reports": [],
        "analysis_jobs": [],
        "pending_actions": [],
        "tool_results": [],
    },
    "risk_notes": [],
    "forget_candidates": [],
    "last_compacted_message_id": 0,
}


COMPACT_SYSTEM_PROMPT = """你是 FinClaw 的上下文压缩器。

任务：把历史会话、工具调用结果和后台任务上下文压缩成结构化 Agent Working State。

硬性规则：
- 只输出严格 JSON，不要 Markdown，不要代码块。
- 禁止编造事实；不确定内容放入 risk_notes。
- 必须保留用户目标、用户偏好、系统约束、产品决策、未完成任务、报告ID、任务ID、actionID、ticker、date。
- 删除重复解释、已解决且无后续价值的报错细节、大段日志。
- 不要把报告正文、完整日志、长工具结果塞入记忆；只保留 report/job/action 引用、关键工具证据和结论。
- last_compacted_message_id 必须等于输入中的 max_message_id。

输出 JSON schema：
{
  "stable_memory": {
    "user_preferences": [],
    "system_constraints": [],
    "product_decisions": [],
    "communication_style": []
  },
  "working_state": {
    "current_goals": [],
    "open_threads": [],
    "completed_steps": [],
    "next_actions": [],
    "active_questions": []
  },
  "evidence": {
    "reports": [],
    "analysis_jobs": [],
    "pending_actions": [],
    "tool_results": []
  },
  "risk_notes": [],
  "forget_candidates": [],
  "last_compacted_message_id": 0
}
"""


class MemoryManager:
    def build_context(self, session_id: str, extra_system: str | None = None) -> list[dict[str, Any]]:
        """
        构建完整的上下文，包括：
        1. 静态 prompt + runtime facts
        2. 长期记忆（Profile/Playbook/Convictions）
        3. 会话级工作记忆（压缩的历史消息）
        4. 最近的对话历史
        """
        from backend.services.sessions import chat_session_store

        self.maybe_compact(session_id, reason="before_run")
        memory_state = chat_session_store.get_memory_state(session_id)
        memory = self._normalize_memory(memory_state.get("memory") or {})

        # 获取最近的用户消息（用于 Playbook 关键词路由）
        recent_messages = chat_session_store.recent_context(session_id, limit=1)
        user_message = ""
        if recent_messages and recent_messages[0].get("role") == "user":
            user_message = recent_messages[0].get("content", "")

        # 构建系统消息（包含长期记忆）
        context: list[dict[str, Any]] = prompt_builder.build_system_messages(
            session_id,
            user_message=user_message,
            extra_system=extra_system
        )

        # 添加会话级工作记忆（压缩的历史消息）
        if memory:
            context.append(
                {
                    "role": "system",
                    "content": "会话结构化记忆（由 compact 生成，硬事实以系统索引为准）：\n"
                    + json.dumps(memory, ensure_ascii=False, indent=2, default=str),
                }
            )

        # 添加最近的对话历史
        context.extend(chat_session_store.recent_context(session_id, limit=RECENT_CONTEXT_MESSAGES))
        return context

    def maybe_compact(self, session_id: str, reason: str) -> bool:
        from backend.services.sessions import chat_session_store

        state = chat_session_store.get_memory_state(session_id)
        last_id = int(state.get("last_compacted_message_id") or 0)
        latest_id = chat_session_store.latest_message_id(session_id)
        if latest_id <= last_id:
            return False

        messages = chat_session_store.messages_for_compaction(session_id, after_id=last_id, limit=MAX_COMPACT_MESSAGES)
        if len(messages) <= COMPACT_TARGET_MESSAGES:
            return False

        estimate = self._estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))
        if reason not in {"manual"} and len(messages) < COMPACT_TRIGGER_MESSAGES and estimate < COMPACT_TRIGGER_TOKENS:
            return False

        if not openai_stream_client.configured:
            return False

        try:
            compacted = self._compact_with_llm(state.get("memory") or EMPTY_MEMORY, messages, latest_id)
            compacted = self._normalize_memory(compacted)
            compacted = self._merge_tool_results(compacted, messages)
            self._validate_memory(compacted, latest_id)
            chat_session_store.save_memory_state(session_id, compacted, latest_id)
            return True
        except Exception as exc:
            chat_session_store.save_compact_error(session_id, str(exc))
            return False

    def _compact_with_llm(
        self,
        previous_memory: dict[str, Any],
        messages: list[dict[str, Any]],
        max_message_id: int,
    ) -> dict[str, Any]:
        user_payload = {
            "previous_memory": self._normalize_memory(previous_memory or EMPTY_MEMORY),
            "max_message_id": max_message_id,
            "messages_to_compact": self._trim_messages(messages),
        }
        chunks: list[str] = []
        for chunk in openai_stream_client.stream_chat(
            [
                {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
            tools=[],
        ):
            if chunk.content:
                chunks.append(chunk.content)
        text = "".join(chunks).strip()
        if not text:
            raise LLMNotConfiguredError("compact LLM returned empty response")
        return self._extract_json(text)

    def _trim_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        trimmed = []
        for msg in messages:
            item = dict(msg)
            item["content"] = str(item.get("content") or "")[:3000]
            item["reasoning_content"] = str(item.get("reasoning_content") or "")[:1000]
            if item.get("tool_calls"):
                tool_calls = item.get("tool_calls")
                if isinstance(tool_calls, list):
                    item["tool_calls"] = [self._trim_tool_call(tool_call) for tool_call in tool_calls[:12]]
                else:
                    item["tool_calls"] = self._safe_preview(tool_calls, 1200)
            if item.get("tool_results"):
                tool_results = item.get("tool_results")
                if isinstance(tool_results, list):
                    item["tool_results"] = [self._trim_tool_result(tool_result) for tool_result in tool_results[:12]]
                else:
                    item["tool_results"] = self._safe_preview(tool_results, 1200)
            if item.get("report_links"):
                item["report_links"] = item["report_links"][:10]
            trimmed.append(item)
        return trimmed

    def _trim_tool_call(self, tool_call: Any) -> dict[str, Any]:
        if not isinstance(tool_call, dict):
            return {"preview": self._safe_preview(tool_call, 1000)}
        trimmed: dict[str, Any] = {
            "tool": str(tool_call.get("tool") or tool_call.get("name") or "unknown"),
        }
        if tool_call.get("arguments") is not None:
            trimmed["arguments_preview"] = self._safe_preview(tool_call.get("arguments"), 800)
        if tool_call.get("result") is not None:
            trimmed["result"] = self._trim_tool_result(tool_call.get("result"))
        return trimmed

    def _trim_tool_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            trimmed: dict[str, Any] = {"fields": list(result.keys())[:20]}
            for key in (
                "status",
                "message",
                "summary",
                "note",
                "report_id",
                "job_id",
                "error",
            ):
                if key in result:
                    trimmed[key] = self._safe_scalar(result.get(key), 500)
            nested = result.get("result")
            if isinstance(nested, dict):
                trimmed["result_fields"] = list(nested.keys())[:20]
                for key in ("status", "message", "summary", "report_id", "job_id"):
                    if key in nested:
                        trimmed[f"result_{key}"] = self._safe_scalar(nested.get(key), 500)
            elif nested is not None:
                trimmed["result_preview"] = self._safe_preview(nested, 1000)
            trimmed["preview"] = self._safe_preview(result, 1500)
            return trimmed
        if isinstance(result, list):
            return {
                "type": "list",
                "count": len(result),
                "preview": self._safe_preview(result[:5], 1500),
            }
        return {"preview": self._safe_preview(result, 1200)}

    def _safe_scalar(self, value: Any, limit: int) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value)
            return text[:limit] if isinstance(value, str) else value
        return self._safe_preview(value, limit)

    def _safe_preview(self, value: Any, limit: int) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]

    def _merge_tool_results(self, memory: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        merged = self._normalize_memory(json.loads(json.dumps(memory, ensure_ascii=False, default=str)))
        evidence = merged.setdefault("evidence", {})
        existing = evidence.get("tool_results") or []
        seen_keys = set()
        normalized_existing = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            key = item.get("source_message_id") or json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            normalized_existing.append(item)

        extracted = []
        for msg in messages:
            message_id = msg.get("message_id")
            created_at = msg.get("created_at")
            tool_calls = msg.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                result = tc.get("result")
                if result is None:
                    continue
                tool_name = str(tc.get("tool") or "unknown")
                tool_evidence = self._build_tool_result_evidence(
                    tool_name=tool_name,
                    result=result,
                    message_id=message_id,
                    created_at=created_at,
                )
                if tool_evidence is not None:
                    extracted.append(tool_evidence)

        for item in extracted:
            key = item.get("source_message_id") or json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            normalized_existing.append(item)

        evidence["tool_results"] = normalized_existing[-24:]
        merged["evidence"] = evidence
        return merged

    def _build_tool_result_evidence(
        self,
        tool_name: str,
        result: Any,
        message_id: Any,
        created_at: Any,
    ) -> dict[str, Any] | None:
        if result is None:
            return None
        preview = json.dumps(result, ensure_ascii=False, default=str)
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary") or result.get("message") or "")
        if not summary:
            summary = self._summarize_tool_result(tool_name, result)
        return {
            "tool_name": tool_name,
            "source_message_id": int(message_id) if str(message_id or "").isdigit() else message_id,
            "created_at": created_at,
            "summary": summary[:500],
            "result_preview": preview[:800],
        }

    def _summarize_tool_result(self, tool_name: str, result: Any) -> str:
        if isinstance(result, dict):
            keys = ", ".join(list(result.keys())[:10])
            return f"{tool_name} 返回对象，字段：{keys}"
        if isinstance(result, list):
            return f"{tool_name} 返回列表，共 {len(result)} 项"
        return f"{tool_name} 返回结果"

    def _extract_json(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("compact output must be a JSON object")
        return data

    def _validate_memory(self, memory: dict[str, Any], latest_id: int) -> None:
        memory = self._normalize_memory(memory)
        for key in ("stable_memory", "working_state", "evidence", "risk_notes", "forget_candidates"):
            if key not in memory:
                raise ValueError(f"compact output missing key: {key}")
        reported_id = int(memory.get("last_compacted_message_id") or 0)
        if reported_id != latest_id:
            raise ValueError(f"invalid last_compacted_message_id: {reported_id}, expected {latest_id}")
        if not isinstance(memory.get("stable_memory"), dict):
            raise ValueError("stable_memory must be object")
        if not isinstance(memory.get("working_state"), dict):
            raise ValueError("working_state must be object")
        if not isinstance(memory.get("evidence"), dict):
            raise ValueError("evidence must be object")
        if not isinstance(memory["evidence"].get("tool_results", []), list):
            raise ValueError("evidence.tool_results must be list")

    def _normalize_memory(self, memory: dict[str, Any]) -> dict[str, Any]:
        normalized = json.loads(json.dumps(EMPTY_MEMORY, ensure_ascii=False, default=str))
        incoming = json.loads(json.dumps(memory or {}, ensure_ascii=False, default=str))
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                normalized[key] = value
        evidence = normalized.setdefault("evidence", {})
        legacy_artifacts = normalized.pop("artifacts", None)
        if isinstance(legacy_artifacts, dict):
            for key in ("reports", "analysis_jobs", "pending_actions", "tool_results"):
                current = evidence.get(key)
                legacy = legacy_artifacts.get(key)
                if not current and legacy:
                    evidence[key] = legacy
        for key in ("reports", "analysis_jobs", "pending_actions", "tool_results"):
            if not isinstance(evidence.get(key), list):
                evidence[key] = []
        normalized["evidence"] = evidence
        return normalized

    def _estimate_tokens(self, text: str) -> int:
        # Conservative mixed Chinese/English approximation; intentionally avoids frequent compaction.
        return max(1, len(text) // 2)


memory_manager = MemoryManager()
