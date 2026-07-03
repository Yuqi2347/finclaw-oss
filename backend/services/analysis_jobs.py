from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.core.config import BETTAFISH_ROOT, CAPABILITY_RUNTIME_DIR, DATAHUB_BASE_URL, DEFAULT_PYTHON, PROJECT_ROOT, TRADINGAGENTS_ROOT
from backend.core.env import settings
from backend.core.subprocess_utils import safe_popen, safe_subprocess_env


TRADINGAGENTS_STAGE_ORDER = [
    ("market", "技术分析"),
    ("social", "情绪分析"),
    ("news", "新闻舆情"),
    ("fundamentals", "基本面"),
    ("policy", "政策分析"),
    ("quality_gate", "质量门控"),
    ("debate", "多空辩论"),
    ("trader", "交易决策"),
    ("risk", "风控评估"),
    ("pm", "最终决策"),
]


MARKET_DISCOVERY_STAGE_ORDER = [
    ("mindspider", "MindSpider"),
    ("research", "Query/Media/Insight 并行研究"),
    ("forum", "ForumEngine"),
    ("structure", "结构化汇总"),
    ("report", "ReportEngine"),
]


def _format_tradingagents_failure(event: dict[str, Any]) -> str:
    message = str(event.get("error") or "个股深研失败")
    error_type = str(event.get("error_type") or "").strip()
    error_repr = str(event.get("error_repr") or "").strip()
    failed_stage = str(event.get("failed_stage") or "").strip()
    completed = event.get("completed_stages") or []
    traceback_text = str(event.get("traceback") or "").strip()

    parts = [message]
    if error_type:
        parts.append(f"type={error_type}")
    if error_repr and error_repr != message:
        parts.append(f"repr={error_repr}")
    if failed_stage:
        parts.append(f"stage={failed_stage}")
    if completed:
        parts.append(f"completed_stages={','.join(str(item) for item in completed)}")
    if traceback_text:
        trace_lines = traceback_text.splitlines()
        trace_tail = "\n".join(trace_lines[-40:])
        parts.append(f"traceback_tail:\n{trace_tail}")
    return "\n".join(parts)


class AnalysisJob(BaseModel):
    job_id: str
    job_type: str
    status: str = "running"
    current_stage: str = "queued"
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: str = "default"
    origin_run_id: str | None = None
    continuation_status: str = "none"
    stages: list[dict[str, str]] = Field(default_factory=list)
    progress_log: list[str] = Field(default_factory=list)
    output_report_id: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class AnalysisJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, AnalysisJob] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def create_market_discovery(
        self,
        no_resume: bool = False,
        session_id: str = "default",
        origin_run_id: str | None = None,
    ) -> AnalysisJob:
        return self._create("market_discovery", {"no_resume": no_resume}, session_id, origin_run_id)

    def create_stock_research(
        self,
        ticker: str,
        trade_date: str | None = None,
        session_id: str = "default",
        origin_run_id: str | None = None,
    ) -> AnalysisJob:
        selected_date = trade_date or date.today().isoformat()
        return self._create("stock_research", {"ticker": ticker, "trade_date": selected_date}, session_id, origin_run_id)

    def list_jobs(self, limit: int = 20) -> list[AnalysisJob]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            for job in jobs:
                self._normalize_job_stages(job)
            return jobs[:limit]

    def get(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._normalize_job_stages(job)
            return job

    def cancel(self, job_id: str) -> AnalysisJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in {"running", "cancelling"}:
                return job
            job.status = "cancelling"
            job.current_stage = "cancelling"
            job.progress_log = (job.progress_log + ["用户请求取消后台分析任务"])[-80:]
            job.updated_at = datetime.now().isoformat(timespec="seconds")
            proc = self._processes.get(job_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            threading.Thread(target=self._finalize_cancelled_process, args=(job_id, proc), daemon=True).start()
        else:
            self._mark_cancelled(job_id, "后台分析任务已取消")
        return self.get(job_id) or job

    def _finalize_cancelled_process(self, job_id: str, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=8)
        self._mark_cancelled(job_id, "后台分析任务已取消")

    def _mark_cancelled(self, job_id: str, progress: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "completed":
                return
            job.status = "cancelled"
            job.current_stage = "cancelled"
            job.progress_log = (job.progress_log + [progress])[-80:]
            job.completed_at = datetime.now().isoformat(timespec="seconds")
            job.updated_at = datetime.now().isoformat(timespec="seconds")
            self._processes.pop(job_id, None)
        self._publish_completion_event(job_id, "cancelled")

    def query_jobs(
        self,
        job_type: str | None = None,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized = ticker.upper() if ticker else None
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            rows: list[dict[str, Any]] = []
            for job in jobs:
                if job_type and job.job_type != job_type:
                    continue
                if status and job.status != status:
                    continue
                if normalized and str(job.args.get("ticker", "")).upper() != normalized:
                    continue
                self._normalize_job_stages(job)
                data = job.model_dump()
                data["latest_progress"] = job.progress_log[-1] if job.progress_log else None
                rows.append(data)
                if len(rows) >= limit:
                    break
            return rows

    def find_latest_stock_research(
        self,
        ticker: str,
        trade_date: str | None = None,
        session_id: str | None = None,
    ) -> AnalysisJob | None:
        normalized_ticker = ticker.upper()
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            for job in jobs:
                if job.job_type != "stock_research":
                    continue
                if str(job.args.get("ticker", "")).upper() != normalized_ticker:
                    continue
                if trade_date and str(job.args.get("trade_date", "")) != trade_date:
                    continue
                if session_id and job.session_id != session_id:
                    continue
                self._normalize_job_stages(job)
                return job
        return None

    def _create(
        self,
        job_type: str,
        args: dict[str, Any],
        session_id: str = "default",
        origin_run_id: str | None = None,
    ) -> AnalysisJob:
        now = datetime.now().isoformat(timespec="seconds")
        job = AnalysisJob(
            job_id=str(uuid4()),
            job_type=job_type,
            args=args,
            session_id=session_id,
            origin_run_id=origin_run_id,
            current_stage="starting",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        thread = threading.Thread(target=self._run, args=(job.job_id,), daemon=True)
        thread.start()
        return job

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        try:
            if job.job_type == "market_discovery":
                self._run_market_discovery(job)
            elif job.job_type == "stock_research":
                self._run_stock_research(job)
            else:
                raise ValueError(f"unknown analysis job type: {job.job_type}")
        except Exception as exc:
            if self._is_cancelled(job_id):
                self._mark_cancelled(job_id, "后台分析任务已取消")
                return
            self._update(
                job_id,
                status="failed",
                current_stage="failed",
                progress=f"失败：{exc}",
                error=str(exc),
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._publish_completion_event(job_id, "failed")
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

    def _run_market_discovery(self, job: AnalysisJob) -> None:
        cmd = [DEFAULT_PYTHON, "-m", "BettaFishFin.skill_runner", "market-discovery"]
        if job.args.get("no_resume"):
            cmd.append("--no-resume")
        self._run_market_discovery_process(job.job_id, cmd, str(BETTAFISH_ROOT))
        today = date.today().isoformat()
        self._update(job.job_id, output_report_id=f"market_discovery:A股全市场:{today}")
        self._publish_completion_event(job.job_id, "completed")

    def _run_stock_research(self, job: AnalysisJob) -> None:
        ticker = str(job.args["ticker"])
        trade_date = str(job.args["trade_date"])
        self._run_tradingagents_process(job.job_id, ticker, trade_date)
        self._update(job.job_id, output_report_id=f"stock_research:{ticker}:{trade_date}")
        self._publish_completion_event(job.job_id, "completed")

    def _run_tradingagents_process(self, job_id: str, ticker: str, trade_date: str) -> None:
        self._update(
            job_id,
            status="running",
            current_stage="market",
            progress=f"启动个股深研：{ticker} {trade_date}",
            stages=self._build_stage_progress("market", set()),
        )
        completed_seen: set[str] = set()
        cmd = [
            DEFAULT_PYTHON,
            "-m",
            "backend.adapters.tradingagents_runner",
            "--root",
            str(TRADINGAGENTS_ROOT),
            "--ticker",
            ticker,
            "--trade-date",
            trade_date,
        ]
        proc = safe_popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=self._tradingagents_env(),
        )
        self._register_process(job_id, proc)
        assert proc.stdout is not None
        last_error = ""
        for line in proc.stdout:
            clean = line.strip()
            if not clean:
                continue
            if "LangChainPendingDeprecationWarning" in clean or "langgraph\\checkpoint" in clean or "langgraph/checkpoint" in clean:
                continue
            try:
                event = json.loads(clean)
            except Exception:
                last_error = clean
                self._update(job_id, progress=clean)
                continue
            event_type = event.get("event")
            if event_type == "stage_done":
                stage_id = str(event.get("stage_id", ""))
                if stage_id:
                    completed_seen.add(stage_id)
                self._update(job_id, progress=f"完成：{event.get('stage_name', stage_id)}")
            elif event_type == "progress":
                current = str(event.get("current_stage") or "running")
                completed_seen.update(str(item) for item in event.get("completed_stages", []))
                self._update(job_id, current_stage=current, stages=self._build_stage_progress(current, completed_seen))
            elif event_type == "failed":
                last_error = _format_tradingagents_failure(event)
                self._update(job_id, progress=f"失败：{last_error}")
            elif event_type == "completed":
                completed_seen.update(str(item) for item in event.get("completed_stages", []))
                self._update(job_id, current_stage="completed", stages=self._build_stage_progress("completed", completed_seen))
            elif event_type == "started":
                self._update(job_id, current_stage=str(event.get("current_stage") or "market"))
        returncode = proc.wait(timeout=30)
        if self._is_cancelled(job_id) or returncode < 0:
            raise RuntimeError("analysis job cancelled")
        if returncode != 0:
            raise RuntimeError(last_error or f"个股深研进程失败，returncode={returncode}")
        self._update(
            job_id,
            status="completed",
            current_stage="completed",
            progress="个股深研完成",
            stages=self._build_stage_progress("completed", {stage_id for stage_id, _ in TRADINGAGENTS_STAGE_ORDER}),
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _run_market_discovery_process(self, job_id: str, cmd: list[str], cwd: str) -> None:
        current = "mindspider"
        completed_seen: set[str] = set()
        self._update(
            job_id,
            status="running",
            current_stage=current,
            progress=f"启动主线雷达：{' '.join(cmd)}",
            stages=self._build_named_stage_progress(MARKET_DISCOVERY_STAGE_ORDER, current, completed_seen),
        )
        proc = safe_popen(
            cmd,
            cwd=cwd,
        )
        self._register_process(job_id, proc)
        assert proc.stdout is not None
        last_error = ""
        for line in proc.stdout:
            clean = line.strip()
            if not clean:
                continue
            last_error = clean
            next_stage = self._infer_market_stage(clean, current)
            if next_stage != current:
                completed_seen.update(self._previous_market_stages(next_stage))
                current = next_stage
            if "BettaFish-Fin skill finished" in clean or "报告生成完成" in clean:
                if current == "report":
                    completed_seen.add("report")
            self._update(
                job_id,
                current_stage=current,
                progress=clean,
                stages=self._build_named_stage_progress(MARKET_DISCOVERY_STAGE_ORDER, current, completed_seen),
            )
        returncode = proc.wait(timeout=30)
        if self._is_cancelled(job_id) or returncode < 0:
            raise RuntimeError("analysis job cancelled")
        if returncode != 0:
            raise RuntimeError(last_error or f"analysis process failed with returncode={returncode}")
        all_done = {stage_id for stage_id, _ in MARKET_DISCOVERY_STAGE_ORDER}
        self._update(
            job_id,
            status="completed",
            current_stage="completed",
            progress="主线雷达完成",
            stages=self._build_named_stage_progress(MARKET_DISCOVERY_STAGE_ORDER, "completed", all_done),
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _infer_market_stage(self, line: str, current_stage: str) -> str:
        lowered = line.lower()
        if "[bettafishfin][stage:mindspider]" in lowered:
            return "mindspider"
        if "[bettafishfin][stage:research]" in lowered:
            return "research"
        if "[bettafishfin][stage:query]" in lowered:
            return "research"
        if "[bettafishfin][stage:media]" in lowered:
            return "research"
        if "[bettafishfin][stage:insight]" in lowered:
            return "research"
        if "[bettafishfin][stage:forum]" in lowered:
            return "forum"
        if "[bettafishfin][stage:structure]" in lowered:
            return "structure"
        if "[bettafishfin][stage:report]" in lowered:
            return "report"
        if "reportengine" in lowered or "开始生成报告" in line or "生成章节" in line or "html报告已保存" in line:
            return "report"
        if "结构化" in line or "structured" in lowered or "structure_" in lowered:
            return "structure"
        if "forumengine" in lowered or "[HOST]" in line or "论坛主持人" in line:
            return "forum"
        if "insightengine" in lowered or "insight agent" in lowered or "insight agent已初始化" in line or "insight agent已初始化" in lowered:
            return "research"
        if "mediaengine" in lowered or "Media Agent" in line:
            return "research"
        if "queryengine" in lowered or "Query Agent" in line:
            return "research"
        if "mindspider" in lowered or "broadtopicextraction" in lowered or "deepsentimentcrawling" in lowered:
            return "mindspider"
        if "Engine status:" in line or "ReportEngine:" in line:
            return "report"
        return current_stage

    def _previous_market_stages(self, stage_id: str) -> set[str]:
        return self._previous_named_stages(MARKET_DISCOVERY_STAGE_ORDER, stage_id)

    def _previous_named_stages(self, stage_order: list[tuple[str, str]], stage_id: str) -> set[str]:
        order = [sid for sid, _ in stage_order]
        if stage_id not in order:
            return set()
        return set(order[: order.index(stage_id)])

    def _stage_order_for_job(self, job_type: str) -> list[tuple[str, str]]:
        if job_type == "market_discovery":
            return MARKET_DISCOVERY_STAGE_ORDER
        if job_type == "stock_research":
            return TRADINGAGENTS_STAGE_ORDER
        return []

    def _utf8_env(self) -> dict[str, str]:
        return safe_subprocess_env()

    def _tradingagents_env(self) -> dict[str, str]:
        equityscope_runtime = CAPABILITY_RUNTIME_DIR / "equityscope"
        env = {
            "TRADINGAGENTS_DATAHUB_URL": DATAHUB_BASE_URL,
            "FINDATAHUB_API_BASE": DATAHUB_BASE_URL,
            "TRADINGAGENTS_RESULTS_DIR": str(equityscope_runtime / "logs"),
            "TRADINGAGENTS_CACHE_DIR": str(equityscope_runtime / "cache"),
            "TRADINGAGENTS_MEMORY_LOG_PATH": str(equityscope_runtime / "memory" / "trading_memory.md"),
        }
        finclaw_env = {
            "FINCLAW_LLM_API_KEY": settings.llm_api_key,
            "FINCLAW_LLM_BASE_URL": settings.llm_base_url,
            "FINCLAW_LLM_MODEL": settings.llm_model,
            "FINCLAW_LLM_TEMPERATURE": str(settings.llm_temperature),
            "FINCLAW_LLM_TIMEOUT": str(settings.llm_timeout),
            "FINCLAW_LLM_THINKING": settings.llm_thinking,
        }
        env.update({key: value for key, value in finclaw_env.items() if value})
        self._sync_provider_key(env)
        return env

    def _sync_provider_key(self, env: dict[str, str]) -> None:
        api_key = settings.llm_api_key
        if not api_key:
            return
        # Capabilities use FinClaw's root LLM config as the source of truth.
        # Populate common provider env vars as compatibility aliases because
        # upstream capability code may still read provider-specific names.
        env.setdefault("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY") or api_key)
        provider = os.getenv("FINCLAW_LLM_PROVIDER", "").strip().lower() or self._infer_llm_provider(settings.llm_base_url, settings.llm_model)
        provider_env_names = {
            "openai": ("OPENAI_API_KEY",),
            "minimax": ("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY"),
            "deepseek": ("DEEPSEEK_API_KEY",),
            "qwen": ("DASHSCOPE_API_KEY", "DASHSCOPE_CN_API_KEY"),
            "glm": ("ZHIPU_API_KEY", "ZHIPU_CN_API_KEY"),
            "openrouter": ("OPENROUTER_API_KEY",),
            "xai": ("XAI_API_KEY",),
        }.get(provider, ())
        for name in provider_env_names:
            env[name] = os.getenv(name) or api_key

    def _infer_llm_provider(self, base_url: str, model: str) -> str:
        value = f"{base_url} {model}".lower()
        if "minimax" in value or "mimo" in value:
            return "minimax"
        if "deepseek" in value:
            return "deepseek"
        if "dashscope" in value or "qwen" in value:
            return "qwen"
        if "z.ai" in value or "zhipu" in value or "glm" in value:
            return "glm"
        if "openrouter" in value:
            return "openrouter"
        if "x.ai" in value or "grok" in value:
            return "xai"
        return "openai"

    def _build_stage_progress(self, current_stage: str, completed: set[str]) -> list[dict[str, str]]:
        return self._build_named_stage_progress(TRADINGAGENTS_STAGE_ORDER, current_stage, completed)

    def _build_named_stage_progress(
        self,
        stage_order: list[tuple[str, str]],
        current_stage: str,
        completed: set[str],
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for stage_id, name in stage_order:
            if stage_id in completed or current_stage == "completed":
                status = "done"
            elif stage_id == current_stage:
                status = "active"
            else:
                status = "pending"
            rows.append({"id": stage_id, "name": name, "status": status})
        return rows

    def _normalize_job_stages(self, job: AnalysisJob) -> None:
        stage_order = self._stage_order_for_job(job.job_type)
        if not stage_order:
            return

        canonical_stage_ids = [stage_id for stage_id, _ in stage_order]
        existing_rows = [row for row in (job.stages or []) if isinstance(row, dict)]
        completed = {
            str(row.get("id"))
            for row in existing_rows
            if row.get("status") == "done" and str(row.get("id")) in canonical_stage_ids
        }
        active_from_rows = next(
            (
                str(row.get("id"))
                for row in existing_rows
                if row.get("status") == "active" and str(row.get("id")) in canonical_stage_ids
            ),
            None,
        )

        current_stage = job.current_stage
        if current_stage not in canonical_stage_ids:
            current_stage = active_from_rows or current_stage

        if job.status == "completed":
            completed.update(canonical_stage_ids)
            current_stage = "completed"
        elif current_stage in canonical_stage_ids:
            completed.update(self._previous_named_stages(stage_order, current_stage))

        job.stages = self._build_named_stage_progress(stage_order, current_stage, completed)

    def _update(
        self,
        job_id: str,
        status: str | None = None,
        current_stage: str | None = None,
        progress: str | None = None,
        stages: list[dict[str, str]] | None = None,
        output_report_id: str | None = None,
        error: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if status:
                job.status = status
            if current_stage:
                job.current_stage = current_stage
            if progress:
                job.progress_log = (job.progress_log + [progress])[-80:]
            if stages is not None:
                job.stages = stages
            if output_report_id:
                job.output_report_id = output_report_id
            if error:
                job.error = error
            if completed_at:
                job.completed_at = completed_at
            self._normalize_job_stages(job)
            job.updated_at = datetime.now().isoformat(timespec="seconds")

    def _register_process(self, job_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes[job_id] = proc

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.status in {"cancelling", "cancelled"})

    def _publish_completion_event(self, job_id: str, status: str) -> None:
        from backend.services.continuation import continuation_service
        from backend.services.sessions import chat_session_store

        job = self.get(job_id)
        if job is None or not job.session_id:
            return
        if status in {"failed", "cancelled"}:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current.continuation_status = "status_panel_only"
            return
        payload = job.model_dump()
        payload["completion_status"] = status
        with self._lock:
            current = self._jobs.get(job_id)
            if current:
                current.continuation_status = "queued"
        chat_session_store.add_event(
            job.session_id,
            f"analysis_job.{status}",
            payload,
            priority=50,
        )
        continuation_service.kick()


analysis_job_store = AnalysisJobStore()
