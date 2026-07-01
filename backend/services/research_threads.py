from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.core.llm_client import llm_client
from backend.core.skill_manager import skill_manager
from backend.services.capabilities import capability_service
from backend.services.analysis_jobs import analysis_job_store
from backend.tools import analysis
from backend.tools.datahub import datahub_client
from backend.tools.industry_graph import control_industry_graph
from backend.tools.industry_graph import read_industry_graph
from backend.tools.industry_graph import read_industry_graph_node
from backend.tools.reports import report_library
from backend.tools.web_research import web_research


DB_PATH = DATA_DIR / "research_threads.sqlite"
RECORDS_DIR = DATA_DIR / "research_records"
RUNS_DIR = DATA_DIR / "research_runs"
MEMORY_DIR = DATA_DIR / "memory"
PROMPTS_DIR = DATA_DIR.parent / "prompts"
DEEP_RESEARCH_AGENT_PROMPT = PROMPTS_DIR / "core" / "deep_research_agent.md"
DEEP_RESEARCH_VALIDATOR_PROMPT = PROMPTS_DIR / "core" / "deep_research_validator.md"

STATUSES = {"pending", "in_progress", "waiting_approval", "paused", "completed", "failed", "cancelled"}
SUBJECT_TYPES = {"stock", "mainline", "market", "comparison", "unknown"}
DEPTHS = {"quick", "standard", "deep"}
MAX_WEB_BATCHES = 2
ASYNC_RESEARCH_WAIT_SECONDS = 3600
ASYNC_RESEARCH_POLL_SECONDS = 5
INDUSTRY_GRAPH_WAIT_SECONDS = 3600
INDUSTRY_GRAPH_POLL_SECONDS = 5
DEPTH_ITERATIONS = {"quick": 2, "standard": 4, "deep": 7}
TOOL_BUDGET_PROFILES = {
    "quick": {
        "max_iterations": 2,
        "max_tool_calls": 24,
        "max_tool_calls_per_loop": 6,
        "max_high_cost_runs": 1,
        "max_high_cost_runs_per_loop": 0,
        "max_web_batches": 2,
        "max_web_batches_per_loop": 1,
    },
    "standard": {
        "max_iterations": 4,
        "max_tool_calls": 42,
        "max_tool_calls_per_loop": 10,
        "max_high_cost_runs": 4,
        "max_high_cost_runs_per_loop": 1,
        "max_web_batches": 6,
        "max_web_batches_per_loop": 2,
    },
    "deep": {
        "max_iterations": 5,
        "max_tool_calls": 60,
        "max_tool_calls_per_loop": 12,
        "max_high_cost_runs": 5,
        "max_high_cost_runs_per_loop": 1,
        "max_web_batches": 25,
        "max_web_batches_per_loop": 5,
    },
}
DATAHUB_VALID_SECTIONS = {"daily", "events", "financials", "indicators", "news", "position", "profile", "quality"}
LOW_COST_RESEARCH_TOOLS = {
    "search_stock_symbol",
    "get_stock_snapshot",
    "get_stock_data_package",
    "web_research",
    "list_report_catalog",
    "get_report_detail",
    "query_report",
    "read_report_section",
    "read_research_record",
    "read_industry_graph",
    "read_industry_graph_node",
    "get_analysis_jobs",
}
HIGH_COST_RESEARCH_TOOLS = {
    "run_market_discovery",
    "run_stock_research",
    "control_industry_graph",
}
INTERNAL_RESEARCH_TOOLS = {"activate_skill"}
DEFAULT_RESEARCH_TOOLS = sorted(LOW_COST_RESEARCH_TOOLS | HIGH_COST_RESEARCH_TOOLS)
_INTERNAL_RECORD_SECTIONS = {
    "Manifest",
    "读取指南",
    "结论边界",
    "研究过程摘要",
    "工具诊断",
    "工具执行账本",
    "证据账本摘要",
    "验证 Agent 反馈",
    "来源索引",
    "风险备忘",
}


@dataclass
class ResearchBudget:
    web_batches: int = 0
    loop_tool_calls: int = 0
    loop_high_cost_runs: int = 0
    loop_web_batches: int = 0


@dataclass
class ResearchState:
    thread_id: str
    session_id: str
    subject: str
    subject_type: str
    depth: str
    user_goal: str = ""
    ticker: str | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)
    related_research: list[dict[str, Any]] = field(default_factory=list)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    budget: ResearchBudget = field(default_factory=ResearchBudget)
    loop_iterations: int = 0
    allowed_tools: set[str] = field(default_factory=set)
    blocked_tools: set[str] = field(default_factory=set)
    max_iterations: int = 4
    max_tool_calls: int = 16
    max_tool_calls_per_loop: int = 10
    max_high_cost_runs: int = 1
    max_high_cost_runs_per_loop: int = 1
    tool_calls_used: int = 0
    high_cost_runs_used: int = 0
    max_web_batches: int = MAX_WEB_BATCHES
    max_web_batches_per_loop: int = 2
    constraints: str = ""
    last_draft: str = ""
    last_validation: dict[str, Any] = field(default_factory=dict)


class ResearchThreadService:
    """Persistent research task manager.

    This is intentionally a bounded agent loop. The LLM chooses authorized tool
    calls, receives bounded direct tool results, and an isolated validator checks
    whether the current draft satisfies the research goal.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._workers: dict[str, threading.Thread] = {}
        self._init_db()

    def start_thread(
        self,
        subject: str,
        subject_type: str = "unknown",
        depth: str = "standard",
        session_id: str = "default",
        user_goal: str = "",
        auto_start: bool = True,
        force_new: bool = False,
        research_goal: str = "",
        subject_hint: str = "",
        scope_hint: str = "",
        budget_profile: str = "",
        allowed_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
        constraints: str = "",
    ) -> dict[str, Any]:
        research_goal = str(research_goal or user_goal or "").strip()
        subject_hint = str(subject_hint or subject or "").strip()
        scope_hint = str(scope_hint or "").strip()
        subject = str(subject or subject_hint or research_goal).strip()
        if not subject:
            raise ValueError("research subject is required")
        subject_type = subject_type if subject_type in SUBJECT_TYPES else "unknown"
        budget_profile = str(budget_profile or depth or "standard").strip()
        if budget_profile not in DEPTHS:
            budget_profile = "standard"
        depth = depth if depth in DEPTHS else budget_profile
        tool_policy = self._normalize_tool_policy(allowed_tools, blocked_tools)
        if not force_new:
            reusable = self._find_active_thread(subject, subject_type, depth, session_id)
            if reusable:
                if auto_start and reusable.get("status") in {"pending", "paused", "failed"}:
                    self.resume_thread(str(reusable.get("thread_id")))
                    reusable = self.get_thread(str(reusable.get("thread_id")))
                reusable["reused_existing"] = True
                reusable["reuse_reason"] = "active_thread"
                return reusable
            reusable_completed = self._find_recent_completed_thread(subject, subject_type, depth, session_id)
            if reusable_completed:
                reusable_completed["reused_existing"] = True
                reusable_completed["reuse_reason"] = "recent_completed_thread"
                return reusable_completed
        thread_id = f"rt_{uuid4().hex[:12]}"
        now = _now()
        thread = {
            "thread_id": thread_id,
            "session_id": session_id,
            "subject": subject,
            "subject_type": subject_type,
            "depth": depth,
            "status": "pending",
            "user_goal": research_goal or user_goal or subject,
            "plan": self._default_plan(subject, subject_type, depth),
            "evidence": [],
            "gaps": [],
            "recommended_actions": [],
            "related_research": [],
            "validation_results": [],
            "metrics": {
                "step_timings": [],
                "budget": {},
                "research_goal": research_goal or user_goal or subject,
                "subject_hint": subject_hint,
                "scope_hint": scope_hint,
                "budget_profile": budget_profile,
                "allowed_tools": sorted(tool_policy["allowed_tools"]),
                "blocked_tools": sorted(tool_policy["blocked_tools"]),
                "constraints": str(constraints or ""),
                "mode": "deep_research_agent",
            },
            "current_conclusion": "研究线程已创建，等待执行。",
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        with self._lock, self._connect() as conn:
            self._upsert(conn, thread)
        self._ensure_loop_workspace(thread)
        if auto_start:
            self.resume_thread(thread_id)
        return self.get_thread(thread_id)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from research_threads where thread_id=?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown research thread: {thread_id}")
        return self._row_to_thread(row)

    def list_threads(
        self,
        session_id: str | None = None,
        status: str | None = None,
        subject: str | None = None,
        limit: int = 20,
        detail: str = "summary",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            args.append(session_id)
        if status:
            if status == "active":
                clauses.append("status in ('pending', 'in_progress', 'paused', 'waiting_approval', 'failed')")
            else:
                clauses.append("status=?")
                args.append(status)
        if subject:
            clauses.append("(subject like ? or current_conclusion like ?)")
            args.extend([f"%{subject}%", f"%{subject}%"])
        where = f"where {' and '.join(clauses)}" if clauses else ""
        sql = f"select * from research_threads {where} order by updated_at desc limit ?"
        args.append(max(1, min(int(limit or 20), 100)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        threads = [self._row_to_thread(row) for row in rows]
        threads = [self.compact_thread(thread) for thread in threads]
        return {"success": True, "threads": threads, "detail": "summary"}

    def compact_thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        compact = dict(thread)
        compact["record_id"] = self.record_id_for_thread(thread)
        compact["plan"] = [
            {
                "step_id": step.get("step_id"),
                "question": step.get("question"),
                "tool_type": step.get("tool_type"),
                "status": step.get("status"),
                "conclusion": str(step.get("conclusion") or "")[:240],
            }
            for step in (thread.get("plan") or [])[:12]
            if isinstance(step, dict)
        ]
        metrics = thread.get("metrics") if isinstance(thread.get("metrics"), dict) else {}
        compact["rounds"] = self._compact_rounds(thread)
        compact["validator"] = metrics.get("last_validation") if isinstance(metrics.get("last_validation"), dict) else {}
        compact["truncated"] = {
            "round_total": len(compact.get("rounds") or []),
        }
        return compact

    def _compact_rounds(self, thread: dict[str, Any]) -> list[dict[str, Any]]:
        metrics = thread.get("metrics") if isinstance(thread.get("metrics"), dict) else {}
        runs = metrics.get("agent_tool_runs") if isinstance(metrics.get("agent_tool_runs"), list) else []
        validations = metrics.get("validator_history") if isinstance(metrics.get("validator_history"), list) else []
        by_round: dict[int, dict[str, Any]] = {}
        for step in thread.get("plan") or []:
            if not isinstance(step, dict):
                continue
            match = re.search(r"agent_loop_(\d+)", str(step.get("step_id") or ""))
            if not match:
                continue
            round_no = int(match.group(1))
            item = by_round.setdefault(round_no, {"round": round_no, "tools": []})
            item["focus"] = str(step.get("question") or f"第 {round_no} 轮研究")[:180]
            item["status"] = step.get("status") or "pending"
            item["summary"] = str(step.get("conclusion") or "")[:260]
            item["updated_at"] = step.get("updated_at")
        for run in runs:
            if not isinstance(run, dict):
                continue
            try:
                round_no = int(run.get("round") or 0)
            except Exception:
                round_no = 0
            if round_no <= 0:
                continue
            item = by_round.setdefault(round_no, {"round": round_no, "tools": []})
            tools = item.setdefault("tools", [])
            if isinstance(tools, list):
                tools.append({
                    "tool": run.get("tool"),
                    "status": run.get("status"),
                    "elapsed_ms": run.get("elapsed_ms"),
                    "summary": str(run.get("summary") or "")[:160],
                    "finished_at": run.get("finished_at"),
                })
        for index, validation in enumerate(validations, start=1):
            if not isinstance(validation, dict):
                continue
            item = by_round.setdefault(index, {"round": index, "tools": []})
            item["validator_status"] = validation.get("status")
            item["validator_confidence"] = validation.get("confidence")
        return [by_round[key] for key in sorted(by_round.keys())][-10:]

    def record_id_for_thread(self, thread: dict[str, Any]) -> str:
        folder = self._record_folder(str(thread.get("subject_type") or "unknown"))
        slug = _safe_slug(str(thread.get("subject") or thread.get("thread_id") or "unknown"))
        return f"{folder}/{slug}"

    def control_thread(self, thread_id: str, action: str) -> dict[str, Any]:
        action = str(action or "").strip()
        thread = self.get_thread(thread_id)
        if action == "pause":
            if thread["status"] in {"completed", "failed", "cancelled"}:
                return {"success": True, "thread": thread, "message": "线程已结束，无法暂停。"}
            self._patch_thread(thread_id, status="paused", current_conclusion="研究已暂停。")
        elif action == "resume":
            if thread["status"] in {"completed", "cancelled"}:
                return {"success": True, "thread": thread, "message": "线程已结束，无法恢复。"}
            if thread["status"] in {"failed", "paused"}:
                self._reset_failed_steps(thread_id)
            self.resume_thread(thread_id)
        elif action == "cancel":
            self._patch_thread(thread_id, status="cancelled", current_conclusion="研究已取消。", completed_at=_now())
        else:
            raise ValueError("action must be pause, resume, or cancel")
        return {"success": True, "thread": self.get_thread(thread_id)}

    def list_records(self, subject_type: str | None = None, limit: int = 50, query: str | None = None) -> dict[str, Any]:
        RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        folder = self._record_folder(str(subject_type or "")) if subject_type else ""
        folders = [folder] if folder else ["stocks", "mainlines", "comparisons", "unknown"]
        records: list[dict[str, Any]] = []
        query_tokens = _query_match_tokens(query or "")
        for folder in folders:
            root = RECORDS_DIR / str(folder)
            if not root.exists():
                continue
            for path in root.glob("*.md"):
                content = path.read_text(encoding="utf-8")
                summary = self._record_summary(path, content)
                if query_tokens:
                    haystack = " ".join(
                        str(summary.get(key) or "")
                        for key in ("record_id", "title", "user_goal", "core_conclusion", "validator_status", "quality_level")
                    ).lower()
                    normalized_haystack = _normalize_subject_key(haystack)
                    if not any(token in normalized_haystack for token in query_tokens):
                        continue
                summary["content_preview"] = content[:1200]
                records.append(summary)
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        if query:
            for item in records:
                item["match_reason"] = "未做旁路语义筛选；后台研究 Agent 应根据目录和章节自行判断是否复用。"
        for item in records:
            item.pop("content_preview", None)
        return {"success": True, "records": records[: max(1, min(int(limit or 50), 200))]}

    def get_record(self, record_id: str, section: str | None = None, max_chars: int = 12000, offset: int = 0) -> dict[str, Any]:
        path = self._record_path_from_id(record_id)
        if not path.exists():
            candidates = self.list_records(query=record_id, limit=12)
            return {
                "success": False,
                "status": "record_not_found",
                "error": f"research record not found: {record_id}",
                "query_candidates": candidates.get("records") or [],
                "message": "record_id 未命中；已按该值搜索候选。请使用候选中的真实 record_id 继续读取；如果没有候选，再改用报告读取工具。",
            }
        full_content = path.read_text(encoding="utf-8")
        content = full_content
        if section:
            section = str(section or "").strip()
            if section == "summary":
                content = self._record_summary_view(full_content)
            elif section == "body":
                content = self._record_body_view(full_content)
            elif section in _INTERNAL_RECORD_SECTIONS:
                content = self._record_default_view(full_content)
            else:
                content = _extract_markdown_section(content, section)
                if not content.strip() and section == "研究摘要":
                    content = _extract_markdown_section(full_content, "结论摘要")
                if not content.strip() and section == "待验证判断":
                    content = _extract_markdown_section(full_content, "待验证问题") or _extract_markdown_section(full_content, "未解决问题")
        else:
            content = self._record_default_view(full_content)
        window = _page_text(content, max_chars=max_chars, offset=offset)
        return {"success": True, "record": self._record_summary(path, full_content), "read_window": window}

    def resume_thread(self, thread_id: str) -> None:
        with self._lock:
            if thread_id in self._workers and self._workers[thread_id].is_alive():
                return
            worker = threading.Thread(target=self._run_thread_safe, args=(thread_id,), daemon=True)
            self._workers[thread_id] = worker
            worker.start()

    def _run_thread_safe(self, thread_id: str) -> None:
        try:
            self._run_thread(thread_id)
        except Exception as exc:
            text = str(exc)
            if "research thread paused" in text:
                self._patch_thread(thread_id, status="paused", error=None, current_conclusion="研究已暂停，可稍后恢复。")
                return
            if "research thread cancelled" in text:
                self._patch_thread(thread_id, status="cancelled", error=None, current_conclusion="研究已取消。", completed_at=_now())
                return
            self._patch_thread(thread_id, status="failed", error=str(exc), current_conclusion=f"研究失败：{exc}")

    def _run_thread(self, thread_id: str) -> None:
        thread = self.get_thread(thread_id)
        if thread["status"] in {"completed", "cancelled"}:
            return
        subject = str(thread.get("subject") or "")
        subject_type = str(thread.get("subject_type") or "unknown")
        depth = str(thread.get("depth") or "standard")
        state = ResearchState(
            thread_id=thread_id,
            session_id=str(thread.get("session_id") or "default"),
            subject=subject,
            subject_type=subject_type,
            depth=depth,
            user_goal=str(thread.get("user_goal") or ""),
            plan=list(thread.get("plan") or []),
            gaps=list(thread.get("gaps") or []),
            recommended_actions=list(thread.get("recommended_actions") or []),
            related_research=list(thread.get("related_research") or []),
            validation_results=list(thread.get("validation_results") or []),
            metrics=dict(thread.get("metrics") or {}),
        )
        self._hydrate_agent_state_from_metrics(state)

        self._patch_thread(thread_id, status="in_progress", current_conclusion="Deep Research Agent 正在启动。")
        state.metrics.setdefault("started_at", _now())
        self._ensure_loop_workspace(self.get_thread(thread_id), state=state)
        self._run_deep_research_agent(state)
        conclusion = self._read_current_draft(state).strip() or f"# {state.subject}\n\n## 研究摘要\n暂无。\n\n## 待验证判断\n暂无。\n"
        final_status = "completed" if conclusion.strip() else "failed"
        if final_status == "completed":
            record_id = f"{self._record_folder(subject_type)}/{_safe_slug(subject)}"
            self._append_loop_event(
                state,
                "Deep Research Completed",
                f"- status: completed\n- record_id: {record_id}\n- completed_at: {_now()}\n- conclusion: {_first_meaningful_line(conclusion)[:240]}",
            )
        self._patch_thread(
            thread_id,
            subject_type=state.subject_type,
            status=final_status,
            plan=state.plan,
            evidence=[],
            gaps=_dedupe_strings(state.gaps),
            recommended_actions=_dedupe_actions(state.recommended_actions),
            related_research=state.related_research,
            validation_results=state.validation_results,
            metrics=self._finalize_metrics(state),
            current_conclusion=conclusion,
            completed_at=_now(),
        )
        if final_status == "completed":
            self._write_record(self.get_thread(thread_id))

    def _default_plan(self, subject: str, subject_type: str, depth: str) -> list[dict[str, Any]]:
        return [
            {"step_id": "deep_research_loop", "question": "研究 Agent 自主调用授权工具；独立验证 Agent 审查是否达标", "status": "pending", "conclusion": ""},
        ]

    def _hydrate_agent_state_from_metrics(self, state: ResearchState) -> None:
        metrics = state.metrics if isinstance(state.metrics, dict) else {}
        profile = str(metrics.get("budget_profile") or state.depth or "standard")
        if profile not in TOOL_BUDGET_PROFILES:
            profile = "standard"
        budget = dict(TOOL_BUDGET_PROFILES[profile])
        state.max_iterations = int(metrics.get("max_iterations") or budget["max_iterations"])
        state.max_tool_calls = int(metrics.get("max_tool_calls") or budget["max_tool_calls"])
        state.max_tool_calls_per_loop = int(metrics.get("max_tool_calls_per_loop") or budget["max_tool_calls_per_loop"])
        state.max_high_cost_runs = int(metrics.get("max_high_cost_runs") or budget["max_high_cost_runs"])
        state.max_high_cost_runs_per_loop = int(metrics.get("max_high_cost_runs_per_loop") or budget["max_high_cost_runs_per_loop"])
        state.max_web_batches = int(metrics.get("max_web_batches") or budget["max_web_batches"])
        state.max_web_batches_per_loop = int(metrics.get("max_web_batches_per_loop") or budget["max_web_batches_per_loop"])
        state.constraints = str(metrics.get("constraints") or "")
        policy = self._normalize_tool_policy(metrics.get("allowed_tools"), metrics.get("blocked_tools"))
        state.allowed_tools = set(policy["allowed_tools"])
        state.blocked_tools = set(policy["blocked_tools"])
        state.tool_calls_used = int(metrics.get("tool_calls_used") or 0)
        state.high_cost_runs_used = int(metrics.get("high_cost_runs_used") or 0)
        if not isinstance(state.metrics.get("tool_failures"), dict):
            state.metrics["tool_failures"] = {}
        if not isinstance(state.metrics.get("tool_usage_counts"), dict):
            state.metrics["tool_usage_counts"] = {}
        if state.subject_type == "stock" and not state.ticker:
            state.ticker = self._resolve_ticker(state.subject)

    def _reset_loop_budget(self, state: ResearchState) -> None:
        state.budget.loop_tool_calls = 0
        state.budget.loop_high_cost_runs = 0
        state.budget.loop_web_batches = 0

    def _normalize_tool_policy(self, allowed_tools: Any, blocked_tools: Any) -> dict[str, set[str]]:
        disabled_external = capability_service.disabled_external_tools()
        default_allowed = set(DEFAULT_RESEARCH_TOOLS) - disabled_external
        allowed = {str(item).strip() for item in (allowed_tools or default_allowed) if str(item).strip()}
        blocked = {str(item).strip() for item in (blocked_tools or []) if str(item).strip()}
        blocked.update(disabled_external)
        allowed = {item for item in allowed if item in DEFAULT_RESEARCH_TOOLS}
        blocked = {item for item in blocked if item in DEFAULT_RESEARCH_TOOLS}
        allowed.update(LOW_COST_RESEARCH_TOOLS)
        if not allowed:
            allowed = set(default_allowed)
        allowed -= blocked
        return {"allowed_tools": allowed, "blocked_tools": blocked}

    def _run_deep_research_agent(self, state: ResearchState) -> None:
        self._mark_step(state.plan, "deep_research_loop", "in_progress")
        playbook = self._read_playbook_context()
        state.metrics["playbook_status"] = "loaded" if playbook.strip() else "missing_or_empty"
        if not playbook.strip():
            state.gaps.append("playbook.md 为空或不存在，本轮无法基于用户研究框架约束研究路径。")
        ticker = self._resolve_ticker(state.subject) if state.subject_type in {"stock", "unknown"} else None
        if ticker:
            state.ticker = ticker
            state.subject_type = "stock"
            self._record_agent_tool_run(
                state,
                "search_stock_symbol",
                {"query": state.subject},
                "ok",
                f"研究对象已解析为标准 A 股代码 {ticker}。",
                0,
            )
        self._append_loop_event(state, "Loop 启动", f"playbook_status={state.metrics.get('playbook_status')} ticker={state.ticker or ''}")
        if not self._read_current_draft(state).strip():
            self._write_current_draft(state, f"# {state.subject}\n\n## 研究摘要\n暂无。\n\n## 待验证判断\n暂无。\n")
        if self._strategy_needs_initialization(state):
            strategy = self._initialize_research_strategy(state, playbook)
            self._write_research_strategy(state, strategy)
            self._append_loop_event(state, "Research Strategy Initialized", strategy[:2400])

        for iteration in range(1, state.max_iterations + 1):
            self._raise_if_stopped(state.thread_id)
            if state.tool_calls_used >= state.max_tool_calls:
                self._append_system_event(state, "达到全局工具调用硬上限，停止继续研究。")
                break
            self._reset_loop_budget(state)
            state.loop_iterations = iteration
            step = {
                "step_id": f"agent_loop_{iteration}",
                "question": f"第 {iteration} 轮研究稿完善",
                "tool_type": "agent",
                "actions": [],
                "status": "in_progress",
                "conclusion": "",
                "updated_at": _now(),
            }
            state.plan.append(step)
            started = time.perf_counter()
            final_status = "done"
            self._save_state(state, f"Deep Research Agent 第 {iteration}/{state.max_iterations} 轮：研究 Agent 正在完善研究稿。")
            decision = self._run_research_inner_loop(state, iteration, step)
            state.last_draft = self._read_current_draft(state)
            self._apply_agent_strategy_update(state, decision, iteration)
            step["status"] = "done"
            step["question"] = str(decision.get("focus") if isinstance(decision, dict) else "" or step["question"])[:180]
            step["conclusion"] = str(decision.get("focus") or "本轮研究稿已提交审稿。")[:1200] if isinstance(decision, dict) else "本轮研究稿已提交审稿。"
            step["updated_at"] = _now()
            self._record_step_timing(state, str(step["step_id"]), started, final_status, tool_type="agent")
            self._write_round_draft(state, iteration, decision, step)
            self._append_loop_round(state, iteration, decision, step)
            validation = self._deep_research_validate(state, iteration)
            state.last_validation = validation
            self._apply_validator_strategy_patch(state, validation, iteration)
            self._write_round_validation(state, iteration, validation)
            self._append_loop_validation(state, iteration, validation)
            self._save_state(state, f"Deep Research Agent 第 {iteration} 轮完成：验证={validation.get('status') or 'unknown'}；{step['conclusion'][:140]}")
            if self._should_stop_research_loop(state, decision, validation, iteration):
                break

        self._mark_step(state.plan, "deep_research_loop", "done", f"完成 {state.loop_iterations} 轮研究-验证循环。")

    def _run_research_inner_loop(self, state: ResearchState, iteration: int, step: dict[str, Any]) -> dict[str, Any]:
        last_tool_results: list[dict[str, Any]] = []
        last_decision: dict[str, Any] = {}
        while True:
            self._raise_if_stopped(state.thread_id)
            decision = self._deep_research_decide(state, iteration, current_tool_results=last_tool_results)
            last_decision = decision if isinstance(decision, dict) else {}
            draft = self._normalize_round_draft(last_decision, iteration)
            if draft and "未获得有效研究草稿" not in draft:
                self._write_current_draft(state, draft)
                state.last_draft = draft
            actions = self._normalize_agent_actions(last_decision.get("actions") if isinstance(last_decision, dict) else None)
            submit = bool(last_decision.get("submit_draft") or last_decision.get("should_stop"))
            if submit:
                return last_decision
            if not actions:
                return last_decision
            if self._loop_tool_budget_exhausted(state):
                self._append_system_event(state, "本轮工具预算已耗尽，提交当前研究稿给 Validator。")
                return last_decision
            round_results: list[dict[str, Any]] = []
            for action in self._order_agent_actions_for_execution(actions):
                self._raise_if_stopped(state.thread_id)
                if self._loop_tool_budget_exhausted(state):
                    break
                result = self._execute_agent_action(state, action)
                step_actions = step.get("actions")
                if isinstance(step_actions, list):
                    step_actions.append(result)
                round_results.append(self._tool_result_for_agent(result))
            last_tool_results = round_results
            if not round_results:
                return last_decision

    def _loop_tool_budget_exhausted(self, state: ResearchState) -> bool:
        return (
            state.tool_calls_used >= state.max_tool_calls
            or state.budget.loop_tool_calls >= state.max_tool_calls_per_loop
            or state.budget.web_batches >= state.max_web_batches
        )

    def _order_agent_actions_for_execution(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        low_cost = [item for item in actions if str(item.get("tool") or "") not in HIGH_COST_RESEARCH_TOOLS]
        high_cost = [item for item in actions if str(item.get("tool") or "") in HIGH_COST_RESEARCH_TOOLS]
        return low_cost + high_cost

    def _deep_research_decide(self, state: ResearchState, iteration: int, current_tool_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not llm_client.configured:
            return {}
        prompt = DEEP_RESEARCH_AGENT_PROMPT.read_text(encoding="utf-8") if DEEP_RESEARCH_AGENT_PROMPT.exists() else _default_deep_research_agent_prompt()
        payload = self._agent_payload(state, iteration, current_tool_results=current_tool_results or [])
        try:
            parsed = llm_client.chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
                session_id=state.session_id,
                purpose="deep_research_agent",
            )
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            state.gaps.append(f"Deep Research Agent 决策 LLM 不可用，使用保守 fallback：{exc}")
            return {}

    def _deep_research_validate(self, state: ResearchState, iteration: int) -> dict[str, Any]:
        fallback = {
            "status": "fail",
            "confidence": "low",
            "reason": "validator unavailable",
            "missing_analysis": ["验证 Agent 未能运行，本轮不能判定通过。"],
            "overclaims": [],
            "strategy_patch": {
                "reviewer_guidance": "继续完善研究草稿的论证链、反证和边界说明，并在下一轮提交更明确的阶段性结论。"
            },
        }
        if not llm_client.configured:
            return fallback
        prompt = DEEP_RESEARCH_VALIDATOR_PROMPT.read_text(encoding="utf-8") if DEEP_RESEARCH_VALIDATOR_PROMPT.exists() else _default_deep_research_validator_prompt()
        payload = {
            "research_goal": state.user_goal or state.subject,
            "subject": state.subject,
            "subject_type": state.subject_type,
            "ticker": state.ticker,
            "depth": state.depth,
            "iteration": iteration,
            "research_strategy": self._read_research_strategy(state)[:12000],
            "round_draft": self._read_current_draft(state)[:12000],
        }
        try:
            parsed = llm_client.chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
                session_id=state.session_id,
                purpose="deep_research_validator",
            )
        except Exception as exc:
            fallback["reason"] = f"validator failed: {exc}"
            state.gaps.append(f"验证 Agent 调用失败：{exc}")
            return fallback
        if not isinstance(parsed, dict):
            return fallback
        status = str(parsed.get("status") or "fail").lower()
        if status not in {"pass", "fail"}:
            status = "fail"
        validation = {
            "status": status,
            "confidence": str(parsed.get("confidence") or "low").lower(),
            "reason": str(parsed.get("reason") or "")[:1200],
            "analysis_quality": str(parsed.get("analysis_quality") or "").lower()[:80],
            "playbook_alignment": str(parsed.get("playbook_alignment") or "")[:1200],
            "missing_analysis": [str(item)[:400] for item in (parsed.get("missing_analysis") or []) if isinstance(item, str)][:12],
            "overclaims": [str(item)[:400] for item in parsed.get("overclaims") or [] if isinstance(item, str)][:12],
            "strategy_patch": parsed.get("strategy_patch") if isinstance(parsed.get("strategy_patch"), dict) else {
                "reviewer_guidance": str(parsed.get("next_feedback") or "")[:1600],
            },
            "checked_at": _now(),
        }
        history = state.metrics.setdefault("validator_history", [])
        if isinstance(history, list):
            history.append(validation)
        return validation

    def _agent_payload(self, state: ResearchState, iteration: int, current_tool_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "research_goal": state.user_goal or state.subject,
            "subject": state.subject,
            "subject_type": state.subject_type,
            "ticker": state.ticker,
            "depth": state.depth,
            "constraints": state.constraints,
            "tool_policy": {
                "allowed_tools": sorted(state.allowed_tools),
                "limits": {
                    "max_iterations": state.max_iterations,
                    "max_tool_calls": state.max_tool_calls,
                    "max_tool_calls_per_loop": state.max_tool_calls_per_loop,
                    "max_high_cost_runs": state.max_high_cost_runs,
                    "max_high_cost_runs_per_loop": state.max_high_cost_runs_per_loop,
                    "max_web_batches": state.max_web_batches,
                    "max_web_batches_per_loop": state.max_web_batches_per_loop,
                },
                "usage": {
                    "iteration": iteration,
                    "tool_calls_used": state.tool_calls_used,
                    "loop_tool_calls": state.budget.loop_tool_calls,
                    "high_cost_runs_used": state.high_cost_runs_used,
                    "loop_high_cost_runs": state.budget.loop_high_cost_runs,
                    "web_batches": state.budget.web_batches,
                    "loop_web_batches": state.budget.loop_web_batches,
                },
            },
            "tool_usage_counts": state.metrics.get("tool_usage_counts") or {},
            "tool_failures": state.metrics.get("tool_failures") or {},
            "async_jobs": state.metrics.get("async_jobs") or [],
            "playbook_status": state.metrics.get("playbook_status"),
            "research_strategy": self._read_research_strategy(state)[:12000],
            "current_draft": self._read_current_draft(state)[:12000],
            "current_tool_results": current_tool_results or [],
            "available_skills": skill_manager.build_catalog_context(
                mode="deep_research",
                allowed_tools=state.allowed_tools | INTERNAL_RESEARCH_TOOLS,
                session_id=f"research:{state.thread_id}",
                include_active=False,
            ),
            "active_skills": skill_manager.build_active_context(
                mode="deep_research",
                session_id=f"research:{state.thread_id}",
            ),
        }

    def _normalize_agent_actions(self, actions: Any) -> list[dict[str, Any]]:
        if not isinstance(actions, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in actions[:5]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "").strip()
            if tool not in DEFAULT_RESEARCH_TOOLS and tool not in INTERNAL_RESEARCH_TOOLS:
                continue
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            args = self._normalize_agent_tool_args(tool, dict(args))
            normalized.append({
                "tool": tool,
                "arguments": args,
                "reason": str(item.get("reason") or "")[:300],
            })
        return normalized

    def _normalize_agent_tool_args(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "get_stock_data_package":
            mode = str(args.get("mode") or "overview")
            if mode == "section":
                section = str(args.get("section") or "").strip()
                if section in DATAHUB_VALID_SECTIONS:
                    args["section"] = section
                else:
                    args.pop("section", None)
                    mode = "overview"
            args["mode"] = mode
        if tool == "web_research" and args.get("total_source_budget") is None:
            args["total_source_budget"] = 6
        return args

    def _fallback_agent_actions(self, state: ResearchState, iteration: int) -> list[dict[str, Any]]:
        return []

    def _normalize_round_draft(self, decision: dict[str, Any], iteration: int) -> str:
        if not isinstance(decision, dict):
            return f"第 {iteration} 轮未获得有效研究草稿。"
        draft = str(decision.get("draft") or decision.get("current_answer") or decision.get("working_conclusion") or "").strip()
        if draft:
            return draft[:9000]
        parts = [
            f"第 {iteration} 轮阶段草稿",
            f"Focus: {decision.get('focus') or ''}",
            "Analysis delta:",
            "\n".join(
                f"- {item.get('material')}: {item.get('interpretation')}"
                for item in (decision.get("analysis_delta") or [])
                if isinstance(item, dict)
            ) or "- 暂无",
            "Actions:",
            "\n".join(
                f"- {item.get('tool')}: {item.get('reason')}"
                for item in (decision.get("actions") or [])
                if isinstance(item, dict)
            ) or "- 暂无",
        ]
        return "\n".join(parts)[:9000]

    def _should_stop_research_loop(self, state: ResearchState, decision: dict[str, Any], validation: dict[str, Any], iteration: int) -> bool:
        if isinstance(validation, dict) and validation.get("status") == "pass":
            return True
        if iteration >= state.max_iterations:
            self._append_system_event(state, "达到最大研究-审稿轮次，停止继续研究。")
            return True
        if state.tool_calls_used >= state.max_tool_calls:
            self._append_system_event(state, "达到全局工具调用硬上限，停止继续研究。")
            return True
        return False

    def _execute_agent_action(self, state: ResearchState, action: dict[str, Any]) -> dict[str, Any]:
        tool = str(action.get("tool") or "")
        args = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
        skip_reason = self._preflight_agent_action(state, tool, args)
        if skip_reason:
            state.gaps.append(skip_reason)
            self._record_agent_tool_run(state, tool, args, "skipped", skip_reason, 0)
            return {"tool": tool, "status": "skipped", "reason": action.get("reason"), "summary": skip_reason, "elapsed_ms": 0}
        if tool not in INTERNAL_RESEARCH_TOOLS and (tool not in state.allowed_tools or tool in state.blocked_tools):
            message = f"{tool} 未在本线程授权工具集合内，已跳过。"
            state.gaps.append(message)
            return {"tool": tool, "status": "blocked", "summary": message}
        if tool in HIGH_COST_RESEARCH_TOOLS and state.high_cost_runs_used >= state.max_high_cost_runs:
            message = f"{tool} 达到高成本工具预算，已跳过。"
            state.gaps.append(message)
            return {"tool": tool, "status": "budget_exceeded", "summary": message}
        if state.budget.loop_tool_calls >= state.max_tool_calls_per_loop:
            message = f"{tool} 达到本轮工具预算，已跳过。"
            self._append_system_event(state, message)
            return {"tool": tool, "status": "budget_exceeded", "summary": message}
        if tool in HIGH_COST_RESEARCH_TOOLS and state.budget.loop_high_cost_runs >= state.max_high_cost_runs_per_loop:
            message = f"{tool} 达到本轮高成本工具预算，已跳过。"
            self._append_system_event(state, message)
            return {"tool": tool, "status": "budget_exceeded", "summary": message}
        started = time.perf_counter()
        if tool not in INTERNAL_RESEARCH_TOOLS:
            state.tool_calls_used += 1
            state.budget.loop_tool_calls += 1
        self._mark_tool_usage(state, tool, args)
        if tool in HIGH_COST_RESEARCH_TOOLS:
            state.high_cost_runs_used += 1
            state.budget.loop_high_cost_runs += 1
        if tool == "web_research":
            state.budget.web_batches += 1
            state.budget.loop_web_batches += 1
        try:
            result = self._call_research_tool(state, tool, args)
            status = "ok"
            summary = self._summarize_tool_result(tool, result)
            self._update_tool_signal_state(state, tool, args, result, status, summary)
        except Exception as exc:
            result = {"error": str(exc)}
            status = "error"
            summary = f"{tool} 调用失败：{exc}"
            state.gaps.append(summary)
            self._update_tool_signal_state(state, tool, args, result, status, summary)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._record_agent_tool_run(state, tool, args, status, summary, elapsed_ms)
        return {
            "tool": tool,
            "status": status,
            "reason": action.get("reason"),
            "summary": summary,
            "elapsed_ms": elapsed_ms,
            "result": result,
        }

    def _tool_result_for_agent(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Return bounded current-loop tool output for the next LLM call only."""
        item = {
            "tool": action_result.get("tool"),
            "status": action_result.get("status"),
            "reason": action_result.get("reason"),
            "summary": action_result.get("summary"),
            "elapsed_ms": action_result.get("elapsed_ms"),
        }
        if "result" in action_result:
            item["result"] = _bound_current_tool_result(action_result.get("result"))
        return item

    def _call_research_tool(self, state: ResearchState, tool: str, args: dict[str, Any]) -> Any:
        if tool == "activate_skill":
            name = str(args.get("name") or args.get("skill") or "").strip()
            if not name:
                raise ValueError("skill name is required")
            return skill_manager.activate(name=name, session_id=f"research:{state.thread_id}")
        if tool == "search_stock_symbol":
            return datahub_client.search_stock_symbol(str(args.get("query") or state.subject), limit=int(args.get("limit") or 8))
        if tool == "get_stock_snapshot":
            ticker = str(args.get("ticker") or state.ticker or "").strip()
            if not ticker:
                raise ValueError("ticker is required; call search_stock_symbol first if the stock code is unknown")
            return datahub_client.get_stock_snapshot(ticker, timeout=12)
        if tool == "get_stock_data_package":
            ticker = str(args.get("ticker") or state.ticker or "").strip()
            if not ticker:
                raise ValueError("ticker is required; call search_stock_symbol first if the stock code is unknown")
            return datahub_client.get_stock_data_package(
                ticker=ticker,
                mode=str(args.get("mode") or "overview"),
                section=args.get("section"),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 0),
                max_chars=int(args.get("max_chars") or 7000),
                ensure=bool(args.get("ensure", True)),
            )
        if tool == "web_research":
            return web_research(
                query=args.get("query"),
                queries=args.get("queries"),
                intent=str(args.get("intent") or "verify_claim"),
                recency=str(args.get("recency") or "month"),
                max_sources=args.get("max_sources"),
                max_sources_per_query=args.get("max_sources_per_query"),
                total_source_budget=args.get("total_source_budget") or 6,
                source_policy=str(args.get("source_policy") or "finance_first"),
            )
        if tool == "list_report_catalog":
            return report_library.list_report_catalog(
                report_type=args.get("report_type"),
                subject=args.get("subject") or state.subject,
                limit=int(args.get("limit") or 12),
            )
        if tool == "get_report_detail":
            return report_library.get_report_detail(str(args.get("report_id") or ""), max_chars=int(args.get("max_chars") or 6000), offset=int(args.get("offset") or 0))
        if tool == "query_report":
            report_id = str(args.get("report_id") or "")
            if not _looks_like_report_id(report_id):
                return {
                    "status": "invalid_report_id",
                    "tool_name": tool,
                    "report_id": report_id,
                    "message": "report_id 必须来自 list_report_catalog 返回值；请先调用 list_report_catalog。",
                }
            return report_library.query_report(
                report_id=report_id,
                question=str(args.get("question") or state.user_goal or state.subject),
                max_sections=int(args.get("max_sections") or 3),
                per_section_chars=int(args.get("per_section_chars") or 1600),
                total_chars=int(args.get("total_chars") or 5200),
            )
        if tool == "read_report_section":
            report_id = str(args.get("report_id") or "")
            section_id = str(args.get("section_id") or "")
            if not _looks_like_report_id(report_id):
                return {
                    "status": "invalid_report_id",
                    "tool_name": tool,
                    "report_id": report_id,
                    "message": "report_id 必须来自 list_report_catalog 返回值；请先调用 list_report_catalog。",
                }
            if not section_id.strip():
                return {
                    "status": "missing_section_id",
                    "tool_name": tool,
                    "message": "read_report_section 需要 section_id；请先调用 get_report_detail。",
                }
            return report_library.read_report_section(
                report_id=report_id,
                section_id=section_id,
                offset=int(args.get("offset") or 0),
                max_chars=int(args.get("max_chars") or 5000),
            )
        if tool == "read_research_record":
            if args.get("record_id"):
                return self.get_record(
                    record_id=str(args.get("record_id") or ""),
                    section=args.get("section"),
                    offset=int(args.get("offset") or 0),
                    max_chars=int(args.get("max_chars") or 6000),
                )
            return self.list_records(subject_type=args.get("subject_type"), query=str(args.get("query") or state.subject), limit=int(args.get("limit") or 12))
        if tool == "read_industry_graph":
            return read_industry_graph(
                action=str(args.get("action") or "list_mainlines"),
                mainline=str(args.get("mainline") or ""),
                run_id=str(args.get("run_id") or ""),
                node_id=str(args.get("node_id") or ""),
                include_osint=bool(args.get("include_osint", True)),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 0),
                depth=int(args.get("depth") or 1),
            )
        if tool == "read_industry_graph_node":
            return read_industry_graph_node(
                node_id=str(args.get("node_id") or ""),
                include_neighbors=bool(args.get("include_neighbors", False)),
                mainline=str(args.get("mainline") or ""),
                include_osint=bool(args.get("include_osint", True)),
                mode=str(args.get("mode") or "overview"),
                field=str(args.get("field") or ""),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 0),
                max_chars=int(args.get("max_chars") or 7000),
            )
        if tool == "control_industry_graph":
            launched = control_industry_graph(
                action=str(args.get("action") or "start_or_resume"),
                mode=str(args.get("mode") or "mainline"),
                query=str(args.get("query") or state.subject),
                run_id=str(args.get("run_id") or ""),
                node_ids=args.get("node_ids") if isinstance(args.get("node_ids"), list) else None,
                markets=args.get("markets") if isinstance(args.get("markets"), list) else None,
                budget=args.get("budget") if isinstance(args.get("budget"), dict) else None,
            )
            return self._await_industry_graph_result(state, launched)
        if tool == "run_stock_research":
            launched = analysis.run_stock_research(
                ticker=str(args.get("ticker") or state.ticker or state.subject),
                trade_date=args.get("trade_date"),
                force=bool(args.get("force", False)),
                session_id=state.session_id,
            )
            return self._await_analysis_job_result(state, launched)
        if tool == "run_market_discovery":
            launched = analysis.run_market_discovery(no_resume=bool(args.get("no_resume", False)), session_id=state.session_id)
            return self._await_analysis_job_result(state, launched)
        if tool == "get_analysis_jobs":
            return analysis.get_analysis_jobs(
                job_type=args.get("job_type"),
                ticker=args.get("ticker"),
                status=args.get("status"),
                limit=int(args.get("limit") or 10),
            )
        raise ValueError(f"unsupported research tool: {tool}")

    def _await_analysis_job_result(self, state: ResearchState, launched: Any) -> dict[str, Any]:
        if not isinstance(launched, dict):
            return {"status": "unexpected_job_response", "launch_result": launched}
        job = launched.get("job") if isinstance(launched.get("job"), dict) else {}
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            return launched

        started = time.perf_counter()
        snapshots: list[dict[str, Any]] = []
        last: dict[str, Any] = job
        job_type = str(job.get("job_type") or "")
        module_id = "tradingagents" if job_type == "stock_research" else "bettafish" if job_type == "market_discovery" else ""
        wait_seconds = capability_service.timeout_for_module(module_id, ASYNC_RESEARCH_WAIT_SECONDS) if module_id else ASYNC_RESEARCH_WAIT_SECONDS
        while (time.perf_counter() - started) < wait_seconds:
            self._raise_if_stopped(state.thread_id)
            current = analysis_job_store.get(job_id)
            if current is None:
                break
            last = current.model_dump()
            snapshots.append({
                "status": last.get("status"),
                "current_stage": last.get("current_stage"),
                "updated_at": last.get("updated_at"),
                "latest_progress": (last.get("progress_log") or [])[-1] if last.get("progress_log") else None,
            })
            if last.get("status") in {"completed", "failed", "cancelled"}:
                break
            self._save_state(state, f"等待后台研究任务完成：{last.get('job_type')} {last.get('current_stage')}。")
            time.sleep(ASYNC_RESEARCH_POLL_SECONDS)

        status = str(last.get("status") or "unknown")
        result: dict[str, Any] = {
            "status": f"analysis_job_{status}",
            "job": last,
            "wait": {
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "completed": status == "completed",
                "snapshot_count": len(snapshots),
                "recent_snapshots": snapshots[-8:],
            },
            "launch_result": launched,
        }
        async_jobs = state.metrics.setdefault("async_jobs", [])
        if isinstance(async_jobs, list):
            async_jobs.append({
                "job_id": job_id,
                "job_type": last.get("job_type"),
                "status": status,
                "output_report_id": last.get("output_report_id"),
                "wait_elapsed_ms": result["wait"]["elapsed_ms"],
            })
        if status == "completed" and last.get("output_report_id"):
            report_id = str(last.get("output_report_id"))
            try:
                result["report"] = report_library.get_report_detail(report_id)
                result["report_extract"] = report_library.query_report(
                    report_id=report_id,
                    question=state.user_goal or state.subject,
                    max_sections=4,
                    per_section_chars=2400,
                    total_chars=9000,
                )
            except Exception as exc:
                result["report_read_error"] = str(exc)
        elif status in {"failed", "cancelled"}:
            state.gaps.append(f"后台研究任务 {job_id} {status}：{last.get('error') or ((last.get('progress_log') or [''])[-1])}")
        else:
            result["status"] = "analysis_job_timeout"
            state.gaps.append(f"后台研究任务 {job_id} 未在 {wait_seconds} 秒等待预算内完成，不能作为有效证据。")
        return result

    def _await_industry_graph_result(self, state: ResearchState, launched: Any) -> dict[str, Any]:
        if not isinstance(launched, dict):
            return {"status": "unexpected_graph_response", "launch_result": launched}
        run = launched.get("run") if isinstance(launched.get("run"), dict) else {}
        run_id = str(launched.get("run_id") or run.get("id") or "").strip()
        action = str(launched.get("action") or "")
        if not run_id or action in {"pause"}:
            return launched

        started = time.perf_counter()
        snapshots: list[dict[str, Any]] = []
        last = run
        completed_statuses = {"completed", "done", "succeeded", "success", "failed", "error", "cancelled", "paused"}
        wait_seconds = capability_service.timeout_for_module("tradinggraph", INDUSTRY_GRAPH_WAIT_SECONDS)
        while (time.perf_counter() - started) < wait_seconds:
            self._raise_if_stopped(state.thread_id)
            status_payload = read_industry_graph(action="get_run_status", run_id=run_id)
            current = status_payload.get("run") if isinstance(status_payload, dict) and isinstance(status_payload.get("run"), dict) else {}
            if current:
                last = current
            status = str((last or {}).get("status") or (last or {}).get("state") or "").lower()
            snapshots.append({
                "status": status,
                "updated_at": (last or {}).get("updated_at"),
                "stage": (last or {}).get("stage") or (last or {}).get("current_stage"),
            })
            if status in completed_statuses:
                break
            self._save_state(state, f"等待产业链透视研究任务完成：run_id={run_id} status={status or 'unknown'}。")
            time.sleep(INDUSTRY_GRAPH_POLL_SECONDS)

        status = str((last or {}).get("status") or (last or {}).get("state") or "unknown").lower()
        result: dict[str, Any] = {
            "status": f"industry_graph_{status}",
            "run": last,
            "wait": {
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "completed": status in {"completed", "done", "succeeded", "success"},
                "snapshot_count": len(snapshots),
                "recent_snapshots": snapshots[-8:],
            },
            "launch_result": launched,
        }
        async_jobs = state.metrics.setdefault("async_jobs", [])
        if isinstance(async_jobs, list):
            async_jobs.append({
                "job_id": run_id,
                "job_type": "tradinggraph",
                "status": status,
                "output_report_id": None,
                "wait_elapsed_ms": result["wait"]["elapsed_ms"],
            })
        if result["wait"]["completed"]:
            mainline = str((last or {}).get("mainline") or (last or {}).get("query") or state.subject or "")
            try:
                result["graph_summary"] = read_industry_graph(action="get_graph_summary", mainline=mainline, limit=160)
            except Exception as exc:
                result["graph_read_error"] = str(exc)
        elif status in {"failed", "error", "cancelled"}:
            state.gaps.append(f"产业链透视任务 {run_id} {status}：{(last or {}).get('error') or (last or {}).get('message') or ''}")
        elif status == "paused":
            state.gaps.append(f"产业链透视任务 {run_id} 已暂停，不能作为完成研究证据。")
        else:
            result["status"] = "industry_graph_timeout"
            state.gaps.append(f"产业链透视任务 {run_id} 未在 {wait_seconds} 秒等待预算内完成，不能作为有效证据。")
        return result

    def _preflight_agent_action(self, state: ResearchState, tool: str, args: dict[str, Any]) -> str:
        required_skill = skill_manager.required_skill_for_tool(tool)
        if required_skill and not skill_manager.is_tool_skill_active(tool, session_id=f"research:{state.thread_id}"):
            return f"{tool} 需要先调用 activate_skill(name=\"{required_skill}\") 读取完整 Skill 规范。"
        if tool == "read_research_record":
            route_key = self._read_record_route_key(args)
            read_routes = state.metrics.get("read_record_routes") if isinstance(state.metrics.get("read_record_routes"), dict) else {}
            route_state = read_routes.get(route_key) if isinstance(read_routes, dict) else None
            if isinstance(route_state, dict) and route_state.get("completed"):
                return "该研究档案章节窗口已读取完成，重复读取不会获得更多内容；请改读其他 section、使用其他工具，或基于已有内容形成判断。"
        if tool == "web_research" and state.budget.web_batches >= state.max_web_batches:
            return f"web_research 已达到全局预算 {state.budget.web_batches}/{state.max_web_batches}，禁止继续搜索。"
        if tool == "web_research" and state.budget.loop_web_batches >= state.max_web_batches_per_loop:
            return f"web_research 已达到本轮预算 {state.budget.loop_web_batches}/{state.max_web_batches_per_loop}，本轮禁止继续搜索。"
        return ""

    def _mark_tool_usage(self, state: ResearchState, tool: str, args: dict[str, Any]) -> None:
        counts = state.metrics.setdefault("tool_usage_counts", {})
        if not isinstance(counts, dict):
            counts = {}
            state.metrics["tool_usage_counts"] = counts
        route_key = self._tool_route_key(tool, args)
        counts[route_key] = int(counts.get(route_key) or 0) + 1

    def _update_tool_signal_state(self, state: ResearchState, tool: str, args: dict[str, Any], result: Any, status: str, summary: str) -> None:
        route_key = self._tool_route_key(tool, args)
        if status != "ok":
            failures = state.metrics.setdefault("tool_failures", {})
            if not isinstance(failures, dict):
                failures = {}
                state.metrics["tool_failures"] = failures
            failures[route_key] = int(failures.get(route_key) or 0) + 1
        if tool == "read_research_record" and status == "ok" and isinstance(result, dict):
            window = result.get("read_window") if isinstance(result.get("read_window"), dict) else {}
            if window and not bool(window.get("has_more")):
                read_routes = state.metrics.setdefault("read_record_routes", {})
                if not isinstance(read_routes, dict):
                    read_routes = {}
                    state.metrics["read_record_routes"] = read_routes
                read_routes[self._read_record_route_key(args)] = {
                    "completed": True,
                    "returned_chars": window.get("returned_chars"),
                    "total_chars": window.get("total_chars"),
                    "finished_at": _now(),
                }

    def _tool_route_key(self, tool: str, args: dict[str, Any]) -> str:
        if tool == "get_stock_data_package":
            return f"{tool}:{self._datahub_route_section(args)}"
        if tool == "web_research":
            return "web_research"
        if tool in {"run_stock_research", "run_market_discovery", "control_industry_graph"}:
            return tool
        return tool

    def _datahub_route_section(self, args: dict[str, Any]) -> str:
        mode = str(args.get("mode") or "overview")
        if mode != "section":
            return "overview"
        section = str(args.get("section") or "").strip()
        return section or "unknown"

    def _read_record_route_key(self, args: dict[str, Any]) -> str:
        record_id = str(args.get("record_id") or "").strip()
        section = str(args.get("section") or "__default__").strip() or "__default__"
        offset = int(args.get("offset") or 0)
        return f"read_research_record:{record_id}:{section}:{offset}"

    def _summarize_tool_result(self, tool: str, result: Any) -> str:
        if isinstance(result, dict):
            if tool == "read_research_record" and result.get("status") == "record_not_found":
                return f"read_research_record 未命中 record_id，返回候选 records={len(result.get('query_candidates') or [])}；请用候选 record_id 继续读取。"
            if result.get("error"):
                return f"{tool} 返回错误：{result.get('error')}"
            if tool == "activate_skill":
                skill = result.get("skill") if isinstance(result.get("skill"), dict) else {}
                return f"已激活 Skill：{skill.get('name') or result.get('required_skill') or ''}".strip()
            if tool == "web_research":
                return f"联网搜索完成，sources={len(result.get('sources') or [])}, status={result.get('status')}"
            if tool in {"run_stock_research", "run_market_discovery"}:
                job = result.get("job") if isinstance(result.get("job"), dict) else {}
                return f"{tool} 等待结束：{result.get('status')} {job.get('job_id') or ''} report={job.get('output_report_id') or ''}".strip()
            if tool == "control_industry_graph":
                run = result.get("run") if isinstance(result.get("run"), dict) else {}
                return f"产业链透视等待结束：{result.get('status') or result.get('message') or 'ok'} run={run.get('id') or ''}"
            if "records" in result:
                return f"{tool} 返回 records={len(result.get('records') or [])}"
            if "read_window" in result:
                window = result.get("read_window") or {}
                return f"{tool} 返回窗口 chars={window.get('returned_chars')}, has_more={window.get('has_more')}"
        if isinstance(result, list):
            return f"{tool} 返回 {len(result)} 条记录"
        return f"{tool} 返回结果已记录"

    def _record_agent_tool_run(self, state: ResearchState, tool: str, args: dict[str, Any], status: str, summary: str, elapsed_ms: int) -> None:
        record = {
            "round": state.loop_iterations,
            "tool": tool,
            "arguments": args,
            "status": status,
            "summary": summary[:500],
            "elapsed_ms": elapsed_ms,
            "finished_at": _now(),
        }
        runs = state.metrics.setdefault("agent_tool_runs", [])
        if isinstance(runs, list):
            runs.append(record)
        state.metrics["tool_calls_used"] = state.tool_calls_used
        state.metrics["high_cost_runs_used"] = state.high_cost_runs_used
        try:
            root = self._run_dir(state.thread_id)
            root.mkdir(parents=True, exist_ok=True)
            with (root / "tool_runs.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._append_loop_event(state, "Tool Attempt", f"- tool: {tool}\n- status: {status}\n- elapsed_ms: {elapsed_ms}\n- summary: {summary[:500]}")
        except Exception:
            return

    def _save_state(self, state: ResearchState, conclusion: str) -> None:
        self._patch_thread(
            state.thread_id,
            subject_type=state.subject_type,
            plan=state.plan,
            evidence=[],
            gaps=_dedupe_strings(state.gaps),
            recommended_actions=_dedupe_actions(state.recommended_actions),
            related_research=state.related_research,
            validation_results=state.validation_results,
            metrics=self._current_metrics(state),
            current_conclusion=conclusion,
        )

    def _execute_step(self, state: ResearchState, step_id: str, fn: Any) -> None:
        self._raise_if_stopped(state.thread_id)
        if self._step_status(state.plan, step_id) == "done":
            return
        started = time.perf_counter()
        final_status = "done"
        self._mark_step(state.plan, step_id, "in_progress")
        self._save_state(state, f"正在执行研究步骤：{step_id}")
        try:
            conclusion = fn(state)
            self._mark_step(state.plan, step_id, "done", conclusion)
        except Exception as exc:
            state.gaps.append(f"{step_id} 步骤失败：{exc}")
            self._mark_step(state.plan, step_id, "failed", str(exc))
            final_status = "failed"
        finally:
            self._record_step_timing(state, step_id, started, final_status)

    def _step_status(self, plan: list[dict[str, Any]], step_id: str) -> str:
        for step in plan:
            if step.get("step_id") == step_id:
                return str(step.get("status") or "")
        return ""

    def _record_step_timing(
        self,
        state: ResearchState,
        step_id: str,
        started: float,
        status: str,
        tool_type: str = "",
    ) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        timings = state.metrics.setdefault("step_timings", [])
        if isinstance(timings, list):
            timings.append({
                "step_id": step_id,
                "tool_type": tool_type,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "finished_at": _now(),
            })

    def _current_metrics(self, state: ResearchState) -> dict[str, Any]:
        metrics = dict(state.metrics or {})
        metrics["quality"] = _draft_quality_summary(state.last_validation)
        metrics["budget"] = {
            "web_batches": state.budget.web_batches,
            "max_web_batches": state.max_web_batches,
            "loop_web_batches": state.budget.loop_web_batches,
            "max_web_batches_per_loop": state.max_web_batches_per_loop,
            "tool_calls_used": state.tool_calls_used,
            "max_tool_calls": state.max_tool_calls,
            "loop_tool_calls": state.budget.loop_tool_calls,
            "max_tool_calls_per_loop": state.max_tool_calls_per_loop,
            "high_cost_runs_used": state.high_cost_runs_used,
            "max_high_cost_runs": state.max_high_cost_runs,
            "loop_high_cost_runs": state.budget.loop_high_cost_runs,
            "max_high_cost_runs_per_loop": state.max_high_cost_runs_per_loop,
        }
        metrics["max_iterations"] = state.max_iterations
        metrics["max_tool_calls"] = state.max_tool_calls
        metrics["max_tool_calls_per_loop"] = state.max_tool_calls_per_loop
        metrics["max_high_cost_runs"] = state.max_high_cost_runs
        metrics["max_high_cost_runs_per_loop"] = state.max_high_cost_runs_per_loop
        metrics["max_web_batches"] = state.max_web_batches
        metrics["max_web_batches_per_loop"] = state.max_web_batches_per_loop
        metrics["tool_calls_used"] = state.tool_calls_used
        metrics["high_cost_runs_used"] = state.high_cost_runs_used
        metrics["tool_failures"] = state.metrics.get("tool_failures") or {}
        metrics["tool_usage_counts"] = state.metrics.get("tool_usage_counts") or {}
        metrics["async_jobs"] = state.metrics.get("async_jobs") or []
        metrics["loop_iterations"] = state.loop_iterations
        metrics["last_validation"] = state.last_validation
        metrics["research_log_path"] = str(self._run_dir(state.thread_id) / "LOOP.md")
        metrics["research_strategy_path"] = str(self._strategy_path(state.thread_id))
        timings = metrics.get("step_timings")
        if isinstance(timings, list):
            metrics["total_step_elapsed_ms"] = sum(int(item.get("elapsed_ms") or 0) for item in timings if isinstance(item, dict))
            slowest = sorted(
                [item for item in timings if isinstance(item, dict)],
                key=lambda item: int(item.get("elapsed_ms") or 0),
                reverse=True,
            )[:5]
            metrics["slowest_steps"] = slowest
        return metrics

    def _finalize_metrics(self, state: ResearchState) -> dict[str, Any]:
        metrics = self._current_metrics(state)
        metrics["completed_at"] = _now()
        started_at = str(metrics.get("started_at") or "")
        wall_ms = _elapsed_ms_between(started_at, str(metrics.get("completed_at") or ""))
        if wall_ms is not None:
            total_step_ms = metrics.get("total_step_elapsed_ms")
            try:
                wall_ms = max(wall_ms, int(total_step_ms or 0))
            except Exception:
                pass
            metrics["wall_elapsed_ms"] = wall_ms
        return metrics

    def _raise_if_stopped(self, thread_id: str) -> None:
        status = str(self.get_thread(thread_id).get("status") or "")
        if status == "cancelled":
            raise RuntimeError("research thread cancelled")
        if status == "paused":
            raise RuntimeError("research thread paused")

    def _reset_failed_steps(self, thread_id: str) -> None:
        thread = self.get_thread(thread_id)
        plan = []
        for step in thread.get("plan") or []:
            if isinstance(step, dict) and step.get("status") in {"failed", "in_progress"}:
                updated = dict(step)
                updated["status"] = "pending"
                updated["conclusion"] = ""
                plan.append(updated)
            else:
                plan.append(step)
        self._patch_thread(
            thread_id,
            status="pending",
            error=None,
            plan=plan,
            current_conclusion="研究已重置失败步骤，等待恢复执行。",
            completed_at=None,
        )

    def _mark_step(self, plan: list[dict[str, Any]], step_id: str, status: str, conclusion: str = "") -> None:
        for step in plan:
            if step.get("step_id") == step_id:
                step["status"] = status
                if conclusion:
                    step["conclusion"] = conclusion
                step["updated_at"] = _now()
                return

    def _find_active_thread(self, subject: str, subject_type: str, depth: str, session_id: str) -> dict[str, Any] | None:
        normalized_subject = _normalize_subject_key(subject)
        requested_ticker = self._resolve_ticker(subject) if subject_type in {"stock", "unknown"} else None
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select * from research_threads
                where session_id=?
                  and depth=?
                  and status in ('pending', 'in_progress', 'paused', 'waiting_approval', 'failed')
                order by updated_at desc
                limit 20
                """,
                (session_id, depth),
            ).fetchall()
        for row in rows:
            thread = self._row_to_thread(row)
            same_subject = _normalize_subject_key(str(thread.get("subject") or "")) == normalized_subject
            thread_ticker = self._resolve_ticker(str(thread.get("subject") or "")) if thread.get("subject_type") in {"stock", "unknown"} else None
            same_ticker = bool(requested_ticker and thread_ticker and requested_ticker == thread_ticker)
            same_type = subject_type == "unknown" or thread.get("subject_type") in {subject_type, "unknown"}
            if (same_subject or same_ticker) and same_type:
                return thread
        return None

    def _find_recent_completed_thread(self, subject: str, subject_type: str, depth: str, session_id: str) -> dict[str, Any] | None:
        normalized_subject = _normalize_subject_key(subject)
        requested_ticker = self._resolve_ticker(subject) if subject_type in {"stock", "unknown"} else None
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select * from research_threads
                where session_id=?
                  and depth=?
                  and status='completed'
                order by completed_at desc, updated_at desc
                limit 30
                """,
                (session_id, depth),
            ).fetchall()
        for row in rows:
            thread = self._row_to_thread(row)
            completed_at = str(thread.get("completed_at") or thread.get("updated_at") or "")
            age_days = _age_days(completed_at)
            if age_days is not None and age_days > 7:
                continue
            same_subject = _normalize_subject_key(str(thread.get("subject") or "")) == normalized_subject
            thread_ticker = self._resolve_ticker(str(thread.get("subject") or "")) if thread.get("subject_type") in {"stock", "unknown"} else None
            same_ticker = bool(requested_ticker and thread_ticker and requested_ticker == thread_ticker)
            same_type = subject_type == "unknown" or thread.get("subject_type") in {subject_type, "unknown"}
            if (same_subject or same_ticker) and same_type:
                return thread
        return None

    def _resolve_ticker(self, subject: str) -> str | None:
        value = subject.strip().upper()
        if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value):
            if "." in value:
                return value
            if value.startswith("6"):
                return f"{value}.SH"
            if value.startswith(("0", "3")):
                return f"{value}.SZ"
            if value.startswith(("4", "8")):
                return f"{value}.BJ"
        try:
            payload = datahub_client.search_stock_symbol(subject, limit=5)
        except Exception:
            return None
        rows = payload if isinstance(payload, list) else payload.get("items") or payload.get("results") or payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or row.get("ts_code") or row.get("code") or "").strip().upper()
            if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", ticker):
                return ticker
        return None

    def _read_playbook_context(self) -> str:
        path = MEMORY_DIR / "playbook.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")[:6000]

    def _run_dir(self, thread_id: str) -> Path:
        return RUNS_DIR / _safe_slug(thread_id)

    def _draft_path(self, state_or_thread_id: ResearchState | str) -> Path:
        thread_id = state_or_thread_id.thread_id if isinstance(state_or_thread_id, ResearchState) else str(state_or_thread_id)
        return self._run_dir(thread_id) / "draft.md"

    def _strategy_path(self, state_or_thread_id: ResearchState | str) -> Path:
        thread_id = state_or_thread_id.thread_id if isinstance(state_or_thread_id, ResearchState) else str(state_or_thread_id)
        return self._run_dir(thread_id) / "research_strategy.md"

    def _read_current_draft(self, state: ResearchState) -> str:
        path = self._draft_path(state)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _write_current_draft(self, state: ResearchState, content: str) -> None:
        path = self._draft_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(content or "").strip()
        if not text:
            return
        path.write_text(text[:50000] + "\n", encoding="utf-8")

    def _read_research_strategy(self, state: ResearchState) -> str:
        path = self._strategy_path(state)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _write_research_strategy(self, state: ResearchState, content: str) -> None:
        path = self._strategy_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(content or "").strip()
        if not text:
            return
        path.write_text(text[:40000] + "\n", encoding="utf-8")
        state.metrics["research_strategy_path"] = str(path)

    def _strategy_needs_initialization(self, state: ResearchState) -> bool:
        text = self._read_research_strategy(state).strip()
        return not text or "等待研究 Agent 初始化" in text

    def _initialize_research_strategy(self, state: ResearchState, playbook: str) -> str:
        now = _now()
        if llm_client.configured:
            prompt = (
                "你是投资研究策略设计者。只生成本次 Deep Research 的 research_strategy.md，"
                "不要写研究报告，不要调用工具，不要输出 JSON，不要规划具体工具调用。"
            )
            payload = {
                "research_goal": state.user_goal or state.subject,
                "subject": state.subject,
                "subject_type": state.subject_type,
                "ticker": state.ticker,
                "constraints": state.constraints,
                "playbook_excerpt": playbook[:6000],
                "required_sections": [
                    "Research Goal",
                    "Playbook-Derived Focus",
                    "Research Approach",
                    "Cross-Analysis Focus",
                    "Current Thesis",
                    "Key Uncertainties",
                    "Reviewer Guidance",
                    "Strategy Revision Log",
                ],
            }
            try:
                text = llm_client.chat_text(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                    ],
                    session_id=state.session_id,
                    purpose="deep_research_strategy",
                )
                if str(text or "").strip():
                    return str(text).strip()[:40000]
            except Exception as exc:
                state.gaps.append(f"研究策略初始化 LLM 不可用，使用保守模板：{exc}")
        focus = "从用户 playbook 提炼本次研究侧重点；如果 playbook 为空，则以用户研究目标为准。"
        if playbook.strip():
            focus = playbook[:1200]
        return (
            f"# Research Strategy\n\n"
            f"## Research Goal\n{state.user_goal or state.subject}\n\n"
            f"## Playbook-Derived Focus\n{focus}\n\n"
            f"## Research Approach\n围绕最能改变结论的变量展开研究；不要把资料摘要包装成分析结论。\n\n"
            f"## Cross-Analysis Focus\n比较不同研究材料之间的印证、矛盾和缺口，说明它们如何改变 thesis。\n\n"
            f"## Current Thesis\n暂无。等待研究 Agent 基于首轮材料形成阶段性 thesis。\n\n"
            f"## Key Uncertainties\n- 哪些变量最可能改变研究结论尚待识别。\n\n"
            f"## Reviewer Guidance\n暂无。\n\n"
            f"## Strategy Revision Log\n- {now}: initialized.\n"
        )

    def _apply_agent_strategy_update(self, state: ResearchState, decision: dict[str, Any], iteration: int) -> None:
        if not isinstance(decision, dict):
            return
        update = str(decision.get("strategy_update") or "").strip()
        deltas = decision.get("analysis_delta") if isinstance(decision.get("analysis_delta"), list) else []
        if not update and not deltas:
            return
        current = self._read_research_strategy(state).strip() or "# Research Strategy\n"
        lines = [
            "",
            f"## Agent Strategy Update Round {iteration}",
            f"- time: {_now()}",
        ]
        if update:
            lines.extend(["", "### Strategy Update", update[:4000]])
        if deltas:
            lines.append("")
            lines.append("### Analysis Delta")
            for item in deltas[:8]:
                if isinstance(item, dict):
                    lines.append(f"- material: {str(item.get('material') or item.get('new_information') or '')[:500]}")
                    lines.append(f"  interpretation: {str(item.get('interpretation') or '')[:500]}")
                    lines.append(f"  changed_view: {str(item.get('changed_view') or '')[:500]}")
                    lines.append(f"  remaining_uncertainty: {str(item.get('remaining_uncertainty') or '')[:500]}")
                elif isinstance(item, str):
                    lines.append(f"- {item[:800]}")
        self._write_research_strategy(state, (current + "\n" + "\n".join(lines)).strip())

    def _apply_validator_strategy_patch(self, state: ResearchState, validation: dict[str, Any], iteration: int) -> None:
        if not isinstance(validation, dict):
            return
        patch = validation.get("strategy_patch") if isinstance(validation.get("strategy_patch"), dict) else {}
        if not patch:
            return
        current = self._read_research_strategy(state).strip() or "# Research Strategy\n"
        lines = [
            "",
            f"## Reviewer Strategy Patch Round {iteration}",
            f"- time: {_now()}",
            f"- status: {validation.get('status') or 'fail'}",
            f"- analysis_quality: {validation.get('analysis_quality') or ''}",
            "",
        ]
        guidance = str(patch.get("reviewer_guidance") or patch.get("next_focus") or validation.get("reason") or "").strip()
        if guidance:
            lines.extend(["### Reviewer Guidance", guidance[:3000], ""])
        for key in ("current_thesis_correction", "cross_analysis_focus_update"):
            value = str(patch.get(key) or "").strip()
            if value:
                lines.extend([f"### {key}", value[:2000], ""])
        additions = patch.get("key_uncertainties_add")
        if isinstance(additions, list) and additions:
            lines.append("### Key Uncertainties Add")
            for item in additions[:10]:
                lines.append(f"- {str(item)[:500]}")
            lines.append("")
        self._write_research_strategy(state, (current + "\n" + "\n".join(lines)).strip())

    def _append_system_event(self, state: ResearchState, message: str) -> None:
        events = state.metrics.setdefault("system_events", [])
        if isinstance(events, list):
            events.append({"time": _now(), "message": str(message or "")[:500]})
            state.metrics["system_events"] = events[-50:]
        self._append_loop_event(state, "System Event", f"- message: {str(message or '')[:500]}")

    def _ensure_loop_workspace(self, thread: dict[str, Any], state: ResearchState | None = None) -> None:
        thread_id = str(thread.get("thread_id") or (state.thread_id if state else ""))
        if not thread_id:
            return
        root = self._run_dir(thread_id)
        (root / "drafts").mkdir(parents=True, exist_ok=True)
        (root / "validations").mkdir(parents=True, exist_ok=True)
        loop_path = root / "LOOP.md"
        if loop_path.exists():
            return
        playbook = self._read_playbook_context()
        metrics = thread.get("metrics") if isinstance(thread.get("metrics"), dict) else {}
        allowed = metrics.get("allowed_tools") or []
        blocked = metrics.get("blocked_tools") or []
        constraints = str(metrics.get("constraints") or "")
        content = (
            f"# Deep Research Loop: {thread.get('subject') or ''}\n\n"
            "## Goal\n"
            f"{thread.get('user_goal') or thread.get('subject') or ''}\n\n"
            "## Subject\n"
            f"- subject: {thread.get('subject') or ''}\n"
            f"- subject_type: {thread.get('subject_type') or 'unknown'}\n"
            f"- depth: {thread.get('depth') or 'standard'}\n\n"
            "## User Constraints\n"
            f"{constraints or '暂无额外约束。'}\n\n"
            "## Tool Policy\n"
            f"- allowed_tools: {', '.join(str(item) for item in allowed) if allowed else 'default'}\n"
            f"- blocked_tools: {', '.join(str(item) for item in blocked) if blocked else 'none'}\n\n"
            "## Playbook Excerpt\n"
            f"{playbook[:2400] if playbook.strip() else 'playbook.md 为空或不存在。'}\n\n"
            "## Current Understanding\n"
            "暂无。等待研究 Agent 第一轮形成阶段性认知。\n\n"
            "## Open Questions\n"
            "- [ ] 研究目标尚未通过独立验证。\n\n"
            "## Tool Attempts\n"
            "暂无。\n\n"
            "## Validator Feedback\n"
            "暂无。\n\n"
            "## Draft Status\n"
            "pending\n"
        )
        loop_path.write_text(content, encoding="utf-8")
        tool_runs = root / "tool_runs.jsonl"
        if not tool_runs.exists():
            tool_runs.write_text("", encoding="utf-8")
        draft_path = root / "draft.md"
        if not draft_path.exists():
            draft_path.write_text(f"# {thread.get('subject') or ''}\n\n## 研究摘要\n暂无。\n\n## 待验证判断\n暂无。\n", encoding="utf-8")
        strategy_path = root / "research_strategy.md"
        if not strategy_path.exists():
            strategy_path.write_text(
                "# Research Strategy\n\n"
                "## Research Goal\n"
                f"{thread.get('user_goal') or thread.get('subject') or ''}\n\n"
                "## Playbook-Derived Focus\n"
                "等待研究 Agent 初始化。\n\n"
                "## Research Approach\n"
                "等待研究 Agent 初始化。\n\n"
                "## Cross-Analysis Focus\n"
                "等待研究 Agent 初始化。\n\n"
                "## Current Thesis\n"
                "暂无。\n\n"
                "## Key Uncertainties\n"
                "- 待识别。\n\n"
                "## Reviewer Guidance\n"
                "暂无。\n\n"
                "## Strategy Revision Log\n"
                f"- {_now()}: workspace created.\n",
                encoding="utf-8",
            )

    def _read_loop_memory(self, state: ResearchState) -> str:
        path = self._run_dir(state.thread_id) / "LOOP.md"
        if not path.exists():
            self._ensure_loop_workspace(self.get_thread(state.thread_id), state=state)
        try:
            return path.read_text(encoding="utf-8")[-18000:]
        except Exception:
            return ""

    def _append_loop_event(self, state: ResearchState, title: str, body: str) -> None:
        path = self._run_dir(state.thread_id) / "LOOP.md"
        if not path.exists():
            self._ensure_loop_workspace(self.get_thread(state.thread_id), state=state)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n## {title}\n- time: {_now()}\n{body.strip()}\n")

    def _append_loop_round(self, state: ResearchState, iteration: int, decision: dict[str, Any], step: dict[str, Any]) -> None:
        actions = step.get("actions") if isinstance(step.get("actions"), list) else []
        lines = [
            f"### Round {iteration}",
            f"- focus: {decision.get('focus') if isinstance(decision, dict) else ''}",
            f"- draft_file: drafts/round_{iteration:02d}.md",
            f"- tool_actions: {len(actions)}",
            f"- step_summary: {step.get('conclusion') or ''}",
            "",
            "#### Round Draft Preview",
            state.last_draft[:1800] or "暂无。",
        ]
        self._append_loop_event(state, f"Research Round {iteration}", "\n".join(lines))

    def _append_loop_validation(self, state: ResearchState, iteration: int, validation: dict[str, Any]) -> None:
        missing_items = validation.get("missing_analysis") or []
        missing = "\n".join(f"- [ ] {item}" for item in missing_items) or "- 无"
        overclaims = "\n".join(f"- {item}" for item in validation.get("overclaims") or []) or "- 无"
        body = (
            f"### Validation Round {iteration}\n"
            f"- status: {validation.get('status') or 'fail'}\n"
            f"- confidence: {validation.get('confidence') or 'low'}\n"
            f"- analysis_quality: {validation.get('analysis_quality') or ''}\n"
            f"- reason: {validation.get('reason') or ''}\n\n"
            "#### Playbook Alignment\n"
            f"{validation.get('playbook_alignment') or ''}\n\n"
            "#### Missing Analysis\n"
            f"{missing}\n\n"
            "#### Overclaims\n"
            f"{overclaims}\n\n"
            "#### Strategy Patch\n"
            f"```json\n{json.dumps(validation.get('strategy_patch') or {}, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )
        self._append_loop_event(state, f"Validator Feedback Round {iteration}", body)

    def _write_round_draft(self, state: ResearchState, iteration: int, decision: dict[str, Any], step: dict[str, Any]) -> None:
        root = self._run_dir(state.thread_id) / "drafts"
        root.mkdir(parents=True, exist_ok=True)
        content = (
            f"# Round {iteration} Draft\n\n"
            f"- generated_at: {_now()}\n"
            f"- focus: {decision.get('focus') if isinstance(decision, dict) else ''}\n"
            f"- tool_summary: {step.get('conclusion') or ''}\n\n"
            "## Draft\n"
            f"{state.last_draft or '暂无。'}\n\n"
            "## Raw Decision\n"
            f"```json\n{json.dumps(decision if isinstance(decision, dict) else {}, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )
        (root / f"round_{iteration:02d}.md").write_text(content, encoding="utf-8")

    def _write_round_validation(self, state: ResearchState, iteration: int, validation: dict[str, Any]) -> None:
        root = self._run_dir(state.thread_id) / "validations"
        root.mkdir(parents=True, exist_ok=True)
        content = (
            f"# Round {iteration} Validation\n\n"
            f"- checked_at: {validation.get('checked_at') or _now()}\n"
            f"- status: {validation.get('status') or 'fail'}\n"
            f"- confidence: {validation.get('confidence') or 'low'}\n\n"
            f"- analysis_quality: {validation.get('analysis_quality') or ''}\n\n"
            "## Reason\n"
            f"{validation.get('reason') or ''}\n\n"
            "## Playbook Alignment\n"
            f"{validation.get('playbook_alignment') or ''}\n\n"
            "## Missing Analysis\n"
            + "\n".join(f"- {item}" for item in (validation.get("missing_analysis") or []))
            + "\n\n## Overclaims\n"
            + "\n".join(f"- {item}" for item in validation.get("overclaims") or [])
            + "\n\n## Strategy Patch\n"
            f"```json\n{json.dumps(validation.get('strategy_patch') or {}, ensure_ascii=False, indent=2, default=str)}\n```\n"
        )
        (root / f"round_{iteration:02d}.md").write_text(content, encoding="utf-8")

    def _read_existing_record(self, subject: str, subject_type: str) -> dict[str, str] | None:
        folder = self._record_folder(subject_type)
        slug = _safe_slug(subject)
        path = RECORDS_DIR / folder / f"{slug}.md"
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        return {"record_id": f"{folder}/{slug}", "title": subject, "summary": content[:2400]}

    def _write_record(self, thread: dict[str, Any]) -> None:
        folder = self._record_folder(str(thread.get("subject_type") or "unknown"))
        root = RECORDS_DIR / folder
        root.mkdir(parents=True, exist_ok=True)
        slug = _safe_slug(str(thread.get("subject") or thread.get("thread_id")))
        path = root / f"{slug}.md"
        now = _now()
        title = str(thread.get("subject") or "")
        validation = thread.get("validation_results") or []
        metrics = thread.get("metrics") if isinstance(thread.get("metrics"), dict) else {}
        last_validation = metrics.get("last_validation") if isinstance(metrics.get("last_validation"), dict) else {}
        quality = _draft_quality_summary(last_validation)
        draft_path = self._draft_path(str(thread.get("thread_id") or ""))
        if draft_path.exists():
            conclusion_text = draft_path.read_text(encoding="utf-8")
        else:
            conclusion_text = _strip_conclusion_preamble(str(thread.get("current_conclusion") or "暂无结论"))
        conclusion_text = _ensure_record_head(conclusion_text, gaps=[], quality=quality)
        meta = (
            f"# {title}\n\n"
            "<!-- research_record_meta\n"
            f"- record_id: {folder}/{slug}\n"
            f"- updated_at: {now}\n"
            f"- latest_thread_id: {thread.get('thread_id')}\n"
            f"- subject_type: {thread.get('subject_type')}\n"
            f"- depth: {thread.get('depth')}\n"
            f"- user_goal: {str(thread.get('user_goal') or '').replace(chr(10), ' ')[:240]}\n"
            f"- validator_status: {last_validation.get('status') or ''}\n"
            f"- validator_confidence: {last_validation.get('confidence') or ''}\n"
            f"- quality_level: {quality.get('level')}\n"
            f"- quality_score: {quality.get('score')}\n"
            f"- wall_elapsed_ms: {metrics.get('wall_elapsed_ms') or ''}\n"
            "-->\n\n"
        )
        path.write_text(meta + conclusion_text.strip() + "\n", encoding="utf-8")

    def _record_folder(self, subject_type: str) -> str:
        if subject_type in {"stock", "stocks"}:
            return "stocks"
        if subject_type in {"mainline", "mainlines"}:
            return "mainlines"
        if subject_type in {"comparison", "comparisons"}:
            return "comparisons"
        return "unknown" if subject_type not in {"unknown", ""} else "unknown"

    def _record_summary(self, path: Path, content: str) -> dict[str, Any]:
        rel = path.relative_to(RECORDS_DIR).with_suffix("")
        title = content.splitlines()[0].lstrip("# ").strip() if content.strip() else path.stem
        updated = _match_value(content, "updated_at") or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        latest_thread_id = _match_value(content, "latest_thread_id")
        user_goal = _match_value(content, "user_goal") or ""
        validator_status = _match_value(content, "validator_status") or ""
        quality_level = _match_value(content, "quality_level") or ""
        conclusion = _first_meaningful_line(_extract_markdown_section(content, "研究摘要") or _extract_markdown_section(content, "结论摘要"))
        gaps_section = (
            _extract_markdown_section(content, "待验证判断")
            or _extract_markdown_section(content, "待验证问题")
            or _extract_markdown_section(content, "未解决问题")
        )
        gaps = [line for line in gaps_section.splitlines() if line.strip().startswith("-")]
        return {
            "record_id": str(rel).replace("\\", "/"),
            "title": title,
            "subject_type": path.parent.name,
            "updated_at": updated,
            "latest_thread_id": latest_thread_id,
            "user_goal": user_goal,
            "core_conclusion": conclusion,
            "gap_count": len(gaps),
            "validator_status": validator_status,
            "quality_level": quality_level,
            "sections": _public_record_sections(content),
            "file_size": len(content),
        }

    def _record_default_view(self, content: str) -> str:
        lines = []
        title = content.splitlines()[0].strip() if content.strip() else "# 研究档案"
        lines.append(title)
        summary = _extract_markdown_section(content, "研究摘要").strip() or _extract_markdown_section(content, "结论摘要").strip()
        pending = (
            _extract_markdown_section(content, "待验证判断").strip()
            or _extract_markdown_section(content, "待验证问题").strip()
            or _extract_markdown_section(content, "未解决问题").strip()
        )
        if len(summary) > 1200:
            summary = summary[:1200].rstrip() + "\n...（研究摘要较长，已截断；需要完整内容请读取 section=研究摘要）"
        if len(pending) > 1200:
            pending = pending[:1200].rstrip() + "\n...（待验证判断较长，已截断；需要完整内容请读取 section=待验证判断）"
        lines.append(f"\n## 研究摘要\n{summary or '暂无。'}")
        lines.append(f"\n## 待验证判断\n{pending or '暂无。'}")
        sections = _public_record_sections(content)
        lines.append("\n## 目录")
        seen: set[str] = set()
        shown = 0
        for item in sections:
            title = str(item.get("section") or "").strip()
            chars = item.get("chars")
            if not title or title in seen:
                continue
            seen.add(title)
            lines.append(f"- {title} ({chars} chars)")
            shown += 1
            if shown >= 40:
                lines.append("- ...（目录较长，已省略后续重复/低优先级章节）")
                break
        lines.append("\n需要正文细节时，按 section + offset + max_chars 分页读取。")
        return "\n".join(lines).strip() + "\n"

    def _record_summary_view(self, content: str) -> str:
        title = content.splitlines()[0].strip() if content.strip() else "# 研究档案"
        summary = _extract_markdown_section(content, "研究摘要").strip() or _extract_markdown_section(content, "结论摘要").strip()
        pending = (
            _extract_markdown_section(content, "待验证判断").strip()
            or _extract_markdown_section(content, "待验证问题").strip()
            or _extract_markdown_section(content, "未解决问题").strip()
        )
        return (
            f"{title}\n\n"
            f"## 研究摘要\n{summary or '暂无。'}\n\n"
            f"## 待验证判断\n{pending or '暂无。'}\n"
        )

    def _record_body_view(self, content: str) -> str:
        body = _strip_research_record_meta(content)
        body = _remove_markdown_sections(body, {"研究摘要", "结论摘要"})
        return body.strip() + "\n"

    def _record_path_from_id(self, record_id: str) -> Path:
        clean = str(record_id or "").replace("\\", "/").strip().strip("/")
        if ".." in clean:
            raise ValueError("invalid record_id")
        return (RECORDS_DIR / clean).with_suffix(".md")

    def _patch_thread(self, thread_id: str, **updates: Any) -> None:
        thread = self.get_thread(thread_id)
        thread.update(updates)
        thread["updated_at"] = _now()
        with self._lock, self._connect() as conn:
            self._upsert(conn, thread)

    def _upsert(self, conn: sqlite3.Connection, thread: dict[str, Any]) -> None:
        conn.execute(
            """
            insert into research_threads(
                thread_id, session_id, subject, subject_type, depth, status, user_goal,
                plan_json, evidence_json, gaps_json, recommended_actions_json,
                related_research_json, validation_results_json,
                claim_validation_json,
                metrics_json,
                current_conclusion, error, created_at, updated_at, completed_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(thread_id) do update set
                session_id=excluded.session_id,
                subject=excluded.subject,
                subject_type=excluded.subject_type,
                depth=excluded.depth,
                status=excluded.status,
                user_goal=excluded.user_goal,
                plan_json=excluded.plan_json,
                evidence_json=excluded.evidence_json,
                gaps_json=excluded.gaps_json,
                recommended_actions_json=excluded.recommended_actions_json,
                related_research_json=excluded.related_research_json,
                validation_results_json=excluded.validation_results_json,
                claim_validation_json=excluded.claim_validation_json,
                metrics_json=excluded.metrics_json,
                current_conclusion=excluded.current_conclusion,
                error=excluded.error,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            """,
            (
                thread["thread_id"],
                thread.get("session_id"),
                thread.get("subject"),
                thread.get("subject_type"),
                thread.get("depth"),
                thread.get("status"),
                thread.get("user_goal"),
                json.dumps(thread.get("plan") or [], ensure_ascii=False),
                json.dumps(thread.get("evidence") or [], ensure_ascii=False),
                json.dumps(thread.get("gaps") or [], ensure_ascii=False),
                json.dumps(thread.get("recommended_actions") or [], ensure_ascii=False),
                json.dumps(thread.get("related_research") or [], ensure_ascii=False),
                json.dumps(thread.get("validation_results") or [], ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                json.dumps(thread.get("metrics") or {}, ensure_ascii=False),
                thread.get("current_conclusion"),
                thread.get("error"),
                thread.get("created_at"),
                thread.get("updated_at"),
                thread.get("completed_at"),
            ),
        )

    def _row_to_thread(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "thread_id": row["thread_id"],
            "session_id": row["session_id"],
            "subject": row["subject"],
            "subject_type": row["subject_type"],
            "depth": row["depth"],
            "status": row["status"],
            "user_goal": row["user_goal"],
            "plan": _loads(row["plan_json"], []),
            "evidence": _loads(row["evidence_json"], []),
            "gaps": _loads(row["gaps_json"], []),
            "recommended_actions": _loads(row["recommended_actions_json"], []),
            "related_research": _loads(_row_get(row, "related_research_json"), []),
            "validation_results": _loads(_row_get(row, "validation_results_json"), []),
            "metrics": _loads(_row_get(row, "metrics_json"), {}),
            "current_conclusion": row["current_conclusion"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists research_threads (
                    thread_id text primary key,
                    session_id text not null,
                    subject text not null,
                    subject_type text not null,
                    depth text not null,
                    status text not null,
                    user_goal text,
                    plan_json text not null,
                    evidence_json text not null,
                    gaps_json text not null,
                    recommended_actions_json text not null,
                    related_research_json text not null default '[]',
                    validation_results_json text not null default '[]',
                    claim_validation_json text not null default '[]',
                    metrics_json text not null default '{}',
                    current_conclusion text,
                    error text,
                    created_at text not null,
                    updated_at text not null,
                    completed_at text
                );
                create index if not exists idx_research_threads_session_status on research_threads(session_id, status, updated_at);
                create index if not exists idx_research_threads_subject on research_threads(subject, updated_at);
                """
            )
            self._ensure_column(conn, "research_threads", "related_research_json", "text not null default '[]'")
            self._ensure_column(conn, "research_threads", "validation_results_json", "text not null default '[]'")
            self._ensure_column(conn, "research_threads", "claim_validation_json", "text not null default '[]'")
            self._ensure_column(conn, "research_threads", "metrics_json", "text not null default '{}'")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {ddl}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _elapsed_ms_between(started_at: str, completed_at: str) -> int | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
        return max(0, int((completed - started).total_seconds() * 1000))
    except Exception:
        return None


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _row_get(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except Exception:
        return None


def _bound_current_tool_result(result: Any, max_chars: int = 9000) -> Any:
    """Keep a single tool result safe for the immediate next Research Agent call."""
    if isinstance(result, dict):
        window = result.get("read_window")
        if isinstance(window, dict):
            bounded = dict(result)
            bounded["read_window"] = dict(window)
            content = str(bounded["read_window"].get("content") or "")
            if len(content) > max_chars:
                bounded["read_window"]["content"] = content[:max_chars]
                bounded["read_window"]["content_truncated_for_context"] = True
            return bounded
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return result
    return {
        "truncated_for_context": True,
        "chars": len(text),
        "preview": text[:max_chars],
        "message": "工具结果较长，下一轮如需细节应使用该工具的 section/offset/limit 参数继续读取。",
    }


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    return (text or "unknown")[:80]


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _dedupe_actions(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in values:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _first_meaningful_line(text: str) -> str:
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("#*- ")
        if not line:
            continue
        if line in {"好的，我们开始。", "好的", "开始。", "---"}:
            continue
        return line[:240]
    return ""


def _strip_conclusion_preamble(text: str) -> str:
    lines = str(text or "").splitlines()
    while lines:
        line = lines[0].strip()
        if not line or line in {"好的，我们开始。", "好的", "---"}:
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip() or str(text or "").strip()


def _strip_research_record_meta(content: str) -> str:
    text = str(content or "")
    if "<!-- research_record_meta" not in text:
        return text
    return re.sub(r"^<!-- research_record_meta.*?-->\s*\n?", "", text, flags=re.DOTALL | re.MULTILINE)


def _remove_markdown_sections(content: str, sections: set[str]) -> str:
    lines = str(content or "").splitlines()
    kept: list[str] = []
    skip_level: int | None = None
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            title = match.group(2).strip()
            level = len(match.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if title in sections:
                skip_level = level
                continue
        if skip_level is None:
            kept.append(line)
    return "\n".join(kept).strip()


def _ensure_record_head(text: str, gaps: list[str], quality: dict[str, Any]) -> str:
    body = str(text or "").strip() or "暂无正文。"
    if re.search(r"^##\s+研究摘要\s*$", body, flags=re.MULTILINE) and re.search(r"^##\s+待验证判断\s*$", body, flags=re.MULTILINE):
        return body
    summary = _first_meaningful_line(body) or "本次研究未形成明确摘要。"
    if quality.get("level") == "needs_more_work":
        summary = f"本次研究未完全达标。{summary}"
    pending = "\n".join(f"- {item}" for item in gaps[:8]) if gaps else "暂无。"
    return (
        "## 研究摘要\n"
        f"{summary}\n\n"
        "## 待验证判断\n"
        f"{pending}\n\n"
        "## 正文\n"
        f"{body}"
    ).strip()


def _draft_quality_summary(last_validation: dict[str, Any] | None = None) -> dict[str, Any]:
    validator = last_validation or {}
    status = str(validator.get("status") or "").lower()
    confidence = str(validator.get("confidence") or "").lower()
    if status == "pass":
        score = 0.9 if confidence == "high" else 0.8 if confidence == "medium" else 0.72
        level = "solid"
        guidance = "研究稿已通过独立审稿，可作为阶段性研究档案。"
    elif status == "fail":
        score = 0.45 if confidence == "high" else 0.55 if confidence == "medium" else 0.6
        level = "needs_more_work"
        guidance = "研究稿未通过独立审稿，应重点阅读待验证判断和审稿反馈。"
    else:
        score = 0.5
        level = "draft"
        guidance = "研究稿尚未形成明确审稿结论。"
    return {
        "score": score,
        "level": level,
        "guidance": guidance,
        "validator_status": status or "unknown",
        "validator_confidence": confidence or "unknown",
    }


def _normalize_subject_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _query_match_tokens(value: str) -> list[str]:
    text = _normalize_subject_key(value)
    if not text:
        return []
    tokens = {text}
    for match in re.findall(r"\d{6}(?:\.(?:sh|sz|bj))?", text):
        tokens.add(match)
        tokens.add(match.split(".")[0])
    for match in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.add(match)
    for match in re.findall(r"[a-z]{3,}", text):
        tokens.add(match)
    return [token for token in tokens if token]


def _age_days(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text[: len(fmt)], fmt).date()
            return (date.today() - parsed).days
        except Exception:
            continue
    match = re.search(r"(\d{4})[-_/年]?(\d{1,2})[-_/月]?(\d{1,2})", text)
    if not match:
        return None
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return (date.today() - parsed).days
    except Exception:
        return None


def _extract_markdown_section(content: str, section: str) -> str:
    lines = content.splitlines()
    start = None
    level = 2
    for idx, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and section in match.group(2):
            start = idx + 1
            level = len(match.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[idx])
        if match and len(match.group(1)) <= level:
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def _append_markdown_section(existing: str, section: str, new_content: str, max_chars: int) -> str:
    """Append a record section while keeping the newest bounded history."""
    previous = _extract_markdown_section(existing, section) if existing else ""
    previous = "" if previous.strip() in {"暂无", "暂无。"} else previous.strip()
    new_content = str(new_content or "").strip()
    if previous and new_content:
        combined = f"{previous}\n{new_content}"
    else:
        combined = previous or new_content or "暂无。"
    max_chars = max(4000, int(max_chars or 12000))
    if len(combined) <= max_chars:
        return combined
    tail = combined[-max_chars:]
    newline = tail.find("\n")
    if newline >= 0:
        tail = tail[newline + 1 :]
    return "- ...（前序记录已自动裁剪，仅保留最近历史）\n" + tail.strip()


def _page_text(content: str, max_chars: int = 12000, offset: int = 0) -> dict[str, Any]:
    offset = max(0, int(offset or 0))
    max_chars = max(1000, min(int(max_chars or 12000), 12000))
    end = min(len(content), offset + max_chars)
    return {
        "offset": offset,
        "max_chars": max_chars,
        "returned_chars": end - offset,
        "total_chars": len(content),
        "has_more": end < len(content),
        "next_offset": end if end < len(content) else None,
        "content": content[offset:end],
        "message": "该 section 还有后续内容，请用 next_offset 续读。" if end < len(content) else "该 section 已全部读取完成。",
    }


def _match_value(content: str, label: str) -> str | None:
    pattern = rf"^(?:-+\s*)?{re.escape(label)}\s*:\s*(.+)$"
    for line in content.splitlines():
        match = re.match(pattern, line.strip())
        if match:
            return match.group(1).strip()
    return None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _default_deep_research_agent_prompt() -> str:
    return (
        "You are a professional investment research agent. Return strict JSON only. "
        "Use available_skills with progressive disclosure. Activate a skill before complex tool workflows. "
        "Choose actions only from tool_policy.allowed_tools or internal activate_skill to advance the research_goal. "
        "Use tool_policy.limits and tool_policy.usage for budgets; do not expect top-level budget, iteration, or max_iterations. "
        "Follow research_strategy as the only research-method context; it is not a tool plan. "
        "Improve current_draft using current_tool_results and explain analysis_delta. "
        "Schema: focus, analysis_delta[], actions[{tool,arguments,reason,validates_against}], draft, strategy_update, submit_draft, should_stop."
    )


def _default_deep_research_validator_prompt() -> str:
    return (
        "You are an isolated investment research reviewer. Return strict JSON only. "
        "Judge only whether round_draft satisfies research_goal and research_strategy. Do not inspect tool results, continue research, or suggest tool calls. "
        "Schema: status(pass|fail), confidence(low|medium|high), analysis_quality, playbook_alignment, reason, missing_analysis[], overclaims[], strategy_patch{}."
    )


def _markdown_sections(content: str) -> list[dict[str, Any]]:
    matches: list[tuple[int, int, str]] = []
    for match in re.finditer(r"^(#{2,4})\s+(.+?)\s*$", content, flags=re.MULTILINE):
        matches.append((match.start(), len(match.group(1)), match.group(2).strip()))
    sections: list[dict[str, Any]] = []
    for idx, (start, level, title) in enumerate(matches):
        end = len(content)
        for next_start, next_level, _ in matches[idx + 1 :]:
            if next_level <= level:
                end = next_start
                break
        body_start = content.find("\n", start)
        body_start = body_start + 1 if body_start >= 0 else start
        sections.append({
            "section": title,
            "level": level,
            "chars": max(0, end - body_start),
        })
    return sections


def _public_record_sections(content: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _markdown_sections(content):
        title = str(item.get("section") or "").strip()
        if not title or title in _INTERNAL_RECORD_SECTIONS or title in seen:
            continue
        seen.add(title)
        sections.append(item)
    return sections


research_thread_service = ResearchThreadService()
