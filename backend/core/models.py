from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Permission(str, Enum):
    READ = "read"
    ANALYZE_CACHED = "analyze_cached"
    LOW_RISK_REFRESH = "low_risk_refresh"
    EXPENSIVE_CONFIRM = "expensive_confirm"
    WRITE_CONFIRM = "write_confirm"
    DANGEROUS_WRITE = "dangerous_write"


class RiskLevel(str, Enum):
    SAFE_READ = "safe_read"
    LOW_EXPENSIVE = "low_expensive"
    MEDIUM_EXPENSIVE = "medium_expensive"
    HIGH_EXPENSIVE = "high_expensive"
    WRITE = "write"
    DANGEROUS = "dangerous"


class ApprovalPolicy(str, Enum):
    STRICT = "strict"
    BALANCED = "balanced"
    AUTO_LOW_RISK = "auto_low_risk"
    ALWAYS_ASK = "always_ask"
    NEVER = "never"


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    mode: str | None = None


class SessionCreateRequest(BaseModel):
    title: str | None = None


class InterruptRequest(BaseModel):
    message: str
    run_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    pending_actions: list[dict[str, Any]] = Field(default_factory=list)


class SessionSecuritySettings(BaseModel):
    approval_policy: ApprovalPolicy = ApprovalPolicy.BALANCED
    allowed_tool_groups: list[str] = Field(default_factory=lambda: [
        "datahub.read",
        "datahub.write",
        "datahub.refresh",
        "analysis.run",
        "report.read",
        "report.write",
        "job.read",
        "industry_graph.read",
        "industry_graph.run",
        "web.read",
        "memory",
        "research",
        "skill",
    ])


class ConfirmRequest(BaseModel):
    approved: bool = True
    decision: str | None = None
    arguments: dict[str, Any] | None = None


class PendingAction(BaseModel):
    action_id: str
    tool_name: str
    arguments: dict[str, Any]
    permission: Permission
    risk: RiskLevel = RiskLevel.DANGEROUS
    risk_reason: str = ""
    reason: str
    status: str = "pending"
    session_id: str = "default"
    run_id: str | None = None
    idempotency_key: str | None = None
    created_at: str
    expires_at: str | None = None
    decided_at: str | None = None
    executed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
