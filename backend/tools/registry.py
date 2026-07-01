from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.core.models import Permission, RiskLevel


ToolFn = Callable[..., Any]
RiskAssessor = Callable[[dict[str, Any]], tuple[RiskLevel, str] | RiskLevel]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission: Permission
    handler: ToolFn
    parameters: dict[str, Any]
    examples: list[dict[str, Any]] | None = None
    risk_assessor: RiskAssessor | None = None
    group: str = "default"
    layer: str = "atomic"
    side_effects: str = ""
    failure_modes: str = ""
    idempotency: str = ""

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.full_description(),
            "permission": self.permission.value,
            "group": self.group,
            "layer": self.layer,
            "parameters": self.parameters,
            "examples": self.examples or [],
            "side_effects": self.side_effects,
            "failure_modes": self.failure_modes,
            "idempotency": self.idempotency,
        }

    def full_description(self) -> str:
        parts = [
            self.description.strip(),
            f"Tool layer: {self.layer}.",
            "Tool results are the only truth source for this capability.",
        ]
        if self.permission in {Permission.EXPENSIVE_CONFIRM, Permission.WRITE_CONFIRM, Permission.DANGEROUS_WRITE} or self.layer == "workflow":
            parts.extend(
                [
                    "This action may create a confirmation card; do not claim it executed until the tool returns.",
                ]
            )
        return "\n".join(parts)

    def assess_risk(self, arguments: dict[str, Any], default_risk: RiskLevel) -> tuple[RiskLevel, str]:
        if self.risk_assessor is None:
            return default_risk, ""
        assessed = self.risk_assessor(arguments)
        if isinstance(assessed, tuple):
            return assessed
        return assessed, ""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())
