from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.services.capabilities import capability_service


SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    applies_to: tuple[str, ...]
    content: str
    path: Path


class SkillManager:
    """Progressive-disclosure skill loader.

    The catalog is intentionally compact and loaded by default. Full SKILL.md
    content is only returned after an explicit activate_skill call.
    """

    def __init__(self, root: Path = SKILLS_ROOT) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._active: dict[str, set[str]] = {}

    def list_skills(self) -> list[Skill]:
        skills: list[Skill] = []
        if not self.root.exists():
            return skills
        for path in sorted(self.root.glob("*/SKILL.md")):
            skill = self._load_skill(path)
            if skill:
                skills.append(skill)
        return skills

    def get_skill(self, name: str) -> Skill:
        normalized = self._normalize_name(name)
        for skill in self.list_skills():
            if skill.name == normalized:
                return skill
        raise KeyError(f"unknown skill: {name}")

    def activate(self, name: str, session_id: str = "default") -> dict[str, Any]:
        skill = self.get_skill(name)
        key = self._session_key(session_id)
        with self._lock:
            self._active.setdefault(key, set()).add(skill.name)
        return {
            "status": "skill_activated",
            "skill": {
                "name": skill.name,
                "description": skill.description,
                "tools": list(skill.tools),
                "applies_to": list(skill.applies_to),
                "content": skill.content,
            },
            "message": f"Skill {skill.name} 已激活。后续使用相关工具时必须遵循该 Skill。",
        }

    def active_skill_names(self, session_id: str = "default") -> list[str]:
        with self._lock:
            return sorted(self._active.get(self._session_key(session_id), set()))

    def is_tool_skill_active(self, tool_name: str, session_id: str = "default") -> bool:
        required = self.required_skill_for_tool(tool_name)
        if not required:
            return True
        return required in self.active_skill_names(session_id)

    def required_skill_for_tool(self, tool_name: str) -> str | None:
        return REQUIRED_SKILL_TOOLS.get(tool_name)

    def build_catalog_context(
        self,
        *,
        mode: str = "main_agent",
        allowed_tools: set[str] | list[str] | None = None,
        blocked_tools: set[str] | list[str] | None = None,
        session_id: str = "default",
        include_active: bool = True,
    ) -> str:
        allowed = {str(item) for item in allowed_tools or [] if str(item)}
        blocked = {str(item) for item in blocked_tools or [] if str(item)}
        blocked.update(capability_service.disabled_external_tools())
        lines = [
            "<available_skills>",
            "工具使用规范采用 progressive disclosure：这里只列出 Skill catalog。需要某个能力域时，先调用 `activate_skill(name)` 读取完整 SKILL.md，再调用具体工具。",
        ]
        shown = 0
        for skill in self.list_skills():
            if mode not in skill.applies_to and "all" not in skill.applies_to:
                continue
            visible_tools = [tool for tool in skill.tools if tool not in blocked]
            if allowed:
                visible_tools = [tool for tool in visible_tools if tool in allowed or tool == "activate_skill"]
            if not visible_tools:
                continue
            status = "active" if skill.name in self.active_skill_names(session_id) else "available"
            lines.append(
                f"- name: {skill.name}\n"
                f"  status: {status}\n"
                f"  description: {skill.description}\n"
                f"  tools: {', '.join(visible_tools)}"
            )
            shown += 1
        if shown == 0:
            lines.append("- none")
        lines.append("</available_skills>")
        active = self.build_active_context(session_id=session_id, mode=mode) if include_active else ""
        return "\n".join(lines) + (f"\n\n{active}" if active else "")

    def build_active_context(self, *, session_id: str = "default", mode: str = "main_agent") -> str:
        active = set(self.active_skill_names(session_id))
        if not active:
            return ""
        blocks: list[str] = ["<active_skills>"]
        for skill in self.list_skills():
            if skill.name not in active:
                continue
            if mode not in skill.applies_to and "all" not in skill.applies_to:
                continue
            blocks.append(skill.content.strip())
        blocks.append("</active_skills>")
        return "\n\n".join(blocks)

    def _load_skill(self, path: Path) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return None
        meta, body = self._parse_frontmatter(content)
        name = self._normalize_name(str(meta.get("name") or path.parent.name))
        description = str(meta.get("description") or "").strip()
        tools = tuple(str(item).strip() for item in meta.get("tools", []) if str(item).strip())
        applies_to = tuple(str(item).strip() for item in meta.get("applies_to", ["all"]) if str(item).strip()) or ("all",)
        if not description:
            description = self._first_paragraph(body) or name
        return Skill(name=name, description=description, tools=tools, applies_to=applies_to, content=content, path=path)

    def _parse_frontmatter(self, content: str) -> tuple[dict[str, Any], str]:
        if not content.startswith("---"):
            return {}, content
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, flags=re.DOTALL)
        if not match:
            return {}, content
        raw = match.group(1)
        body = match.group(2)
        meta: dict[str, Any] = {}
        current_key = ""
        for raw_line in raw.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if line.startswith("  - ") and current_key:
                meta.setdefault(current_key, []).append(line[4:].strip())
                continue
            if ":" in line and not line.startswith(" "):
                key, value = line.split(":", 1)
                current_key = key.strip()
                value = value.strip()
                if value:
                    meta[current_key] = value.strip('"')
                else:
                    meta[current_key] = []
        return meta, body

    def _first_paragraph(self, text: str) -> str:
        for block in text.split("\n\n"):
            clean = re.sub(r"^#+\s*", "", block.strip())
            if clean:
                return clean[:240]
        return ""

    def _normalize_name(self, name: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-")

    def _session_key(self, session_id: str) -> str:
        return str(session_id or "default")


REQUIRED_SKILL_TOOLS = {
    "get_report_detail": "report-reading",
    "query_report": "report-reading",
    "read_report_section": "report-reading",
    "delete_report": "report-reading",
    "run_stock_research": "background-research-engines",
    "run_market_discovery": "background-research-engines",
    "start_research_thread": "research-thread",
    "read_research_record": "research-record",
    "control_industry_graph": "tradinggraph",
    "record_portfolio_transaction": "portfolio-ledger",
    "memory_write": "memory",
    "memory_update": "memory",
    "memory_archive": "memory",
}


skill_manager = SkillManager()
