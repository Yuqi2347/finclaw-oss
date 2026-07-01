from __future__ import annotations

from backend.tools.registry import ToolRegistry


def registry_to_openai_tools(registry: ToolRegistry, *, exclude: set[str] | None = None) -> list[dict]:
    excluded = exclude or set()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.full_description(),
                "parameters": _strict_object_schema(tool.parameters),
            },
        }
        for tool in registry.list_tools()
        if tool.name not in excluded
    ]


def _strict_object_schema(schema: dict) -> dict:
    normalized = dict(schema)
    if normalized.get("type") == "object":
        normalized.setdefault("additionalProperties", False)
    return normalized
