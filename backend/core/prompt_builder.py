from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.services.analysis_jobs import analysis_job_store
from backend.services.approval import approval_store
from backend.services.long_term_memory import long_term_memory_service
from backend.services.sessions import chat_session_store
from backend.tools.reports import report_library
from backend.core.skill_manager import skill_manager
from backend.core.config import DATA_DIR


PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"
MEMORY_DIR = DATA_DIR / "memory"

STATIC_PROMPT_PARTS = [
    "core/identity.md",
    "core/mission.md",
    "levels/level_1.md", 
    "core/behavior.md",
    "core/tool_use.md",
]


class PromptBuilder:
    def build_static_prompt(self) -> str:
        return "\n\n".join(self._load_prompt(path).strip() for path in STATIC_PROMPT_PARTS).strip()

    def build_memory_context(self, user_message: str = "") -> str:
        """构建记忆上下文，注入到系统消息"""
        try:
            long_term_memory_service.rebuild_index()
            index_path = MEMORY_DIR / "index.md"
            playbook_path = MEMORY_DIR / "playbook.md"
            convictions_path = MEMORY_DIR / "convictions.md"
            index_content = ""
            if index_path.exists():
                index_content = index_path.read_text(encoding="utf-8")[:4000]

            # Profile 只注入 current_level + [SNAPSHOT]，不把 [LOG] 带入主对话上下文。
            profile_state = long_term_memory_service.read_profile_context()
            profile_content = str(profile_state.get("content") or "")
            profile_count = 1 if profile_state.get("snapshot") else 0
            profile_updated = str(profile_state.get("last_updated") or "未知")

            # 读取 Convictions：只注入 active/watching 投资判断，不把归档或说明区当作当前信念。
            convictions_content = ""
            convictions_count = 0
            convictions_watching_count = 0
            convictions_updated = "未知"
            if convictions_path.exists():
                full_convictions = convictions_path.read_text(encoding="utf-8")
                convictions_content, convictions_count, convictions_watching_count = self._extract_convictions_prompt_context(full_convictions)
                convictions_updated = datetime.fromtimestamp(convictions_path.stat().st_mtime).isoformat(timespec="seconds")

            # 读取 Playbook（统一研究框架）。不做关键词路由，避免工程化误判用户意图。
            playbook_content = ""
            playbook_dimension_count = 0
            playbook_updated = "未知"
            if playbook_path.exists():
                full_playbook = playbook_path.read_text(encoding="utf-8")
                playbook_content = self._extract_playbook_prompt_context(full_playbook)
                playbook_dimension_count = len(
                    [line for line in playbook_content.split("\n") if re.match(r"^\s*维度\S*[：:]", line)]
                )
                playbook_updated = datetime.fromtimestamp(playbook_path.stat().st_mtime).isoformat(timespec="seconds")

            # 构建加载声明
            loaded_sections = []
            if index_content:
                loaded_sections.append("- **记忆索引**：已加载最新 index.md")
            if profile_content:
                current_level = str(profile_state.get("current_level") or "Level 1")
                loaded_sections.append(f"- **用户画像**：已加载人物志快照，当前 {current_level}，最后更新 {profile_updated}")
            if playbook_content:
                loaded_sections.append(f"- **研究框架**：已加载当前研究架构（{playbook_dimension_count} 个维度），最后更新 {playbook_updated}")
            if convictions_content:
                loaded_sections.append(
                    f"- **当前投资判断**：{convictions_count} 条 active，"
                    f"{convictions_watching_count} 条 watching，最后更新 {convictions_updated}"
                )

            if not loaded_sections:
                return ""

            # 构建完整的记忆上下文
            memory_context = "<memory_context>\n"
            memory_context += "## 本次对话已加载的记忆\n\n"
            memory_context += "\n".join(loaded_sections)
            memory_context += "\n\n---\n\n"
            memory_context += "> 以下是历史记忆，仅供参考。请基于当前对话和新信息独立判断，不要被历史观点束缚。\n\n"

            if index_content:
                memory_context += "## Memory Index（记忆索引）\n\n"
                memory_context += index_content
                memory_context += "\n\n"

            if profile_content:
                memory_context += "## Profile（用户画像）\n\n"
                memory_context += profile_content
                memory_context += "\n\n"

            if playbook_content:
                memory_context += "## Playbook（研究框架）\n\n"
                memory_context += playbook_content
                memory_context += "\n\n"

            if convictions_content:
                memory_context += "## Convictions（当前投资判断）\n\n"
                memory_context += "> 仅以下 active/watching 判断会影响未来分析；使用时必须检查适用范围、失效条件和复核触发。\n\n"
                memory_context += convictions_content
                memory_context += "\n\n"

            memory_context += "> 历史记忆结束。\n"
            memory_context += "</memory_context>"

            return memory_context

        except Exception as e:
            # 记忆加载失败不应阻塞对话
            return f"<memory_context>\n记忆系统加载失败：{str(e)}\n</memory_context>"

    def _extract_convictions_prompt_context(self, content: str) -> tuple[str, int, int]:
        blocks: list[str] = []
        active_count = 0
        watching_count = 0
        pattern = re.compile(
            r"(?ms)(?:<!-- finclaw-memory:.*?-->\s*)?###\s+\[(active|watching)\]\s+.*?(?=\n(?:<!-- finclaw-memory:|###\s+\[)|\Z)"
        )
        for match in pattern.finditer(str(content or "")):
            status = match.group(1).lower()
            if status == "active":
                active_count += 1
            else:
                watching_count += 1
            blocks.append(self._compact_conviction_block(match.group(0)))
        return "\n\n".join(blocks)[:5000], active_count, watching_count

    def _extract_playbook_prompt_context(self, content: str) -> str:
        text = str(content or "")
        match = re.search(r"(?ms)^##\s+当前研究架构\s*$", text)
        if not match:
            cleaned = re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", text).strip()
            return cleaned[:5000]
        next_heading = re.search(r"(?m)^##\s+", text[match.end() :])
        end = match.end() + next_heading.start() if next_heading else len(text)
        section = text[match.start() : end]
        cleaned = re.sub(r"(?ms)<!-- finclaw-memory:.*?-->\s*", "", section).strip()
        return cleaned[:5000]

    def _compact_conviction_block(self, block: str) -> str:
        lines = []
        keep_prefixes = ("### ", "- **判断**", "- **适用范围**", "- **核心依据**", "- **失效条件**", "- **复核触发**", "- **来源**")
        in_evidence = False
        for raw in str(block or "").splitlines():
            line = raw.rstrip()
            if line.startswith("<!--"):
                continue
            if line.startswith("- **核心依据**"):
                in_evidence = True
                lines.append(line)
                continue
            if line.startswith("- **") and not line.startswith("- **核心依据**"):
                in_evidence = False
            if line.startswith(keep_prefixes) or (in_evidence and line.strip().startswith("- ")):
                lines.append(line)
        compact = "\n".join(lines).strip()
        return compact[:1400]

    def build_runtime_facts(self, session_id: str) -> str:
        return (
            "<runtime_facts>\n"
            "以下是当前运行态硬事实。这些信息由系统生成，优先级高于会话记忆和用户的模糊表述。\n"
            "如果这些事实表明没有 pending action / 没有活动任务 / 没有对应报告，你必须按这些事实回答，不能脑补。\n\n"
            f"{json.dumps(self._collect_runtime_facts(session_id), ensure_ascii=False, indent=2, default=str)}\n"
            "</runtime_facts>"
        )

    def build_system_messages(self, session_id: str, user_message: str = "", extra_system: str | None = None) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": self.build_static_prompt()},
            {"role": "system", "content": skill_manager.build_catalog_context(mode="main_agent", session_id=session_id)},
            {"role": "system", "content": self.build_runtime_facts(session_id)},
        ]

        # 注入记忆上下文
        memory_context = self.build_memory_context(user_message)
        if memory_context:
            messages.append({"role": "system", "content": memory_context})

        if extra_system:
            messages.append({"role": "system", "content": extra_system})
        return messages

    def _collect_runtime_facts(self, session_id: str) -> dict[str, Any]:
        queue_state = chat_session_store.get_approval_queue(session_id)
        active_action_id = queue_state.get("active_action_id")
        queued_action_ids = list(queue_state.get("queued_action_ids") or [])
        active_action = approval_store.get(active_action_id) if active_action_id else None
        queued_actions = [approval_store.get(action_id) for action_id in queued_action_ids]
        pending_actions = [item for item in approval_store.list_pending() if item.session_id == session_id]

        jobs = [
            job
            for job in analysis_job_store.query_jobs(limit=20)
            if job.get("session_id") in {session_id, "default"}
        ]
        running_jobs = [job for job in jobs if job.get("status") == "running"]
        reports = report_library.list_report_catalog(limit=8)
        security = chat_session_store.get_security_settings(session_id)

        return {
            "now": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "approval_policy": security.approval_policy.value,
            "active_pending_action": self._serialize_action(active_action),
            "queued_pending_actions": [self._serialize_action(item) for item in queued_actions if item is not None],
            "all_pending_actions_for_session": [self._serialize_action(item) for item in pending_actions],
            "running_jobs_for_session": [self._serialize_job(job) for job in running_jobs[:6]],
            "recent_jobs_for_session": [self._serialize_job(job) for job in jobs[:8]],
            "recent_reports": [self._serialize_report(item) for item in reports[:8]],
        }

    def _serialize_action(self, action: Any | None) -> dict[str, Any] | None:
        if action is None:
            return None
        return {
            "action_id": action.action_id,
            "tool_name": action.tool_name,
            "arguments": action.arguments,
            "permission": action.permission.value,
            "risk": action.risk.value,
            "risk_reason": action.risk_reason,
            "reason": action.reason,
            "status": action.status,
            "created_at": action.created_at,
            "expires_at": action.expires_at,
        }

    def _serialize_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": job.get("job_id"),
            "job_type": job.get("job_type"),
            "status": job.get("status"),
            "current_stage": job.get("current_stage"),
            "args": job.get("args"),
            "output_report_id": job.get("output_report_id"),
            "latest_progress": job.get("latest_progress"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }

    def _serialize_report(self, report: dict[str, Any]) -> dict[str, Any]:
        return {
            "report_id": report.get("report_id"),
            "source": report.get("source"),
            "report_type": report.get("report_type"),
            "subject": report.get("subject"),
            "date": report.get("date"),
            "title": report.get("title"),
            "status": report.get("status"),
            "freshness": report.get("freshness"),
            "preferred_view": report.get("preferred_view"),
            "preferred_read": report.get("preferred_read"),
            "available_formats": report.get("available_formats"),
        }

    @staticmethod
    @lru_cache(maxsize=None)
    def _load_prompt(relative_path: str) -> str:
        return (PROMPTS_ROOT / relative_path).read_text(encoding="utf-8")


prompt_builder = PromptBuilder()
