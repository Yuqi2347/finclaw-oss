from __future__ import annotations

from pathlib import Path

from backend.services import research_threads as rt
from backend.tools import research as research_tools
from backend.tools.bootstrap import build_registry


def make_service(tmp_path: Path, monkeypatch) -> rt.ResearchThreadService:
    records_dir = tmp_path / "records"
    monkeypatch.setattr(rt, "RECORDS_DIR", records_dir)
    service = rt.ResearchThreadService(tmp_path / "threads.sqlite")
    service._read_playbook_context = lambda: ""
    service._discover_related_research = lambda subject, subject_type, ticker, depth="standard": []
    service._llm_plan = lambda state: None
    service.resume_thread = lambda thread_id: service._run_thread(thread_id)
    return service


def test_start_thread_reuses_active_thread_by_subject(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)

    first = service.start_thread("CPO/光模块", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    second = service.start_thread(" CPO/光模块 ", subject_type="unknown", depth="quick", session_id="s1", auto_start=False)

    assert second["reused_existing"] is True
    assert first["thread_id"] == second["thread_id"]


def test_start_thread_reuses_active_thread_by_ticker(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)

    first = service.start_thread("002281", subject_type="stock", depth="quick", session_id="s1", auto_start=False)
    second = service.start_thread("002281.SZ", subject_type="unknown", depth="quick", session_id="s1", auto_start=False)

    assert second["reused_existing"] is True
    assert first["thread_id"] == second["thread_id"]


def test_start_thread_reuses_recent_completed_thread_by_default(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)

    first = service.start_thread("CPO/光模块", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    service._patch_thread(first["thread_id"], status="completed", completed_at=rt._now())
    second = service.start_thread("CPO/光模块", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)

    assert second["reused_existing"] is True
    assert second["reuse_reason"] == "recent_completed_thread"
    assert second["thread_id"] == first["thread_id"]


def test_start_thread_force_new_skips_recent_completed_reuse(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)

    first = service.start_thread("CPO/光模块", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    service._patch_thread(first["thread_id"], status="completed", completed_at=rt._now())
    second = service.start_thread(
        "CPO/光模块",
        subject_type="mainline",
        depth="quick",
        session_id="s1",
        auto_start=False,
        force_new=True,
    )

    assert second.get("reused_existing") is not True
    assert second["thread_id"] != first["thread_id"]


def test_execute_step_skips_done_step(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", auto_start=False)
    state = rt.ResearchState(
        thread_id=thread["thread_id"],
        subject="测试主线",
        subject_type="mainline",
        depth="quick",
        plan=thread["plan"],
    )
    calls = {"count": 0}

    def step(_state):
        calls["count"] += 1
        _state.evidence.append(rt._evidence("test", "ref", "title", "summary", 0.9))
        return "ok"

    service._execute_step(state, "discover", step)
    service._execute_step(state, "discover", step)

    assert calls["count"] == 1
    assert len(state.evidence) == 1
    assert state.plan[0]["status"] == "done"


def test_completed_thread_has_metrics_and_record_sections(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread(
        "测试主线",
        subject_type="mainline",
        depth="quick",
        user_goal="验证这条主线是否仍然有研究价值",
        auto_start=False,
    )

    service._run_thread(thread["thread_id"])
    completed = service.get_thread(thread["thread_id"])
    records = service.list_records(subject_type="mainlines")["records"]

    assert completed["status"] == "completed"
    assert completed["metrics"]["wall_elapsed_ms"] >= 0
    assert completed["metrics"]["quality"]["level"] in {"thin", "partial", "usable", "conflicted"}
    assert len(completed["metrics"]["step_timings"]) == 5
    assert records
    assert any(section["section"] == "Claim 校验" for section in records[0]["sections"])
    assert any(section["section"] == "证据账本摘要" for section in records[0]["sections"])
    assert records[0]["user_goal"] == "验证这条主线是否仍然有研究价值"
    assert records[0]["evidence_count"] is not None
    assert records[0]["quality_level"]
    manifest = service.get_record(records[0]["record_id"], section="Manifest")["read_window"]["content"]
    assert "quality_level:" in manifest


def test_list_records_accepts_schema_subject_type(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "mainlines"
    root.mkdir(parents=True)
    (root / "demo.md").write_text("# Demo\n\n## Manifest\n- updated_at: now\n", encoding="utf-8")

    payload = service.list_records(subject_type="mainline")

    assert len(payload["records"]) == 1
    assert payload["records"][0]["record_id"] == "mainlines/demo"


def test_list_records_accepts_plural_subject_type(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "mainlines"
    root.mkdir(parents=True)
    (root / "demo.md").write_text("# Demo\n\n## Manifest\n- updated_at: now\n", encoding="utf-8")

    payload = service.list_records(subject_type="mainlines")

    assert len(payload["records"]) == 1
    assert payload["records"][0]["record_id"] == "mainlines/demo"


def test_list_records_can_search_by_query(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "mainlines"
    root.mkdir(parents=True)
    (root / "cpo.md").write_text(
        "# CPO图谱\n\n## Manifest\n- updated_at: now\n\n## 基本认知\n光模块 CPO 硅光 产业链瓶颈\n",
        encoding="utf-8",
    )
    (root / "gold.md").write_text(
        "# 黄金主线\n\n## Manifest\n- updated_at: now\n\n## 基本认知\n黄金 铜 矿业\n",
        encoding="utf-8",
    )

    payload = service.list_records(subject_type="mainline", query="光模块", limit=10)

    assert [record["record_id"] for record in payload["records"]] == ["mainlines/cpo"]
    assert payload["records"][0]["match_score"] >= 0.45


def test_read_record_is_section_paged(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "mainlines"
    root.mkdir(parents=True)
    (root / "demo.md").write_text(
        "# Demo\n\n## Manifest\nsmall\n\n## 来源索引\n" + ("abcdef\n" * 400),
        encoding="utf-8",
    )

    detail = service.get_record("mainlines/demo", section="来源索引", max_chars=1200, offset=0)

    assert detail["read_window"]["has_more"] is True
    assert detail["read_window"]["next_offset"] == 1200
    assert "Manifest" not in detail["read_window"]["content"]


def test_write_record_preserves_append_history_sections(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "mainlines"
    root.mkdir(parents=True)
    record_path = root / "测试主线.md"
    record_path.write_text(
        "# 测试主线\n\n"
        "## Manifest\n- updated_at: old\n\n"
        "## 基本认知\n旧认知会刷新。\n\n"
        "## 历史研究索引\n- old history entry\n\n"
        "## 可信度记录\n- old reliability entry\n\n"
        "## Claim 校验\n- old claim entry\n\n"
        "## 估值记录\n- old valuation entry\n\n"
        "## 逻辑演变\n- old logic entry\n\n"
        "## 风险备忘\n- old risk refreshes\n\n"
        "## 待验证问题\n- old gap refreshes\n\n"
        "## 来源索引\n- old source entry\n",
        encoding="utf-8",
    )
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", auto_start=False)
    updated = service.get_thread(thread["thread_id"])
    updated["status"] = "completed"
    updated["current_conclusion"] = "新结论。"
    updated["evidence"] = [rt._evidence("web", "https://example.com", "new source", "summary", 0.8)]
    updated["validation_results"] = [{
        "reliability": "usable",
        "source_type": "web",
        "source_ref": "https://example.com",
        "reasons": ["new reason"],
    }]
    updated["claim_validation"] = [{
        "status": "supported",
        "claim_type": "price",
        "claim_text": "new claim",
        "confidence": 0.8,
    }]

    service._write_record(updated)
    content = record_path.read_text(encoding="utf-8")

    assert "旧认知会刷新" not in rt._extract_markdown_section(content, "基本认知")
    assert "old history entry" in rt._extract_markdown_section(content, "历史研究索引")
    assert "thread=" in rt._extract_markdown_section(content, "历史研究索引")
    assert "old reliability entry" in rt._extract_markdown_section(content, "可信度记录")
    assert "new reason" in rt._extract_markdown_section(content, "可信度记录")
    assert "old claim entry" in rt._extract_markdown_section(content, "Claim 校验")
    assert "new claim" in rt._extract_markdown_section(content, "Claim 校验")
    assert "old valuation entry" in rt._extract_markdown_section(content, "估值记录")
    assert "old logic entry" in rt._extract_markdown_section(content, "逻辑演变")
    assert "完成一轮研究线程" in rt._extract_markdown_section(content, "逻辑演变")
    assert "old source entry" in rt._extract_markdown_section(content, "来源索引")
    assert "new source" in rt._extract_markdown_section(content, "来源索引")
    assert "读取指南" in content
    assert "证据账本摘要" in content


def test_datahub_sections_follow_user_goal():
    assert rt._datahub_sections_for_focus("给我看看公司资料和触发事件")[:2] == ["profile", "events"]
    assert "position" in rt._datahub_sections_for_focus("结合我的持仓仓位和成本分析")
    assert "daily" in rt._datahub_sections_for_focus("当前现价和近日日线是否矛盾")


def test_research_quality_marks_contradictions():
    summary = rt._research_quality_summary(
        evidence=[rt._evidence("web", "a", "A", "summary", 0.8)],
        gaps=[],
        claim_validation=[{"status": "contradicted"}],
        validation_results=[],
    )

    assert summary["level"] == "conflicted"
    assert summary["claim_statuses"]["contradicted"] == 1


def test_finalize_metrics_never_reports_wall_below_step_total(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    state = rt.ResearchState(
        thread_id="rt_metrics",
        subject="测试主线",
        subject_type="mainline",
        depth="quick",
        metrics={
            "started_at": "2026-06-12T12:00:00",
            "step_timings": [
                {"step_id": "discover", "elapsed_ms": 500},
                {"step_id": "validate", "elapsed_ms": 120},
            ],
        },
    )
    monkeypatch.setattr(rt, "_now", lambda: "2026-06-12T12:00:00")

    metrics = service._finalize_metrics(state)

    assert metrics["total_step_elapsed_ms"] == 620
    assert metrics["wall_elapsed_ms"] == 620


def test_dynamic_datahub_reads_goal_matched_sections(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread(
        "002281.SZ",
        subject_type="stock",
        depth="quick",
        user_goal="给我看看公司资料和触发事件",
        auto_start=False,
    )
    state = rt.ResearchState(
        thread_id=thread["thread_id"],
        subject="002281.SZ",
        subject_type="stock",
        depth="quick",
        user_goal="给我看看公司资料和触发事件",
        ticker="002281.SZ",
        plan=thread["plan"],
    )
    calls = []

    def fake_package(**kwargs):
        calls.append(kwargs)
        if kwargs.get("mode") == "section":
            return {"section": kwargs.get("section"), "read_window": {"content": "section data"}}
        return {"ticker": kwargs.get("ticker"), "readable_sections": []}

    monkeypatch.setattr(rt.datahub_client, "get_stock_data_package", fake_package)

    conclusion = service._dynamic_datahub(state, {"question": "读取资料和事件"})

    sections = [call.get("section") for call in calls if call.get("mode") == "section"]
    assert "profile" in sections
    assert "events" in sections
    assert "已读取 DataHub overview" in conclusion


def test_dynamic_web_uses_depth_budget_and_records_metrics(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    calls = []

    def fake_web_research(**kwargs):
        calls.append(kwargs)
        return {
            "status": "ok",
            "sources": [{"url": "https://example.com/a"}],
            "stopped_reason": "completed",
            "elapsed_ms": 123,
            "provider_attempts": [],
        }

    monkeypatch.setattr(rt, "web_research", fake_web_research)
    standard = rt.ResearchState(thread_id="rt_standard", subject="光迅科技", subject_type="stock", depth="standard")
    deep = rt.ResearchState(thread_id="rt_deep", subject="光迅科技", subject_type="stock", depth="deep")

    standard_conclusion = service._dynamic_web(standard, {"question": "验证近期事件"})
    deep_conclusion = service._dynamic_web(deep, {"question": "验证近期事件"})

    assert len(calls[0]["queries"]) == 2
    assert calls[0]["total_source_budget"] == 5
    assert len(calls[1]["queries"]) == 3
    assert calls[1]["total_source_budget"] == 8
    assert standard.metrics["web_runs"][0]["source_count"] == 1
    assert standard.metrics["web_runs"][0]["status"] == "ok"
    assert "query=2" in standard_conclusion
    assert "query=3" in deep_conclusion


def test_dynamic_web_degrades_failed_or_timeout_results(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)

    def fake_web_research(**_kwargs):
        return {
            "status": "error",
            "sources": [],
            "stopped_reason": "total_timeout",
            "elapsed_ms": 12000,
            "provider_attempts": [],
        }

    monkeypatch.setattr(rt, "web_research", fake_web_research)
    state = rt.ResearchState(thread_id="rt_web_fail", subject="测试主线", subject_type="mainline", depth="standard")

    conclusion = service._dynamic_web(state, {"question": "验证近期事实"})

    assert "status=error" in conclusion
    assert any("联网验证触发总超时" in gap for gap in state.gaps)
    assert any("联网验证未获得有效来源" in gap for gap in state.gaps)
    assert state.evidence[-1]["confidence"] == 0.46
    assert state.metrics["web_runs"][0]["stopped_reason"] == "total_timeout"


def test_dynamic_steps_do_not_duplicate_planner_tool_types(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    state = rt.ResearchState(
        thread_id="rt_test",
        subject="002281.SZ",
        subject_type="stock",
        depth="standard",
        user_goal="系统分析公司资料和触发事件",
        ticker="002281.SZ",
        related_research=[
            {"source_type": "report", "source_ref": "report-1"},
            {"source_type": "tradinggraph", "source_ref": "CPO/光模块"},
        ],
    )
    planner_result = {
        "steps": [
            {"question": "读取本地标的数据", "tool_type": "datahub"},
            {"question": "联网验证事件", "tool_type": "web"},
            {"question": "读已有报告", "tool_type": "report"},
            {"question": "读产业图谱", "tool_type": "graph"},
        ]
    }

    steps = service._build_dynamic_steps(state, planner_result)
    tool_types = [step["tool_type"] for step in steps]

    assert tool_types.count("datahub") == 1
    assert tool_types.count("web") == 1
    assert tool_types.count("report") == 1
    assert tool_types.count("graph") == 1


def test_quick_depth_skips_llm_planner(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    state = rt.ResearchState(
        thread_id="rt_test",
        subject="测试主线",
        subject_type="mainline",
        depth="quick",
    )

    assert service._llm_plan(state) is None


def test_tool_get_research_thread_defaults_to_summary(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    raw = service.get_thread(thread["thread_id"])
    raw["evidence"] = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(thread["thread_id"], evidence=raw["evidence"])
    monkeypatch.setattr(research_tools, "research_thread_service", service)

    payload = research_tools.get_research_thread(thread_id=thread["thread_id"])

    assert payload["detail"] == "summary"
    assert payload["thread"]["record_id"] == "mainlines/测试主线"
    assert len(payload["thread"]["evidence"]) == 12
    assert "summary_preview" in payload["thread"]["evidence"][0]
    assert "summary" not in payload["thread"]["evidence"][0]


def test_tool_get_research_thread_ignores_full_detail_request(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    evidence = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(thread["thread_id"], evidence=evidence)
    monkeypatch.setattr(research_tools, "research_thread_service", service)

    payload = research_tools.get_research_thread(thread_id=thread["thread_id"], detail="full")

    assert payload["detail"] == "summary"
    assert len(payload["thread"]["evidence"]) == 12
    assert "summary_preview" in payload["thread"]["evidence"][0]
    assert "summary" not in payload["thread"]["evidence"][0]


def test_service_list_threads_can_return_summary(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    evidence = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(thread["thread_id"], evidence=evidence)

    payload = service.list_threads(session_id="s1", detail="summary")

    assert payload["detail"] == "summary"
    assert len(payload["threads"][0]["evidence"]) == 12
    assert payload["threads"][0]["truncated"]["evidence_total"] == 20


def test_service_list_threads_defaults_to_summary(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    evidence = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(thread["thread_id"], evidence=evidence)

    payload = service.list_threads(session_id="s1")

    assert payload["detail"] == "summary"
    assert len(payload["threads"][0]["evidence"]) == 12
    assert "summary" not in payload["threads"][0]["evidence"][0]


def test_service_list_threads_ignores_full_detail_request(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    evidence = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(thread["thread_id"], evidence=evidence)

    payload = service.list_threads(session_id="s1", detail="full")

    assert payload["detail"] == "summary"
    assert len(payload["threads"][0]["evidence"]) == 12
    assert "summary_preview" in payload["threads"][0]["evidence"][0]
    assert "summary" not in payload["threads"][0]["evidence"][0]


def test_tool_start_and_control_return_summary_thread(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(research_tools, "research_thread_service", service)

    started = research_tools.start_research_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1")
    controlled_thread = service.start_thread("控制测试", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    evidence = [rt._evidence("source", str(i), "title", "x" * 1000, 0.8) for i in range(20)]
    service._patch_thread(controlled_thread["thread_id"], evidence=evidence)
    controlled = research_tools.control_research_thread(controlled_thread["thread_id"], "pause")

    assert started["detail"] == "summary"
    assert started["thread"]["status"] == "completed"
    assert started["next_actions"][0]["tool"] == "read_research_record"
    assert controlled["detail"] == "summary"
    assert len(controlled["thread"]["evidence"]) == 12
    assert "summary_preview" in controlled["thread"]["evidence"][0]
    assert "summary" not in controlled["thread"]["evidence"][0]


def test_tool_get_completed_thread_points_to_record_sections(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    thread = service.start_thread("测试主线", subject_type="mainline", depth="quick", session_id="s1", auto_start=False)
    service._patch_thread(thread["thread_id"], status="completed", completed_at="2026-06-12T12:00:00")
    monkeypatch.setattr(research_tools, "research_thread_service", service)

    payload = research_tools.get_research_thread(thread_id=thread["thread_id"])

    assert payload["thread"]["record_id"] == "mainlines/测试主线"
    assert [item["tool"] for item in payload["next_actions"]] == [
        "read_research_record",
        "read_research_record",
        "read_research_record",
    ]
    assert payload["next_actions"][0]["arguments"] == {"record_id": "mainlines/测试主线", "section": "Manifest"}


def test_tool_read_research_record_passes_query(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    root = tmp_path / "records" / "stocks"
    root.mkdir(parents=True)
    (root / "光迅科技.md").write_text(
        "# 光迅科技\n\n## Manifest\n- updated_at: now\n\n## 基本认知\nCPO 光模块 标的\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(research_tools, "research_thread_service", service)

    payload = research_tools.read_research_record(subject_type="stock", query="CPO", limit=5)

    assert payload["status"] == "research_record_list"
    assert payload["records"][0]["record_id"] == "stocks/光迅科技"


def test_research_tool_schema_allows_plural_record_filters():
    spec = build_registry().get("read_research_record")
    enum = spec.parameters["properties"]["subject_type"]["enum"]
    description = spec.full_description()

    assert "mainlines" in enum
    assert "stocks" in enum
    assert "get_research_thread returns only compact progress" in description
    assert "read_research_record sections" in description


def test_start_research_thread_schema_exposes_force_new_reuse_guard():
    spec = build_registry().get("start_research_thread")

    assert "force_new" in spec.parameters["properties"]
    assert "defaults to reusing active or recent completed threads" in spec.full_description()
