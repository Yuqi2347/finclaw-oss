from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from json import JSONDecodeError
from typing import Any

import requests

from backend.core.env import settings
from backend.core.llm_client import LLMNotConfiguredError, raise_for_status_with_detail
from backend.services.observability import trace_store
from backend.services.retry import run_with_retry, should_retry_llm_error


@dataclass
class StreamChunk:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


class OpenAIStreamClient:
    def __init__(self) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.llm_configured

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> Iterator[StreamChunk]:
        if not self.configured:
            raise LLMNotConfiguredError("FINCLAW_LLM_API_KEY is not configured")

        thinking_enabled = self.settings.llm_thinking_payload is not None
        request_thinking_payload = self.settings.llm_request_thinking_payload
        prepared_messages = self._prepare_messages(messages, thinking_enabled=thinking_enabled)
        payload = {
            "model": self.settings.llm_model,
            "messages": prepared_messages,
            "temperature": self.settings.llm_temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if request_thinking_payload:
            payload["thinking"] = request_thinking_payload
        trace_meta = self._trace_meta(trace_id)
        span_context = None
        span_cm = None
        if trace_id:
            span_cm = trace_store.span(
                trace_id,
                "llm.chat.completions",
                "llm",
                parent_span_id=parent_span_id,
                input={
                    "message_count": len(messages),
                    "tools_count": len(tools),
                    "last_message": prepared_messages[-1] if prepared_messages else None,
                },
                metadata={"model": self.settings.llm_model, "base_url": self.settings.llm_base_url, "tool_choice": tool_choice},
            )
            span_context = span_cm.__enter__()
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="milliseconds")
        first_token_ms = None
        output_chars = 0
        finish_reason = None
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        malformed_sse_preview: str | None = None
        try:
            response = run_with_retry(
                self._open_stream_response(payload),
                retryable=should_retry_llm_error,
                on_retry=lambda attempt, exc, delay: trace_store.event(
                    trace_id,
                    "llm.retry_scheduled",
                    {"attempt": attempt, "delay_seconds": delay, "error": str(exc)},
                    level="warn",
                ) if trace_id else None,
            )
            with response as resp:
                resp.encoding = "utf-8"
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if (time.perf_counter() - started) > self.settings.llm_timeout:
                        raise TimeoutError(f"LLM stream exceeded timeout after {self.settings.llm_timeout}s")
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        item = json.loads(data)
                    except JSONDecodeError as exc:
                        malformed_sse_preview = data[:1200]
                        if output_chars == 0 and not tool_call_parts:
                            for chunk in self._fallback_non_stream_chat(
                                payload,
                                trace_id=trace_id,
                                trace_meta=trace_meta,
                                tool_choice=tool_choice,
                                started_at=started_at,
                                stream_started=started,
                                first_token_ms=first_token_ms,
                                parse_error=exc,
                                malformed_sse_preview=malformed_sse_preview,
                            ):
                                if chunk.content:
                                    output_chars += len(chunk.content)
                                    content_parts.append(chunk.content)
                                if chunk.reasoning_content:
                                    reasoning_parts.append(chunk.reasoning_content)
                                if chunk.tool_calls:
                                    self._merge_tool_call_deltas(tool_call_parts, chunk.tool_calls)
                                finish_reason = chunk.finish_reason or finish_reason
                                yield chunk
                            if span_context:
                                trace_store.finish_span(
                                    span_context,
                                    output={"output_chars": output_chars, "finish_reason": finish_reason, "fallback": "non_stream"},
                                    metrics={"first_token_ms": first_token_ms, "output_chars": output_chars},
                                )
                                span_cm = None
                            return
                        raise RuntimeError(
                            "LLM stream returned malformed SSE JSON after partial output; "
                            f"provider={self.settings.llm_base_url}; preview={malformed_sse_preview!r}"
                        ) from exc
                    choices = item.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content") or ""
                    reasoning_content = (delta.get("reasoning_content") or "") if thinking_enabled else ""
                    if content and first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    output_chars += len(content)
                    if content:
                        content_parts.append(content)
                    if reasoning_content:
                        reasoning_parts.append(reasoning_content)
                    if delta.get("tool_calls"):
                        self._merge_tool_call_deltas(tool_call_parts, delta.get("tool_calls") or [])
                    finish_reason = choice.get("finish_reason") or finish_reason
                    yield StreamChunk(
                        content=content,
                        reasoning_content=reasoning_content,
                        tool_calls=delta.get("tool_calls"),
                        finish_reason=choice.get("finish_reason"),
                    )
                if (time.perf_counter() - started) > self.settings.llm_timeout:
                    raise TimeoutError(f"LLM stream exceeded timeout after {self.settings.llm_timeout}s")
            if span_context:
                trace_store.finish_span(
                    span_context,
                    output={"output_chars": output_chars, "finish_reason": finish_reason},
                    metrics={"first_token_ms": first_token_ms, "output_chars": output_chars},
                )
                span_cm = None
            self._record_llm_log(
                trace_id=trace_id,
                trace_meta=trace_meta,
                payload=payload,
                tool_choice=tool_choice,
                started_at=started_at,
                started=started,
                first_token_ms=first_token_ms,
                status="completed",
                response={
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                    "tool_calls": [tool_call_parts[idx] for idx in sorted(tool_call_parts)],
                    "finish_reason": finish_reason,
                    "output_chars": output_chars,
                },
            )
        except Exception as exc:
            if span_context:
                trace_store.finish_span(span_context, status="failed", error=str(exc))
                span_cm = None
            self._record_llm_log(
                trace_id=trace_id,
                trace_meta=trace_meta,
                payload=payload,
                tool_choice=tool_choice,
                started_at=started_at,
                started=started,
                first_token_ms=first_token_ms,
                status="failed",
                error=str(exc),
                response={
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                    "tool_calls": [tool_call_parts[idx] for idx in sorted(tool_call_parts)],
                    "finish_reason": finish_reason,
                    "output_chars": output_chars,
                    "malformed_sse_preview": malformed_sse_preview,
                },
            )
            raise
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    def _open_stream_response(self, payload: dict[str, Any]):
        def request() -> requests.Response:
            resp = requests.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.settings.llm_timeout,
                stream=True,
            )
            raise_for_status_with_detail(resp)
            return resp

        return request

    def _fallback_non_stream_chat(
        self,
        payload: dict[str, Any],
        *,
        trace_id: str | None,
        trace_meta: dict[str, Any],
        tool_choice: str | dict[str, Any],
        started_at: str,
        stream_started: float,
        first_token_ms: int | None,
        parse_error: JSONDecodeError,
        malformed_sse_preview: str,
    ) -> Iterator[StreamChunk]:
        fallback_started = time.perf_counter()
        fallback_payload = dict(payload)
        fallback_payload["stream"] = False
        try:
            trace_store.event(
                trace_id,
                "llm.stream_malformed_fallback",
                {
                    "error": str(parse_error),
                    "malformed_sse_preview": malformed_sse_preview,
                    "request_chars": len(json.dumps(fallback_payload, ensure_ascii=False, default=str)),
                },
                level="warn",
            ) if trace_id else None
            resp = requests.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=fallback_payload,
                timeout=self.settings.llm_timeout,
            )
            raw_text = resp.text
            raise_for_status_with_detail(resp)
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            reasoning_content = message.get("reasoning_content") or ""
            tool_calls = message.get("tool_calls") or []
            finish_reason = choice.get("finish_reason")
            output_chars = len(content)
            response = {
                "content": content,
                "reasoning_content": reasoning_content,
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
                "output_chars": output_chars,
                "fallback": "non_stream_after_malformed_sse",
                "malformed_sse_preview": malformed_sse_preview,
                "raw_response": data,
            }
            self._record_llm_log(
                trace_id=trace_id,
                trace_meta=trace_meta,
                payload=fallback_payload,
                tool_choice=tool_choice,
                started_at=started_at,
                started=stream_started,
                first_token_ms=first_token_ms,
                status="completed",
                response=response,
            )
            yield StreamChunk(
                content=content,
                reasoning_content=reasoning_content if self.settings.llm_thinking_payload is not None else "",
                tool_calls=[
                    {
                        "index": index,
                        "id": call.get("id", ""),
                        "type": call.get("type", "function"),
                        "function": call.get("function") or {},
                    }
                    for index, call in enumerate(tool_calls)
                ] or None,
                finish_reason=finish_reason,
            )
        except Exception as exc:
            self._record_llm_log(
                trace_id=trace_id,
                trace_meta=trace_meta,
                payload=fallback_payload,
                tool_choice=tool_choice,
                started_at=started_at,
                started=fallback_started,
                first_token_ms=first_token_ms,
                status="failed",
                error=(
                    "stream malformed and non-stream fallback failed: "
                    f"{exc}; stream_parse_error={parse_error}; malformed_sse_preview={malformed_sse_preview!r}"
                ),
                response={
                    "fallback": "non_stream_after_malformed_sse",
                    "raw_response_text": locals().get("raw_text", "")[:4000],
                    "malformed_sse_preview": malformed_sse_preview,
                },
            )
            raise RuntimeError(
                "LLM stream returned malformed SSE JSON and non-stream fallback failed; "
                f"provider={self.settings.llm_base_url}; preview={malformed_sse_preview!r}; fallback_error={exc}"
            ) from exc

    def _prepare_messages(self, messages: list[dict[str, Any]], thinking_enabled: bool | None = None) -> list[dict[str, Any]]:
        if thinking_enabled is None:
            thinking_enabled = self.settings.llm_thinking_payload is not None
        if thinking_enabled:
            return messages
        return [_strip_reasoning_content(message) for message in messages]

    def _trace_meta(self, trace_id: str | None) -> dict[str, Any]:
        if not trace_id:
            return {}
        try:
            trace = trace_store.get_trace(trace_id)
            if not trace:
                return {}
            row = trace.get("trace") or {}
            return {"session_id": row.get("session_id"), "run_id": row.get("run_id")}
        except Exception:
            return {}

    def _record_llm_log(
        self,
        *,
        trace_id: str | None,
        trace_meta: dict[str, Any],
        payload: dict[str, Any],
        tool_choice: str | dict[str, Any],
        started_at: str,
        started: float,
        first_token_ms: int | None,
        status: str,
        response: dict[str, Any],
        error: str | None = None,
    ) -> None:
        try:
            trace_store.record_llm_call(
                trace_id=trace_id,
                session_id=trace_meta.get("session_id"),
                run_id=trace_meta.get("run_id"),
                model=self.settings.llm_model,
                base_url=self.settings.llm_base_url,
                tool_choice=tool_choice,
                temperature=self.settings.llm_temperature,
                request=self._safe_payload_for_log(payload),
                response=response,
                status=status,
                error=error,
                started_at=started_at,
                completed_at=datetime.now().isoformat(timespec="milliseconds"),
                duration_ms=int((time.perf_counter() - started) * 1000),
                first_token_ms=first_token_ms,
            )
        except Exception:
            # Observability must never break the user-facing stream.
            return

    def _safe_payload_for_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(_redact_image_payload(payload), ensure_ascii=False, default=str))

    def _merge_tool_call_deltas(self, tool_call_parts: dict[int, dict[str, Any]], deltas: list[dict[str, Any]]) -> None:
        for delta in deltas:
            index = int(delta.get("index", 0))
            current = tool_call_parts.setdefault(
                index,
                {"id": delta.get("id", ""), "type": delta.get("type", "function"), "function": {"name": "", "arguments": ""}},
            )
            if delta.get("id"):
                current["id"] = delta["id"]
            if delta.get("type"):
                current["type"] = delta["type"]
            fn_delta = delta.get("function") or {}
            fn = current.setdefault("function", {"name": "", "arguments": ""})
            if fn_delta.get("name"):
                fn["name"] = str(fn.get("name") or "") + str(fn_delta["name"])
            if fn_delta.get("arguments"):
                fn["arguments"] = str(fn.get("arguments") or "") + str(fn_delta["arguments"])


openai_stream_client = OpenAIStreamClient()


def _strip_reasoning_content(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_reasoning_content(item) for key, item in value.items() if key != "reasoning_content"}
    if isinstance(value, list):
        return [_strip_reasoning_content(item) for item in value]
    return value


def _redact_image_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "url" and isinstance(item, str) and item.startswith("data:image/"):
                media_type = item.split(";", 1)[0].replace("data:", "")
                redacted[key] = f"[redacted {media_type} data_url]"
            else:
                redacted[key] = _redact_image_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_image_payload(item) for item in value]
    return value
