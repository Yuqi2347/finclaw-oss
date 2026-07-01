from __future__ import annotations

from pathlib import Path

from backend.services import research_threads as rt
from scripts import diagnose_research_thread as diag


def test_summarize_thread_reports_quality_duplicates_and_records(tmp_path, monkeypatch):
    records_dir = tmp_path / "records"
    monkeypatch.setattr(rt, "RECORDS_DIR", records_dir)
    monkeypatch.setattr(diag.research_thread_service, "list_records", lambda **_kwargs: {
        "records": [
            {
                "record_id": "mainlines/cpo",
                "title": "CPO",
                "quality_level": "usable",
                "gap_count": 1,
                "updated_at": "2026-06-12T12:00:00",
                "match_score": 0.8,
            }
        ]
    })
    thread = {
        "thread_id": "rt_demo",
        "subject": "CPO/光模块",
        "subject_type": "mainline",
        "depth": "standard",
        "status": "completed",
        "plan": [
            {"step_id": "a", "tool_type": "web"},
            {"step_id": "b", "tool_type": "web"},
            {"step_id": "c", "tool_type": "graph"},
        ],
        "evidence": [
            rt._evidence("web_search", "batch", "联网", "summary", 0.8),
            rt._evidence("tradinggraph", "CPO", "图谱", "summary", 0.8),
        ],
        "gaps": ["gap"],
        "claim_validation": [{"status": "supported"}],
        "recommended_actions": [{"tool": "control_industry_graph"}],
        "metrics": {
            "quality": {"level": "usable", "score": 0.78},
            "wall_elapsed_ms": 1200,
            "budget": {"web_batches": 1},
            "slowest_steps": [{"step_id": "a", "elapsed_ms": 800}],
            "web_runs": [{
                "batch": 1,
                "query_count": 2,
                "total_source_budget": 5,
                "status": "ok",
                "elapsed_ms": 500,
            }],
        },
        "current_conclusion": "ok",
    }

    summary = diag.summarize_thread(thread)

    assert summary["quality"]["level"] == "usable"
    assert summary["duplicate_tool_types"] == {"web": 2}
    assert summary["warnings"][0]["code"] == "duplicate_tool_types"
    assert summary["source_counts"] == {"web_search": 1, "tradinggraph": 1}
    assert summary["web_runs"][0]["query_count"] == 2
    assert summary["web_runs"][0]["total_source_budget"] == 5
    assert summary["matching_records"][0]["record_id"] == "mainlines/cpo"


def test_evaluate_summary_reports_quality_time_budget_and_failure():
    warnings = diag.evaluate_summary({
        "status": "failed",
        "error": "boom",
        "quality": {"level": "conflicted", "guidance": "conflict"},
        "wall_elapsed_ms": 999,
        "budget": {"web_batches": 3, "max_web_batches": 2},
        "failed_steps": [{"step_id": "x"}],
    }, max_wall_ms=100)
    codes = {item["code"] for item in warnings}

    assert "thread_failed" in codes
    assert "quality_conflicted" in codes
    assert "wall_time_exceeded" in codes
    assert "budget_exceeded_web_batches" in codes
    assert "failed_steps" in codes


def test_evaluate_summary_reports_standard_missing_web_and_bad_web_run():
    warnings = diag.evaluate_summary({
        "status": "completed",
        "depth": "standard",
        "quality": {"level": "usable"},
        "source_counts": {},
        "budget": {},
        "web_runs": [{
            "status": "error",
            "stopped_reason": "total_timeout",
            "query_count": 5,
            "total_source_budget": 9,
        }],
    })
    codes = {item["code"] for item in warnings}

    assert "missing_web_verification" in codes
    assert "web_run_not_ok" in codes
    assert "web_query_count_exceeded" in codes
    assert "web_source_budget_exceeded" in codes


def test_evaluate_summary_reports_deep_missing_recommended_actions():
    warnings = diag.evaluate_summary({
        "status": "completed",
        "depth": "deep",
        "quality": {"level": "usable"},
        "source_counts": {"web_search": 1},
        "recommended_action_total": 0,
        "budget": {},
    })
    codes = {item["code"] for item in warnings}

    assert "missing_deep_recommended_actions" in codes
    assert "missing_web_verification" not in codes


def test_evaluate_summary_reports_non_terminal_thread():
    warnings = diag.evaluate_summary({
        "status": "in_progress",
        "quality": {"level": "usable"},
        "budget": {},
    })

    assert warnings[0]["code"] == "not_terminal"


def test_diagnostic_store_defaults_to_isolated_temp_store_and_restores_globals():
    old_records_dir = rt.RECORDS_DIR
    old_service = diag.research_thread_service

    with diag.DiagnosticStore(use_live_data=False) as store:
        assert store.temp_dir is not None
        assert rt.RECORDS_DIR != old_records_dir
        assert str(rt.RECORDS_DIR).startswith(store.temp_dir.name)
        assert diag.research_thread_service is not old_service

    assert rt.RECORDS_DIR == old_records_dir
    assert diag.research_thread_service is old_service
