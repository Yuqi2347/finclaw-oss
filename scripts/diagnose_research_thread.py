from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import research_threads as research_threads_module  # noqa: E402


research_thread_service = research_threads_module.research_thread_service


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_MAX_WALL_MS = 10 * 60 * 1000


class DiagnosticStore:
    def __init__(self, use_live_data: bool) -> None:
        self.use_live_data = use_live_data
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.old_records_dir: Path | None = None
        self.old_service: Any = None

    def __enter__(self) -> "DiagnosticStore":
        global research_thread_service
        self.old_records_dir = research_threads_module.RECORDS_DIR
        self.old_service = research_thread_service
        if self.use_live_data:
            research_thread_service = research_threads_module.research_thread_service
            return self
        self.temp_dir = tempfile.TemporaryDirectory(prefix="finclaw_research_diag_", ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        research_threads_module.RECORDS_DIR = root / "research_records"
        research_thread_service = research_threads_module.ResearchThreadService(root / "research_threads.sqlite")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        global research_thread_service
        if self.old_records_dir is not None:
            research_threads_module.RECORDS_DIR = self.old_records_dir
        if self.old_service is not None:
            research_thread_service = self.old_service
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def summarize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    plan = [step for step in (thread.get("plan") or []) if isinstance(step, dict)]
    evidence = [item for item in (thread.get("evidence") or []) if isinstance(item, dict)]
    metrics = thread.get("metrics") if isinstance(thread.get("metrics"), dict) else {}
    quality = metrics.get("quality") if isinstance(metrics.get("quality"), dict) else {}
    budget = metrics.get("budget") if isinstance(metrics.get("budget"), dict) else {}

    tool_counts: dict[str, int] = {}
    duplicate_tool_types: dict[str, int] = {}
    for step in plan:
        tool_type = str(step.get("tool_type") or "")
        if not tool_type:
            continue
        tool_counts[tool_type] = tool_counts.get(tool_type, 0) + 1
    for tool_type, count in tool_counts.items():
        if count > 1:
            duplicate_tool_types[tool_type] = count

    source_counts: dict[str, int] = {}
    for item in evidence:
        source_type = str(item.get("source_type") or "unknown")
        source_counts[source_type] = source_counts.get(source_type, 0) + 1

    slowest_steps = metrics.get("slowest_steps") if isinstance(metrics.get("slowest_steps"), list) else []
    web_runs = metrics.get("web_runs") if isinstance(metrics.get("web_runs"), list) else []
    records = research_thread_service.list_records(
        subject_type=str(thread.get("subject_type") or "") or None,
        query=str(thread.get("subject") or "") or None,
        limit=5,
    ).get("records") or []

    failed_steps = [
        {
            "step_id": step.get("step_id"),
            "tool_type": step.get("tool_type"),
            "conclusion": step.get("conclusion"),
        }
        for step in plan
        if step.get("status") == "failed"
    ]

    summary = {
        "thread_id": thread.get("thread_id"),
        "subject": thread.get("subject"),
        "subject_type": thread.get("subject_type"),
        "depth": thread.get("depth"),
        "status": thread.get("status"),
        "error": thread.get("error"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "completed_at": thread.get("completed_at"),
        "quality": quality,
        "wall_elapsed_ms": metrics.get("wall_elapsed_ms"),
        "total_step_elapsed_ms": metrics.get("total_step_elapsed_ms"),
        "budget": budget,
        "plan_total": len(plan),
        "evidence_total": len(evidence),
        "source_counts": source_counts,
        "duplicate_tool_types": duplicate_tool_types,
        "failed_steps": failed_steps,
        "gap_total": len(thread.get("gaps") or []),
        "claim_total": len(thread.get("claim_validation") or []),
        "recommended_action_total": len(thread.get("recommended_actions") or []),
        "recommended_actions": [
            {
                "tool": item.get("tool"),
                "reason": item.get("reason"),
            }
            for item in (thread.get("recommended_actions") or [])[:8]
            if isinstance(item, dict)
        ],
        "slowest_steps": slowest_steps[:5],
        "web_runs": web_runs[:5],
        "current_conclusion": thread.get("current_conclusion"),
        "matching_records": [
            {
                "record_id": record.get("record_id"),
                "title": record.get("title"),
                "quality_level": record.get("quality_level"),
                "gap_count": record.get("gap_count"),
                "updated_at": record.get("updated_at"),
                "match_score": record.get("match_score"),
            }
            for record in records[:5]
            if isinstance(record, dict)
        ],
    }
    summary["warnings"] = evaluate_summary(summary)
    return summary


def evaluate_summary(summary: dict[str, Any], max_wall_ms: int = DEFAULT_MAX_WALL_MS) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    status = str(summary.get("status") or "")
    quality = summary.get("quality") if isinstance(summary.get("quality"), dict) else {}
    budget = summary.get("budget") if isinstance(summary.get("budget"), dict) else {}

    if status not in TERMINAL_STATUSES:
        warnings.append({"code": "not_terminal", "severity": "error", "message": f"研究线程尚未结束：{status}"})
    if status == "failed":
        warnings.append({"code": "thread_failed", "severity": "error", "message": str(summary.get("error") or "研究线程失败")})
    if summary.get("duplicate_tool_types"):
        warnings.append({
            "code": "duplicate_tool_types",
            "severity": "warning",
            "message": f"存在重复动态工具步骤：{summary.get('duplicate_tool_types')}",
        })
    if summary.get("failed_steps"):
        warnings.append({
            "code": "failed_steps",
            "severity": "error",
            "message": f"存在失败步骤：{summary.get('failed_steps')}",
        })
    if quality.get("level") in {"thin", "conflicted"}:
        warnings.append({
            "code": f"quality_{quality.get('level')}",
            "severity": "warning" if quality.get("level") == "thin" else "error",
            "message": str(quality.get("guidance") or "研究质量不足"),
        })
    depth = str(summary.get("depth") or "")
    source_counts = summary.get("source_counts") if isinstance(summary.get("source_counts"), dict) else {}
    if depth in {"standard", "deep"} and source_counts.get("web_search", 0) <= 0:
        warnings.append({
            "code": "missing_web_verification",
            "severity": "warning",
            "message": f"{depth} 研究未产生 web_search 证据；可能是联网关闭、无来源或动态计划未包含验证步骤。",
        })
    if depth == "deep" and _as_int(summary.get("recommended_action_total")) == 0:
        warnings.append({
            "code": "missing_deep_recommended_actions",
            "severity": "warning",
            "message": "deep 研究未生成高成本后续动作建议；请确认是否已充分利用 TradingAgents/BettaFish/TradingGraph 的确认式流程。",
        })
    web_runs = summary.get("web_runs") if isinstance(summary.get("web_runs"), list) else []
    for index, run in enumerate(web_runs[:5], start=1):
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "") not in {"", "ok"}:
            warnings.append({
                "code": "web_run_not_ok",
                "severity": "warning",
                "message": f"第 {index} 轮联网验证状态为 {run.get('status')}，原因 {run.get('stopped_reason') or 'unknown'}。",
            })
        query_count = _as_int(run.get("query_count"))
        source_budget = _as_int(run.get("total_source_budget"))
        if query_count is not None and query_count > 4:
            warnings.append({
                "code": "web_query_count_exceeded",
                "severity": "error",
                "message": f"第 {index} 轮联网 query_count={query_count} 超过并行查询上限 4。",
            })
        if source_budget is not None and source_budget > 8:
            warnings.append({
                "code": "web_source_budget_exceeded",
                "severity": "error",
                "message": f"第 {index} 轮联网 source_budget={source_budget} 超过上限 8。",
            })
    wall_ms = _as_int(summary.get("wall_elapsed_ms"))
    if wall_ms is not None and wall_ms > max_wall_ms:
        warnings.append({
            "code": "wall_time_exceeded",
            "severity": "warning",
            "message": f"耗时 {wall_ms}ms 超过阈值 {max_wall_ms}ms",
        })
    for key in ("web_batches", "report_queries", "graph_reads"):
        used = _as_int(budget.get(key))
        maximum = _as_int(budget.get(f"max_{key}"))
        if used is not None and maximum is not None and used > maximum:
            warnings.append({
                "code": f"budget_exceeded_{key}",
                "severity": "error",
                "message": f"{key}={used} 超过上限 {maximum}",
            })
    return warnings


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def wait_for_thread(thread_id: str, timeout: float, poll: float) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout)
    while True:
        thread = research_thread_service.get_thread(thread_id)
        if str(thread.get("status")) in TERMINAL_STATUSES:
            return thread
        if time.time() >= deadline:
            return thread
        time.sleep(max(0.2, poll))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnose a FinClaw research thread and print a bounded JSON summary.")
    parser.add_argument("subject", nargs="?", help="研究对象，例如 002281.SZ、光迅科技、CPO/光模块。")
    parser.add_argument("--thread-id", default="", help="已有研究线程 ID；传入后不创建新线程。")
    parser.add_argument("--subject-type", default="unknown", choices=["stock", "mainline", "market", "comparison", "unknown"])
    parser.add_argument("--depth", default="quick", choices=["quick", "standard", "deep"])
    parser.add_argument("--user-goal", default="", help="用户原始研究目标。")
    parser.add_argument("--session-id", default="diagnostic", help="诊断会话 ID。")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待线程完成的秒数。")
    parser.add_argument("--poll", type=float, default=1.0, help="轮询间隔秒数。")
    parser.add_argument("--max-wall-ms", type=int, default=DEFAULT_MAX_WALL_MS, help="诊断耗时告警阈值，默认 10 分钟。")
    parser.add_argument("--fail-on-warnings", action="store_true", help="存在 warning/error 时返回非零退出码，用于压测门禁。")
    parser.add_argument("--no-wait", action="store_true", help="创建/读取后立即输出，不等待完成。")
    parser.add_argument("--use-live-data", action="store_true", help="默认使用临时隔离库；传此参数才读写正式研究线程和研究档案。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with DiagnosticStore(args.use_live_data):
        try:
            if args.thread_id:
                thread = research_thread_service.get_thread(args.thread_id)
            else:
                if not args.subject:
                    raise ValueError("subject is required unless --thread-id is provided")
                thread = research_thread_service.start_thread(
                    subject=args.subject,
                    subject_type=args.subject_type,
                    depth=args.depth,
                    session_id=args.session_id,
                    user_goal=args.user_goal,
                    auto_start=True,
                )
            if not args.no_wait:
                thread = wait_for_thread(str(thread.get("thread_id")), timeout=args.timeout, poll=args.poll)
            summary = summarize_thread(thread)
            summary["warnings"] = evaluate_summary(summary, max_wall_ms=args.max_wall_ms)
            summary["isolated"] = not args.use_live_data
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
            if args.fail_on_warnings and summary["warnings"]:
                return 2
            return 0
        except Exception as exc:
            print(json.dumps({
                "success": False,
                "error": str(exc),
                "thread_id": args.thread_id or None,
                "subject": args.subject or None,
                "isolated": not args.use_live_data,
            }, ensure_ascii=False, indent=2, default=str))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
