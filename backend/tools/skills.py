from __future__ import annotations

from typing import Any

from backend.core.skill_manager import skill_manager


def activate_skill(name: str, session_id: str = "default", run_id: str | None = None) -> dict[str, Any]:
    return skill_manager.activate(name=name, session_id=session_id)
