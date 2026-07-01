from __future__ import annotations

import hashlib
import json

from jsonschema import Draft7Validator

from backend.core.models import ApprovalPolicy, RiskLevel, ToolCall
from backend.core.policy import default_risk_for_permission, permission_label, requires_confirmation, risk_label
from backend.services.approval import approval_store
from backend.services.audit import log_event
from backend.services.capabilities import capability_service
from backend.services.observability import trace_store
from backend.services.resource_guard import ResourceLimitExceeded, resource_guard
from backend.services.retry import run_with_retry, should_retry_tool_error, tool_retry_allowed
from backend.services.safety import SafetyViolation, validate_tool_arguments
from backend.services.sessions import chat_session_store
from backend.tools.bootstrap import build_registry
from backend.core.skill_manager import skill_manager


MAX_INLINE_TOOL_RESULT_CHARS = 16_000


class ToolGateway:
    def __init__(self) -> None:
        self.registry = build_registry()

    def invoke(self, call: ToolCall, session_id: str = "default", run_id: str | None = None, trace_id: str | None = None) -> dict:
        spec = self.registry.get(call.name)
        arguments = self._validate_arguments(spec, call.arguments)
        self._check_tool_allowed(spec, session_id, trace_id)
        skill_required = self._skill_required_result(spec.name, session_id)
        if skill_required:
            return {"pending": False, "result": skill_required}
        recoverable = self._recoverable_argument_result(spec.name, arguments)
        if recoverable:
            return {"pending": False, "result": recoverable}
        risk, risk_reason = spec.assess_risk(arguments, default_risk_for_permission(spec.permission))
        policy = self._approval_policy(session_id)
        span_cm = trace_store.span(
            trace_id,
            f"tool.{call.name}",
            "tool",
            input={"arguments": arguments},
            metadata={"risk": risk.value, "risk_reason": risk_reason, "policy": policy.value},
        ) if trace_id else None
        span = span_cm.__enter__() if span_cm else None
        if requires_confirmation(spec.permission, risk, policy):
            action = self.create_pending_action(
                call.name,
                arguments,
                session_id=session_id,
                run_id=run_id,
                source="pending_action",
                risk=risk,
                risk_reason=risk_reason,
                trace_id=trace_id,
            )
            if span:
                trace_store.finish_span(span, output={"pending": True, "action_id": action.get("action_id") if action else None})
                span_cm = None
            return {"pending": True, "action": action}

        try:
            resource_guard.check_and_record_tool(session_id, run_id, call.name, spec.group)
            result = self._execute_handler_with_recovery(spec, arguments, session_id, run_id, trace_id)
            result = self._enforce_result_budget(call.name, result)
            log_event(
                "tool_executed",
                {"tool": call.name, "arguments": arguments, "risk": risk.value, "policy": policy.value},
            )
            if span:
                trace_store.finish_span(span, output={"pending": False, "result": result})
                span_cm = None
            return {"pending": False, "result": result}
        except Exception as exc:
            if span:
                trace_store.finish_span(span, status="failed", error=str(exc))
                span_cm = None
            raise
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    def create_pending_recommended_action(
        self,
        result: object,
        session_id: str = "default",
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict | None:
        recommended = self._extract_recommended_action(result)
        if not recommended:
            return None
        tool_name = recommended.get("tool")
        arguments = recommended.get("arguments") or {}
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return None
        spec = self.registry.get(tool_name)
        arguments = self._validate_arguments(spec, arguments)
        self._check_tool_allowed(spec, session_id, trace_id)
        risk, risk_reason = spec.assess_risk(arguments, default_risk_for_permission(spec.permission))
        policy = self._approval_policy(session_id)
        if not requires_confirmation(spec.permission, risk, policy):
            resource_guard.check_and_record_tool(session_id, run_id, tool_name, spec.group)
            result_payload = self._execute_handler_with_recovery(spec, arguments, session_id, run_id, trace_id)
            result_payload = self._enforce_result_budget(tool_name, result_payload)
            log_event(
                "recommended_action_auto_executed",
                {"tool": tool_name, "arguments": arguments, "risk": risk.value, "policy": policy.value},
            )
            if trace_id:
                trace_store.event(
                    trace_id,
                    "recommended_action.auto_executed",
                    {"tool": tool_name, "arguments": arguments, "risk": risk.value},
                )
            return {
                "auto_executed": True,
                "tool_name": tool_name,
                "arguments": arguments,
                "risk": risk.value,
                "risk_reason": risk_reason,
                "result": result_payload,
            }
        action = self.create_pending_action(
            tool_name,
            arguments,
            session_id=session_id,
            run_id=run_id,
            source="recommended",
            risk=risk,
            risk_reason=risk_reason or str(recommended.get("reason") or ""),
            trace_id=trace_id,
        )
        if action is None:
            return None
        log_event(
            "recommended_pending_action_created",
            {"action": action, "source_result": self._summarize_recommended_source(result)},
        )
        return action

    def invoke_tool_call(self, name: str, arguments_json: str, session_id: str = "default", run_id: str | None = None, trace_id: str | None = None) -> dict:
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid tool arguments json: {exc}") from exc
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        return self.invoke(ToolCall(name=name, arguments=arguments), session_id=session_id, run_id=run_id, trace_id=trace_id)

    def create_pending_action(
        self,
        tool_name: str,
        arguments: dict,
        session_id: str = "default",
        run_id: str | None = None,
        source: str = "runtime_guard",
        risk: RiskLevel | None = None,
        risk_reason: str = "",
        trace_id: str | None = None,
    ) -> dict | None:
        spec = self.registry.get(tool_name)
        validated_arguments = self._validate_arguments(spec, arguments)
        self._check_tool_allowed(spec, session_id, trace_id)
        assessed_risk = risk or spec.assess_risk(validated_arguments, default_risk_for_permission(spec.permission))[0]
        if not requires_confirmation(spec.permission, assessed_risk, self._approval_policy(session_id)):
            return None
        idempotency_key = self._idempotency_key(session_id, run_id, tool_name, validated_arguments)
        action = approval_store.create(
            tool_name=tool_name,
            arguments=validated_arguments,
            permission=spec.permission,
            risk=assessed_risk,
            risk_reason=risk_reason,
            reason=risk_reason or f"{permission_label(spec.permission)}需要用户确认：{risk_label(assessed_risk)}",
            session_id=session_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )
        event_type = "pending_action_created" if source == "pending_action" else f"{source}_pending_action_created"
        log_event(event_type, action.model_dump())
        if trace_id:
            trace_store.event(trace_id, "approval.required", {"action": action.model_dump(), "source": source})
        chat_session_store.enqueue_approval_action(session_id, action.action_id)
        return action.model_dump()

    def execute_confirmed(self, action_id: str, trace_id: str | None = None) -> dict:
        action = approval_store.get(action_id)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        if action.status != "pending":
            return {"status": action.status, "action": action.model_dump()}
        if action.expires_at and action.expires_at < self._now():
            approval_store.update(action_id, status="expired", error="approval action expired", decided_at=self._now())
            if trace_id:
                trace_store.event(trace_id, "approval.expired", {"action": action.model_dump()}, level="warn")
            return {"status": "expired", "action": action.model_dump()}

        spec = self.registry.get(action.tool_name)
        span_cm = trace_store.span(
            trace_id,
            f"approval.execute.{action.tool_name}",
            "approval",
            input={"action_id": action_id, "arguments": action.arguments},
            metadata={"risk": action.risk.value, "risk_reason": action.risk_reason},
        ) if trace_id else None
        span = span_cm.__enter__() if span_cm else None
        try:
            self._check_tool_allowed(spec, action.session_id, trace_id)
            resource_guard.check_and_record_tool(action.session_id, action.run_id, action.tool_name, spec.group)
            approval_store.update(action_id, status="running")
            result = self._execute_handler_with_recovery(spec, action.arguments, action.session_id, action.run_id, trace_id)
            result = self._enforce_result_budget(action.tool_name, result)
            updated = approval_store.update(
                action_id,
                status="executed",
                result=result if isinstance(result, dict) else {"value": result},
                executed_at=self._now(),
            )
            log_event("pending_action_executed", updated.model_dump() if updated else action.model_dump())
            if span:
                trace_store.finish_span(span, output={"status": "executed", "result": result})
                span_cm = None
            return {"status": "executed", "result": result}
        except Exception as exc:
            approval_store.mark(action_id, "failed")
            approval_store.update(action_id, error=str(exc))
            log_event("pending_action_failed", {"action": action.model_dump(), "error": str(exc)})
            if span:
                trace_store.finish_span(span, status="failed", error=str(exc))
                span_cm = None
            raise
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    def _execute_handler(self, spec, arguments: dict, session_id: str, run_id: str | None) -> object:
        return self._call_handler(spec.handler, arguments, session_id, run_id)

    def _execute_handler_with_recovery(self, spec, arguments: dict, session_id: str, run_id: str | None, trace_id: str | None) -> object:
        if spec.name == "search_stock_symbol":
            return self._execute_handler(spec, arguments, session_id, run_id)
        if not tool_retry_allowed(spec.permission):
            return self._execute_handler(spec, arguments, session_id, run_id)

        def on_retry(attempt: int, exc: Exception, delay: float) -> None:
            payload = {
                "tool": spec.name,
                "attempt": attempt,
                "delay_seconds": delay,
                "error": str(exc),
            }
            log_event("tool_retry_scheduled", payload)
            if trace_id:
                trace_store.event(trace_id, "tool.retry_scheduled", payload, level="warn")

        return run_with_retry(
            lambda: self._execute_handler(spec, arguments, session_id, run_id),
            retryable=should_retry_tool_error,
            on_retry=on_retry,
        )

    def _call_handler(self, handler, arguments: dict, session_id: str, run_id: str | None) -> object:
        try:
            return handler(**arguments, session_id=session_id, run_id=run_id)
        except TypeError as exc:
            if "session_id" not in str(exc) and "run_id" not in str(exc):
                raise
            return handler(**arguments)

    def _enforce_result_budget(self, tool_name: str, result: object) -> object:
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= MAX_INLINE_TOOL_RESULT_CHARS:
            return result
        return {
            "status": "tool_result_exceeded_budget",
            "tool_name": tool_name,
            "result_chars": len(text),
            "max_inline_chars": MAX_INLINE_TOOL_RESULT_CHARS,
            "message": (
                "工具结果超过单次上下文预算，系统不会保存或返回超长结果。"
                "请改用该工具的 mode/section/field/offset/limit/max_chars 参数缩小读取范围，"
                "或使用更具体的问题重新查询。"
            ),
            "result_shape": self._result_shape(result),
        }

    def _result_shape(self, result: object) -> object:
        if isinstance(result, dict):
            return {
                "type": "object",
                "fields": list(result.keys())[:30],
            }
        if isinstance(result, list):
            return {
                "type": "list",
                "count": len(result),
                "item_fields": list(result[0].keys())[:20] if result and isinstance(result[0], dict) else None,
            }
        return {"type": type(result).__name__}

    def _validate_arguments(self, spec, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        validator = Draft7Validator(spec.parameters)
        errors = sorted(validator.iter_errors(arguments), key=lambda item: item.path)
        if errors:
            message = "; ".join(error.message for error in errors[:3])
            raise ValueError(f"invalid arguments for {spec.name}: {message}")
        try:
            return validate_tool_arguments(spec.name, arguments)
        except SafetyViolation as exc:
            log_event("unsafe_argument_rejected", {"tool": spec.name, "code": exc.code, "message": str(exc), "arguments": arguments})
            raise

    def _skill_required_result(self, tool_name: str, session_id: str) -> dict | None:
        required = skill_manager.required_skill_for_tool(tool_name)
        if not required:
            return None
        if skill_manager.is_tool_skill_active(tool_name, session_id=session_id):
            return None
        return {
            "status": "skill_required",
            "tool_name": tool_name,
            "required_skill": required,
            "message": f"调用 {tool_name} 前必须先调用 activate_skill(name=\"{required}\") 读取完整使用规范。",
            "next_action": {
                "tool": "activate_skill",
                "arguments": {"name": required},
            },
        }

    def _recoverable_argument_result(self, tool_name: str, arguments: dict) -> dict | None:
        if tool_name in {"query_report", "get_report_detail", "read_report_section", "delete_report"}:
            report_id = str(arguments.get("report_id") or "").strip()
            if not self._looks_like_report_id(report_id):
                return {
                    "status": "invalid_report_id",
                    "tool_name": tool_name,
                    "report_id": report_id,
                    "message": "report_id 必须是 list_report_catalog 返回的完整报告 ID，不能使用股票代码、股票名或日期。",
                    "next_action": {
                        "tool": "list_report_catalog",
                        "arguments": {"subject": report_id} if report_id else {},
                    },
                }
        if tool_name == "read_report_section" and not str(arguments.get("section_id") or "").strip():
            return {
                "status": "missing_section_id",
                "tool_name": tool_name,
                "message": "read_report_section 需要 section_id。请先调用 get_report_detail 获取 manifest.sections。",
            }
        if tool_name in {"get_stock_snapshot", "get_stock_data_package", "run_stock_research", "get_stock_research_status", "recommend_stock_research_action"}:
            ticker = str(arguments.get("ticker") or "").strip().upper()
            if not self._looks_like_a_share_ticker(ticker):
                return {
                    "status": "ticker_required",
                    "tool_name": tool_name,
                    "ticker": ticker,
                    "message": "该工具需要标准 A 股 ticker，例如 601899.SH。若用户提供中文名或模糊代码，请先调用 search_stock_symbol。",
                    "next_action": {
                        "tool": "search_stock_symbol",
                        "arguments": {"query": ticker} if ticker else {},
                    },
                }
        if tool_name == "read_industry_graph_node":
            node_id = str(arguments.get("node_id") or "").strip()
            if not node_id:
                return {
                    "status": "node_reference_required",
                    "tool_name": tool_name,
                    "message": "read_industry_graph_node 需要 node_id 或图谱摘要中的 node_ref。请先调用 read_industry_graph 获取节点目录。",
                }
        return None

    def _looks_like_report_id(self, value: str) -> bool:
        if not value or ":" not in value:
            return False
        parts = [part for part in value.split(":") if part]
        return len(parts) >= 3 and parts[0] in {"stock_research", "market_discovery", "theme_deep_dive", "trading_plan"}

    def _looks_like_a_share_ticker(self, value: str) -> bool:
        import re

        return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value))

    def _approval_policy(self, session_id: str) -> ApprovalPolicy:
        return chat_session_store.get_security_settings(session_id).approval_policy

    def _check_tool_allowed(self, spec, session_id: str, trace_id: str | None = None) -> None:
        if spec.name in capability_service.disabled_external_tools():
            payload = {"tool": spec.name, "reason": "capability_disabled"}
            log_event("tool_not_allowed", payload)
            if trace_id:
                trace_store.event(trace_id, "security.capability_disabled", payload, level="warn")
            raise PermissionError(f"capability disabled for tool: {spec.name}")
        settings = chat_session_store.get_security_settings(session_id)
        if spec.group not in settings.allowed_tool_groups:
            payload = {"tool": spec.name, "group": spec.group, "allowed_groups": settings.allowed_tool_groups}
            log_event("tool_not_allowed", payload)
            if trace_id:
                trace_store.event(trace_id, "security.tool_not_allowed", payload, level="warn")
            raise PermissionError(f"tool group not allowed: {spec.group}")

    def _idempotency_key(self, session_id: str, run_id: str | None, tool_name: str, arguments: dict) -> str:
        payload = json.dumps(
            {
                "session_id": session_id,
                "run_id": run_id,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _now(self) -> str:
        from datetime import datetime

        return datetime.now().isoformat(timespec="seconds")

    def _extract_recommended_action(self, result: object) -> dict | None:
        if not isinstance(result, dict):
            return None
        if isinstance(result.get("recommended_action"), dict):
            return result["recommended_action"]
        nested = result.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("recommended_action"), dict):
            return nested["recommended_action"]
        return None

    def _summarize_recommended_source(self, result: object) -> dict:
        if not isinstance(result, dict):
            return {}
        return {
            "status": result.get("status"),
            "ticker": result.get("ticker"),
            "message": result.get("message"),
            "missing": result.get("missing"),
        }


tool_gateway = ToolGateway()
