from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

import requests

from backend.core.env import settings
from backend.services.observability import trace_store


class LLMNotConfiguredError(RuntimeError):
    pass


def raise_for_status_with_detail(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = (resp.text or "").strip()
        if detail:
            detail = detail[:2000]
            raise requests.HTTPError(
                f"{exc}. response_body={detail}",
                request=resp.request,
                response=resp,
            ) from exc
        raise


class LLMClient:
    def __init__(self) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.llm_configured

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        purpose: str = "json_completion",
    ) -> dict[str, Any]:
        try:
            content = self.chat_text(
                messages,
                response_format={"type": "json_object"},
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                purpose=purpose,
            )
            return self._parse_json(content)
        except Exception:
            if not self._needs_json_format_fallback():
                raise
            content = self.chat_text(
                messages,
                response_format=None,
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                purpose=f"{purpose}.fallback_no_response_format",
            )
            return self._parse_json(content)

    def chat_text(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        purpose: str = "text_completion",
    ) -> str:
        if not self.configured:
            raise LLMNotConfiguredError("FINCLAW_LLM_API_KEY is not configured")

        thinking_enabled = self.settings.llm_thinking_payload is not None
        request_thinking_payload = self.settings.llm_request_thinking_payload
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": self._prepare_messages(messages, thinking_enabled=thinking_enabled),
            "temperature": self.settings.llm_temperature,
        }
        if request_thinking_payload:
            payload["thinking"] = request_thinking_payload
        if response_format:
            payload["response_format"] = response_format

        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="milliseconds")
        try:
            resp = requests.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.settings.llm_timeout,
            )
            raw_text = resp.text
            raise_for_status_with_detail(resp)
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"LLM returned non-JSON HTTP response: {raw_text[:1200]}") from exc
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
            self._record_llm_log(
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                payload=payload,
                started_at=started_at,
                started=started,
                response={"content": content, "raw_response": data, "purpose": purpose},
                status="completed",
            )
            return content
        except Exception as exc:
            self._record_llm_log(
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                payload=payload,
                started_at=started_at,
                started=started,
                response={"purpose": purpose, "raw_response_text": locals().get("raw_text", "")[:4000]},
                status="failed",
                error=str(exc),
            )
            raise

    def _prepare_messages(self, messages: list[dict[str, Any]], thinking_enabled: bool | None = None) -> list[dict[str, Any]]:
        if thinking_enabled is None:
            thinking_enabled = self.settings.llm_thinking_payload is not None
        if thinking_enabled:
            return messages
        return [_strip_reasoning_content(message) for message in messages]

    def _parse_json(self, content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def _needs_json_format_fallback(self) -> bool:
        base_url = (self.settings.llm_base_url or "").lower()
        model = (self.settings.llm_model or "").lower()
        return "xiaomimimo" in base_url or "mimo" in model

    def _record_llm_log(
        self,
        *,
        trace_id: str | None,
        session_id: str | None,
        run_id: str | None,
        payload: dict[str, Any],
        started_at: str,
        started: float,
        response: dict[str, Any],
        status: str,
        error: str | None = None,
    ) -> None:
        try:
            trace_store.record_llm_call(
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                model=self.settings.llm_model,
                base_url=self.settings.llm_base_url,
                tool_choice="none",
                temperature=self.settings.llm_temperature,
                request=json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
                response=response,
                status=status,
                error=error,
                started_at=started_at,
                completed_at=datetime.now().isoformat(timespec="milliseconds"),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception:
            return


llm_client = LLMClient()


def _strip_reasoning_content(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_reasoning_content(item) for key, item in value.items() if key != "reasoning_content"}
    if isinstance(value, list):
        return [_strip_reasoning_content(item) for item in value]
    return value
