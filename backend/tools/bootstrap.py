from __future__ import annotations

from backend.core.models import Permission
from backend.core.models import RiskLevel
from backend.tools import analysis
from backend.tools import industry_graph
from backend.tools import research
from backend.tools import skills
from backend.tools import web_research
from backend.tools.datahub import datahub_client
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.tools.reports import report_library
from backend.tools import memory_tools
from backend.services.portfolio_ledger import portfolio_ledger_service


EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}


def assess_single_stock_refresh(args: dict) -> tuple[RiskLevel, str]:
    ticker = str(args.get("ticker") or "").strip()
    if ticker:
        return RiskLevel.LOW_EXPENSIVE, f"刷新单只股票 {ticker} 的外部数据"
    return RiskLevel.LOW_EXPENSIVE, "未指定单只股票，默认只处理当前请求范围"


def assess_market_context_refresh(args: dict) -> tuple[RiskLevel, str]:
    return RiskLevel.LOW_EXPENSIVE, "刷新市场指数和板块上下文数据"


def assess_analysis_run(args: dict) -> tuple[RiskLevel, str]:
    return RiskLevel.HIGH_EXPENSIVE, "启动后台多引擎研究任务"


def assess_research_thread_start(args: dict) -> tuple[RiskLevel, str]:
    subject = str(args.get("subject") or args.get("user_input") or "").strip()
    return RiskLevel.LOW_EXPENSIVE, f"启动长研究线程：{subject or '未命名研究'}"


def assess_report_delete(args: dict) -> tuple[RiskLevel, str]:
    report_id = str(args.get("report_id") or "").strip()
    if args.get("permanent"):
        return RiskLevel.DANGEROUS, f"永久删除研究报告 {report_id}"
    return RiskLevel.WRITE, f"将研究报告 {report_id} 移入本地回收区"


def assess_portfolio_transaction(args: dict) -> tuple[RiskLevel, str]:
    ticker = str(args.get("ticker") or "").strip()
    side = str(args.get("side") or "").strip()
    if ticker and side:
        return RiskLevel.WRITE, f"记录账户交易 {ticker} {side}"
    return RiskLevel.WRITE, "记录账户交易流水"


def ticker_schema(extra: dict | None = None) -> dict:
    properties = {
        "ticker": {
            "type": "string",
            "description": "A股代码，例如 600584.SH 或 002281.SZ。",
        }
    }
    if extra:
        properties.update(extra)
    return {"type": "object", "properties": properties, "required": ["ticker"]}


def portfolio_transaction_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "A股代码，例如 600584.SH 或 002281.SZ。"},
            "name": {"type": "string", "description": "股票名称，可选。"},
            "side": {"type": "string", "description": "交易方向：buy/sell/add/reduce/clear/exit/stop_loss。清仓可传 clear，系统会推断现有数量但仍需要价格。"},
            "quantity": {"type": "number", "description": "成交数量；清仓缺失时会从当前账本持仓推断。缺失则进入 pending。"},
            "price": {"type": "number", "description": "成交价格。卖出、清仓、止损必须提供卖出价格；缺失则进入 pending。"},
            "datetime": {"type": "string", "description": "成交时间 ISO 字符串；默认当前时间。"},
            "fee": {"type": "number", "description": "手续费，默认 0。"},
            "tax": {"type": "number", "description": "税费，默认 0。"},
            "source": {"type": "string", "description": "来源，默认 agent。"},
            "decision_context": {"type": "string", "description": "当时决策上下文，可选。"},
            "rationale": {"type": "string", "description": "交易理由或纪律，可选。"},
            "position_thread_id": {"type": "string", "description": "持仓线程 ID；默认 ticker。"},
        },
        "required": ["ticker", "side"],
    }



def tool_spec(
    name: str,
    description: str,
    permission: Permission,
    handler,
    parameters: dict,
    **kwargs,
) -> ToolSpec:
    layer = _default_layer(name, kwargs.get("group", "default"))
    side_effects = _default_side_effects(permission, layer)
    failure_modes = _default_failure_modes(permission, layer)
    idempotency = _default_idempotency(permission, layer)
    return ToolSpec(
        name,
        description,
        permission,
        handler,
        parameters,
        layer=layer,
        side_effects=side_effects,
        failure_modes=failure_modes,
        idempotency=idempotency,
        **kwargs,
    )


def _default_layer(name: str, group: str) -> str:
    if group == "analysis.run" or name.startswith("run_"):
        return "workflow"
    if name.endswith("_all") or name.endswith("_package") or name.startswith("recommend_"):
        return "composite"
    return "atomic"


def _default_side_effects(permission: Permission, layer: str) -> str:
    if layer == "workflow":
        return "启动后台任务，产生运行日志和报告产物；需要用户确认，可取消。"
    if permission == Permission.READ or permission == Permission.ANALYZE_CACHED:
        return "只读，无持久化变更。"
    if permission == Permission.LOW_RISK_REFRESH:
        return "会访问外部数据源并更新本地缓存，但不修改用户持仓或关注等手工数据。默认自动执行。"
    if permission == Permission.WRITE_CONFIRM:
        return "会修改本地 DataHub 用户数据；需要用户确认。"
    if permission == Permission.EXPENSIVE_CONFIRM:
        return "会访问外部数据源并更新本地缓存；需要用户确认。"
    return "可能产生持久化变更；需要按权限策略确认。"


def _default_failure_modes(permission: Permission, layer: str) -> str:
    if layer == "workflow":
        return "任务可能排队、运行中、失败、取消或产出报告；失败时查询 get_analysis_jobs 和日志。"
    if permission == Permission.READ or permission == Permission.ANALYZE_CACHED:
        return "可能返回空结果、缓存缺失或数据过期；不要编造缺失内容。"
    if permission == Permission.LOW_RISK_REFRESH:
        return "可能因外部数据源超时、限流、标的不存在或部分维度缺失失败；失败时可重试或查看刷新日志。"
    return "可能因参数校验、权限确认、外部 API、资源限制或数据源不可用失败。"


def _default_idempotency(permission: Permission, layer: str) -> str:
    if layer == "workflow":
        return "非幂等：同一参数可能启动新的后台任务，必须通过确认和任务状态避免重复启动。"
    if permission == Permission.READ or permission == Permission.ANALYZE_CACHED:
        return "幂等：同输入通常只读取同一缓存状态，允许自动重试。"
    if permission == Permission.LOW_RISK_REFRESH:
        return "通常可重复执行：同一标的刷新多次只会更新缓存，允许自动重试但仍应避免高频重复调用。"
    if permission == Permission.WRITE_CONFIRM:
        return "部分幂等：upsert 类更新可重复但删除/新增需避免重复确认。"
    return "非严格幂等：可能刷新缓存或访问外部服务，重试有上限。"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(tool_spec("activate_skill", "读取指定能力域的完整 SKILL.md 使用规范。复杂工具流程调用前先激活对应 skill。", Permission.READ, skills.activate_skill, {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill 名称，例如 report-reading、stock-data、web-research、research-thread。"},
        },
        "required": ["name"],
    }, group="skill"))

    registry.register(tool_spec("get_watchlist", "读取用户关注列表。", Permission.READ, datahub_client.get_watchlist, EMPTY_SCHEMA, group="datahub.read"))
    registry.register(tool_spec("get_positions", "读取用户当前手动录入的持仓列表。", Permission.READ, datahub_client.get_positions, EMPTY_SCHEMA, group="datahub.read"))
    registry.register(tool_spec("get_portfolio_summary", "读取当前组合摘要。", Permission.READ, datahub_client.get_portfolio_summary, EMPTY_SCHEMA, group="datahub.read"))
    registry.register(tool_spec("search_stock_symbol", "按股票中文名、简称或代码搜索 A 股标的，返回标准 ticker。用户只给中文名或新上市标的时必须先用本工具解析。", Permission.READ, datahub_client.search_stock_symbol, {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "股票名称、简称或代码，例如 盛合晶微 或 688820。"},
            "limit": {"type": "integer", "description": "返回数量上限，默认 10。"},
        },
        "required": ["query"],
    }, group="datahub.read"))
    registry.register(tool_spec("get_stock_snapshot", "读取某只 A 股的最新行情快照。", Permission.READ, datahub_client.get_stock_snapshot, ticker_schema(), group="datahub.read"))
    registry.register(tool_spec("get_stock_data_package", "读取某只 A 股的 DataHub 结构化数据包，支持 overview 和 section 分页。", Permission.READ, datahub_client.get_stock_data_package, ticker_schema({
        "mode": {"type": "string", "enum": ["overview", "section"], "description": "默认 overview；section 用于读取一个指定 section。"},
        "section": {"type": "string", "enum": ["daily", "indicators", "financials", "profile", "valuation", "moneyflow", "limits", "freshness", "availability", "news", "events", "quality", "position"], "description": "mode=section 时必填。news 仅为旧缓存新闻，不是最新事实校验；最新新闻/来源验证用 web_research。"},
        "offset": {"type": "integer", "description": "section 分页偏移，默认 0。"},
        "limit": {"type": "integer", "description": "section 列表分页条数，默认自动按字符预算返回。"},
        "max_chars": {"type": "integer", "description": "section 字符预算，最大 12000。"},
    }), group="datahub.read"))
    registry.register(tool_spec("web_research", "联网搜索并返回可引用来源，用于验证事实、最新信息和外部材料真实性。", Permission.READ, web_research.web_research, {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "description": "需要验证或检索的单个问题。query 和 queries 二选一；复杂问题优先使用 queries。"},
            "queries": {
                "type": "array",
                "description": "批量并行检索的问题列表，最多 4 个。复杂问题、多个事实校验、公司/行业/事件对比时优先一次性传入，不要连续多次调用工具。",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 2, "description": "单个可搜索问题。"},
                        "intent": {"type": "string", "enum": ["verify_claim", "current_fact", "background", "source_lookup"], "description": "该 query 的用途。"},
                        "recency": {"type": "string", "enum": ["day", "week", "month", "year", "any"], "description": "该 query 的时效范围。"},
                        "source_policy": {"type": "string", "enum": ["official_first", "finance_first", "broad_web"], "description": "该 query 的来源偏好。"},
                    },
                    "required": ["query"],
                },
            },
            "intent": {"type": "string", "enum": ["verify_claim", "current_fact", "background", "source_lookup"], "description": "默认 verify_claim。"},
            "recency": {"type": "string", "enum": ["day", "week", "month", "year", "any"], "description": "时效范围，默认 any；最新消息用 day/week。"},
            "source_policy": {"type": "string", "enum": ["official_first", "finance_first", "broad_web"], "description": "默认 finance_first；监管/公告/指数事实用 official_first。"},
            "max_sources": {"type": "integer", "description": "兼容旧参数：单 query 返回来源数量；新请求优先用 max_sources_per_query/total_source_budget。"},
            "max_sources_per_query": {"type": "integer", "description": "每个 query 最多返回来源数，默认 verify/current=3，background=4，最大 5。"},
            "total_source_budget": {"type": "integer", "description": "所有 query 合计来源上限，默认最多 8，最大 12。"},
        },
        "anyOf": [
            {"required": ["query"]},
            {"required": ["queries"]},
        ],
    }, group="web.read"))
    registry.register(tool_spec("add_watchlist_item", "新增关注标的。需要用户确认。", Permission.WRITE_CONFIRM, datahub_client.add_watchlist_item, ticker_schema({
        "name": {"type": "string", "description": "股票名称，可选。"},
        "list_name": {"type": "string", "description": "关注列表名称，默认：默认关注。"},
        "status": {"type": "string", "description": "关注状态，默认：观察。"},
        "reason": {"type": "string", "description": "关注原因，可选。"},
        "note": {"type": "string", "description": "兼容字段，等同 reason。"},
    }), group="datahub.write"))
    registry.register(tool_spec("remove_watchlist_item", "移除关注标的。需要用户确认。", Permission.WRITE_CONFIRM, datahub_client.remove_watchlist_item, ticker_schema({
        "list_name": {"type": "string", "description": "关注列表名称，默认：默认关注。"},
    }), group="datahub.write"))
    registry.register(tool_spec("remove_position", "删除整条持仓记录。需要用户确认。", Permission.WRITE_CONFIRM, datahub_client.remove_position, ticker_schema(), group="datahub.write"))
    registry.register(tool_spec("upsert_position", "新增或更新持仓。需要用户确认。", Permission.WRITE_CONFIRM, datahub_client.upsert_position, ticker_schema({
        "name": {"type": "string", "description": "股票名称，可选。"},
        "quantity": {"type": "number", "description": "持仓数量。只修改股数时仅传该字段，不要补 0 成本；若传 0 或负数，将删除整条持仓。"},
        "avg_cost": {"type": "number", "description": "平均成本。只有用户明确提供成本时才传。"},
        "cost_price": {"type": "number", "description": "平均成本价格，优先级高于 avg_cost。只有用户明确提供成本时才传。"},
        "note": {"type": "string", "description": "备注，可选。"},
    }), group="datahub.write"))
    registry.register(tool_spec("record_portfolio_transaction", "记录买入/卖出/加减仓/清仓/止损等账户交易。需要用户确认。", Permission.WRITE_CONFIRM, portfolio_ledger_service.record_transaction, portfolio_transaction_schema(), group="datahub.write", risk_assessor=assess_portfolio_transaction))
    registry.register(tool_spec("list_report_catalog", "列出统一研究报告目录。返回报告对象摘要，不返回 HTML/MD/JSON 原始文件列表。用户问有哪些研究报告时优先使用。", Permission.ANALYZE_CACHED, report_library.list_report_catalog, {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "可选：市场层、主题/板块层、个股层、交易计划层。"},
            "report_type": {"type": "string", "description": "可选报告类型，例如 market_discovery、theme_deep_dive、stock_research。"},
            "source": {"type": "string", "description": "可选来源，例如 主线雷达/ThemeRadar 或 个股深研/EquityScope。"},
            "subject": {"type": "string", "description": "可选主题或股票代码。"},
            "limit": {"type": "integer", "description": "返回数量上限，默认 50。"},
        },
        "required": [],
    }, group="report.read"))
    registry.register(tool_spec("get_report_detail", "读取某份报告的元信息、章节目录、字符数和打开链接。", Permission.ANALYZE_CACHED, report_library.get_report_detail, {
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "description": "报告对象 ID，来自 list_report_catalog。"},
        },
        "required": ["report_id"],
    }, group="report.read"))
    registry.register(tool_spec("query_report", "按问题从指定报告中抽取受控关键材料。", Permission.ANALYZE_CACHED, report_library.query_report, {
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "description": "报告对象 ID，来自 list_report_catalog。"},
            "question": {"type": "string", "description": "用户问题或关注点，用于选择相关章节。"},
            "max_sections": {"type": "integer", "description": "最多抽取章节数，默认 4，最大 8。"},
            "per_section_chars": {"type": "integer", "description": "每个章节摘录字符预算，默认 2600，最大 4000。"},
            "total_chars": {"type": "integer", "description": "总返回字符预算，默认 9000，最大 12000。"},
        },
        "required": ["report_id"],
    }, group="report.read"))
    registry.register(tool_spec("read_report_section", "按 section_id 分页读取报告正文。", Permission.ANALYZE_CACHED, report_library.read_report_section, {
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "description": "报告对象 ID，来自 list_report_catalog。"},
            "section_id": {"type": "string", "description": "章节 ID，来自 get_report_detail.manifest.sections，例如 s001。"},
            "max_chars": {"type": "integer", "description": "最大读取字符数，默认 12000，系统最大 12000。"},
            "offset": {"type": "integer", "description": "读取偏移。read_window.has_more=true 时使用 next_offset 续读。"},
        },
        "required": ["report_id", "section_id"],
    }, group="report.read"))
    registry.register(tool_spec("get_stock_research_status", "判断某只股票的个股深研状态。", Permission.ANALYZE_CACHED, report_library.get_stock_research_status, ticker_schema({
        "stale_days": {"type": "integer", "description": "过期天数阈值，默认 60。"},
    }), group="report.read"))
    registry.register(tool_spec("recommend_stock_research_action", "根据报告新鲜度和事件标记判断是否需要重跑个股研究。", Permission.ANALYZE_CACHED, report_library.recommend_stock_research_action, ticker_schema({
        "stale_days": {"type": "integer", "description": "过期天数阈值，默认 60。"},
        "major_event": {"type": "boolean", "description": "是否存在重大事件导致旧报告可能失效。"},
    }), group="report.read"))

    registry.register(tool_spec("get_report_links", "获取报告安全打开或下载链接。", Permission.ANALYZE_CACHED, report_library.get_report_links, {
        "type": "object",
        "properties": {
            "report_type": {"type": "string", "description": "可选：market_discovery 或 stock_research。"},
            "date": {"type": "string", "description": "报告日期，YYYY-MM-DD，可选。"},
            "query": {"type": "string", "description": "关键词，可选。"},
            "limit": {"type": "integer", "description": "返回数量上限，默认 5。"},
        },
        "required": [],
    }, group="report.read"))
    registry.register(tool_spec("delete_report", "删除一份研究报告。默认移入 FinClaw 本地回收区；只有用户明确要求永久删除时才传 permanent=true。需要用户确认。", Permission.DANGEROUS_WRITE, report_library.delete_report, {
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "description": "报告对象 ID，来自 list_report_catalog，例如 stock_research:688820.SH:2026-05-17。"},
            "permanent": {"type": "boolean", "description": "是否永久删除。默认 false，表示移入本地回收区。只有用户明确要求永久删除时才设为 true。"},
        },
        "required": ["report_id"],
    }, risk_assessor=assess_report_delete, group="report.write"))
    registry.register(tool_spec("get_analysis_jobs", "查询正在运行、失败或已完成的后台分析任务进度。用户问某个调研/分析进行到哪了、是否完成、后台任务状态时优先使用本工具，不要重新启动分析。", Permission.ANALYZE_CACHED, analysis.get_analysis_jobs, {
        "type": "object",
        "properties": {
            "job_type": {"type": "string", "description": "可选：stock_research 或 market_discovery。"},
            "ticker": {"type": "string", "description": "可选 A 股代码，例如 000988.SZ。"},
            "status": {"type": "string", "description": "可选：running、failed、completed。"},
            "limit": {"type": "integer", "description": "返回数量上限，默认 10。"},
        },
        "required": [],
    }, group="job.read"))
    registry.register(tool_spec("run_market_discovery", "运行主线雷达市场主线发现任务。", Permission.EXPENSIVE_CONFIRM, analysis.run_market_discovery, {
        "type": "object",
        "properties": {
            "no_resume": {"type": "boolean", "description": "是否忽略已有中间结果重新运行，默认 false。"},
        },
        "required": [],
    }, risk_assessor=assess_analysis_run, group="analysis.run"))
    registry.register(tool_spec("run_stock_research", "运行个股深研任务。", Permission.EXPENSIVE_CONFIRM, analysis.run_stock_research, ticker_schema({
        "trade_date": {"type": "string", "description": "交易日期，YYYY-MM-DD，可选。"},
        "force": {"type": "boolean", "description": "仅当已有同标的任务 failed/cancelled 且用户明确要求重跑时设为 true；已有 running 任务仍会复用，不会重复启动。"},
    }), risk_assessor=assess_analysis_run, group="analysis.run"))

    registry.register(tool_spec("control_industry_graph", "控制产业链透视图谱任务。", Permission.EXPENSIVE_CONFIRM, industry_graph.control_industry_graph, {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start_or_resume", "pause", "resume", "continue_from_node", "enrich_nodes"], "description": "start_or_resume 会优先复用可恢复任务；continue_from_node 是从图中某节点继续扩展；enrich_nodes 只做节点补充深挖。"},
            "mode": {"type": "string", "enum": ["mainline", "ticker"], "description": "新建任务模式，默认 mainline。"},
            "query": {"type": "string", "description": "新建或查找可恢复任务的主线名/股票代码。操作已有或模糊主线前先调用 read_industry_graph.list_mainlines。"},
            "run_id": {"type": "string", "description": "暂停、恢复、从节点继续或深挖时必填。"},
            "node_ids": {"type": "array", "items": {"type": "string"}, "description": "continue_from_node 只传 1 个；enrich_nodes 可传多个。"},
            "markets": {"type": "array", "items": {"type": "string", "enum": ["CN", "US", "HK"]}, "description": "默认 CN/US/HK。"},
            "budget": {"type": "object", "description": "可选预算，例如 max_nodes、max_depth。"},
        },
        "required": ["action"],
    }, risk_assessor=assess_analysis_run, group="industry_graph.run"))
    registry.register(tool_spec("read_industry_graph", "读取产业链透视的主线目录、节点目录、邻居关系或任务状态。", Permission.ANALYZE_CACHED, industry_graph.read_industry_graph, {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list_mainlines", "get_graph_summary", "get_node_neighbors", "get_run_status", "get_resumable_run"], "description": "默认 list_mainlines；选节点/看全节点目录用 get_graph_summary；节点上下游关系用 get_node_neighbors；完整节点内容用 read_industry_graph_node。完整图谱读取已禁用。"},
            "mainline": {"type": "string", "description": "精确主线名。模糊时先 list_mainlines。"},
            "run_id": {"type": "string", "description": "get_run_status 必填。"},
            "node_id": {"type": "string", "description": "get_node_neighbors 必填。可传图谱摘要里的 node_ref（如 n001，需同时传 mainline）或节点详情里的真实 node_id。"},
            "include_osint": {"type": "boolean", "description": "是否包含 OSINT 边，默认 true。"},
            "offset": {"type": "integer", "description": "分页偏移。get_graph_summary 用于节点目录续读；get_node_neighbors 用于邻居/边续读。默认 0。"},
            "limit": {"type": "integer", "description": "分页大小。摘要目录默认尽量返回全量但有字符预算；邻居默认 20，最大 20。过大会自动收紧而不是报错。"},
            "depth": {"type": "integer", "description": "get_node_neighbors 的关系深度，默认 1，最大 2。超过最大值会自动降级并返回 truncated=true。"},
        },
        "required": [],
    }, group="industry_graph.read"))
    registry.register(tool_spec("read_industry_graph_node", "读取产业链透视单节点安全概览或指定字段。", Permission.ANALYZE_CACHED, industry_graph.read_industry_graph_node, {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "产业链透视节点 ID 或摘要中的 node_ref（如 n001；用 node_ref 时建议同时传 mainline 以便解析）。"},
            "include_neighbors": {"type": "boolean", "description": "是否同时返回该节点上下游/邻居关系，默认 false。"},
            "mainline": {"type": "string", "description": "可选精确主线名，用于限制邻居关系范围。"},
            "include_osint": {"type": "boolean", "description": "include_neighbors=true 时是否包含 OSINT 边，默认 true。"},
            "mode": {"type": "string", "enum": ["overview", "field"], "description": "默认 overview；field 用于读取 readable_fields 中的一个字段。"},
            "field": {"type": "string", "description": "mode=field 时必填。只能使用 overview 返回的 readable_fields，如 tickers、bottleneck_profile、price_changes、key_findings。支持点路径，如 bottleneck_profile.metrics。"},
            "offset": {"type": "integer", "description": "字段分页偏移。字段返回 has_more=true 时使用 next_offset 续读。"},
            "limit": {"type": "integer", "description": "字段分页条数。过大会自动收紧。"},
            "max_chars": {"type": "integer", "description": "字段读取字符预算，最大按系统工具阈值收紧。"},
        },
        "required": ["node_id"],
    }, group="industry_graph.read"))

    registry.register(tool_spec("start_research_thread", "仅供前端“开启研究”按钮模式使用：创建并启动可恢复的 Deep Research Agent 线程。普通对话不要调用。", Permission.EXPENSIVE_CONFIRM, research.start_research_thread, {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "研究对象，例如 紫金矿业、CPO/光模块、光迅科技 vs 新易盛。"},
            "subject_type": {"type": "string", "enum": ["stock", "mainline", "market", "comparison", "unknown"], "description": "研究对象类型，默认 unknown。"},
            "depth": {"type": "string", "enum": ["quick", "standard", "deep"], "description": "兼容字段；建议和 budget_profile 保持一致。"},
            "research_goal": {"type": "string", "description": "用户确认后的研究目标。只提炼用户明确表达的研究对象、问题和约束；不要自行扩展研究范围、添加用户未提出的问题、预设结论或研究框架。"},
            "subject_hint": {"type": "string", "description": "可选：用户原始表达中的对象线索。"},
            "scope_hint": {"type": "string", "description": "可选：研究范围，例如 只看A股映射、重点验证近期事件、对比三家公司。"},
            "budget_profile": {"type": "string", "enum": ["quick", "standard", "deep"], "description": "后台 Agent 预算档位。quick 低预算，standard 常规，deep 可使用授权高成本工具。默认 standard。"},
            "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "允许后台 Deep Research Agent 自主调用的工具名。用户确认卡片可修改。"},
            "blocked_tools": {"type": "array", "items": {"type": "string"}, "description": "禁止后台 Deep Research Agent 调用的工具名。用户说不使用某工具时必须填入。"},
            "constraints": {"type": "string", "description": "用户约束，例如 不使用产业链透视、不要启动新研究、只用本地报告。"},
            "user_goal": {"type": "string", "description": "兼容字段；没有 research_goal 时使用。"},
            "force_new": {"type": "boolean", "description": "默认 false，会复用同会话活跃或最近完成的同对象研究；只有用户明确要求重跑/重新研究时才传 true。"},
        },
        "required": ["subject", "research_goal"],
    }, risk_assessor=assess_research_thread_start, group="research"))
    registry.register(tool_spec("get_research_thread", "读取研究线程详情或列表。", Permission.READ, research.get_research_thread, {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "研究线程 ID；不传则按 session/status/subject 列表查询。"},
            "status": {"type": "string", "description": "可选状态过滤，例如 in_progress/completed/failed。"},
            "subject": {"type": "string", "description": "可选研究对象关键词。"},
            "limit": {"type": "integer", "description": "列表数量上限，默认 10。"},
        },
        "required": [],
    }, group="research"))
    registry.register(tool_spec("control_research_thread", "暂停、恢复或取消研究线程。", Permission.READ, research.control_research_thread, {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "研究线程 ID。"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel"], "description": "控制动作。"},
        },
        "required": ["thread_id", "action"],
    }, group="research"))
    registry.register(tool_spec("read_research_record", "读取已沉淀的研究档案，支持列表、目录和章节分页。", Permission.READ, research.read_research_record, {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "研究档案 ID，例如 stocks/光迅科技；为空则列出档案。"},
            "subject_type": {"type": "string", "enum": ["stock", "stocks", "mainline", "mainlines", "comparison", "comparisons", "unknown", ""], "description": "列表过滤类型；单数和复数目录名都兼容。"},
            "query": {"type": "string", "description": "可选主题/标的/主线关键词。启动新研究前应先传 query 检索相关旧研究。"},
            "section": {"type": "string", "description": "可选章节名，例如 Manifest、结论边界、验证 Agent 反馈、证据账本摘要、待验证问题、来源索引。"},
            "offset": {"type": "integer", "description": "分页偏移，read_window.has_more=true 时传 next_offset 续读。"},
            "max_chars": {"type": "integer", "description": "单次读取字符预算，最大由后端收紧。默认 6000。"},
            "limit": {"type": "integer", "description": "列表数量上限，默认 20。"},
        },
        "required": [],
    }, group="research"))

    # 记忆系统工具（不需要 approval）
    registry.register(tool_spec("memory_read", "读取长期记忆文件；profile 默认只读快照，playbook 默认只读当前研究架构。", Permission.READ, memory_tools.memory_read,
        {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "enum": ["profile", "playbook", "convictions"],
                    "description": "记忆文件类型：profile（用户画像）、playbook（研究框架）、convictions（当前有效投资判断）"
                },
                "section": {
                    "type": "string",
                    "description": "可选章节名称。不传时：profile 返回人物志快照，playbook 返回当前研究架构，convictions 返回当前文件。"
                }
            },
            "required": ["file"]
        },
        group="memory"
    ))

    registry.register(tool_spec("memory_write", "生成长期记忆写入候选；profile 可自动写入，playbook/convictions 需要用户确认。playbook 只能提交研究架构修改，不保存案例或报告摘要。", Permission.READ, memory_tools.memory_write,
        {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "enum": ["profile", "playbook", "convictions"],
                    "description": "记忆文件类型。playbook 只记录当前研究架构修改；convictions 只记录当前有效投资判断，必须包含判断、失效条件、来源，不能包含具体买卖/仓位操作。"
                },
                "content": {
                    "type": "string",
                    "description": "要写入的 markdown 内容。playbook 内容必须是研究架构本身，例如维度、问题、关注重点的增删改。"
                },
                "reason": {
                    "type": "string",
                    "description": "写入原因（一句话说明为什么记录这个）"
                },
                "position": {
                    "type": "string",
                    "description": "写入位置。playbook 不使用位置追加语义；如果需要修改已有架构，优先使用 memory_update 并提供精确 target。"
                },
                "metadata": {
                    "type": "object",
                    "description": "可选的长期记忆元数据，用于覆盖系统默认值。",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "source": {"type": "string"},
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                        "ttl_days": {"type": "integer"},
                        "decay_weight": {"type": "number"},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "session_id": {"type": "string"},
                        "source_message_id": {"type": "integer"},
                        "trigger": {"type": "string"},
                    },
                }
            },
            "required": ["file", "content", "reason"]
        },
        group="memory"
    ))

    registry.register(tool_spec("memory_update", "生成长期记忆更新候选；用于修正已有 playbook 架构片段或 convictions 条目。", Permission.READ, memory_tools.memory_update,
        {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "enum": ["profile", "playbook", "convictions"],
                    "description": "记忆文件类型。playbook 更新用于替换当前研究架构片段；convictions 更新用于修正已有投资判断，不用于追加相反观点。"
                },
                "target": {
                    "type": "string",
                    "description": "要替换的原文片段（精确字符串匹配）"
                },
                "new_content": {
                    "type": "string",
                    "description": "新内容"
                },
                "reason": {
                    "type": "string",
                    "description": "修改原因"
                },
                "metadata": {
                    "type": "object",
                    "description": "可选的长期记忆元数据，用于覆盖系统默认值。",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "source": {"type": "string"},
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                        "ttl_days": {"type": "integer"},
                        "decay_weight": {"type": "number"},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "session_id": {"type": "string"},
                        "source_message_id": {"type": "integer"},
                        "trigger": {"type": "string"},
                    },
                }
            },
            "required": ["file", "target", "new_content", "reason"]
        },
        group="memory"
    ))

    registry.register(tool_spec("memory_archive", "生成归档候选；批准后将内容从活跃记忆移入 archive。", Permission.READ, memory_tools.memory_archive,
        {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "enum": ["profile", "playbook", "convictions"],
                    "description": "记忆文件类型"
                },
                "target": {
                    "type": "string",
                    "description": "要归档的原文片段"
                },
                "reason": {
                    "type": "string",
                    "description": "归档原因"
                },
                "metadata": {
                    "type": "object",
                    "description": "可选的长期记忆元数据，用于覆盖系统默认值。",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "source": {"type": "string"},
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                        "ttl_days": {"type": "integer"},
                        "decay_weight": {"type": "number"},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "session_id": {"type": "string"},
                        "source_message_id": {"type": "integer"},
                        "trigger": {"type": "string"},
                    },
                }
            },
            "required": ["file", "target", "reason"]
        },
        group="memory"
    ))

    return registry
