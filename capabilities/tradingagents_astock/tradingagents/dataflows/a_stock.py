"""A-share DataHub vendor for TradingAgents.

This module intentionally does not call upstream market data providers.
All A-share tools read from FinDataHub so TradingAgents uses one consistent,
auditable data layer.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Annotated, Any

import pandas as pd
import requests

from .config import get_config
from .utils import safe_ticker_component

def set_active_ticker(ticker: str | None) -> None:
    # Compatibility hook for older callers. The actual binding is enforced in
    # dataflows.interface via config["active_ticker"].
    return None


def _base_url() -> str:
    config = get_config()
    return (
        config.get("datahub_api_base")
        or os.getenv("TRADINGAGENTS_DATAHUB_URL")
        or os.getenv("FINDATAHUB_API_BASE")
        or "http://127.0.0.1:8700"
    ).rstrip("/")


def _normalize_ticker(symbol: str) -> str:
    s = safe_ticker_component(symbol.strip().upper())
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            return s
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            code = s[len(prefix) :]
            suffix = ".SH" if prefix == "SH" else ".SZ" if prefix == "SZ" else ".BJ"
            return f"{code}{suffix}"
    if s.startswith("6"):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return s


def _pure_code(symbol: str) -> str:
    return _normalize_ticker(symbol).split(".", 1)[0]


def _get(path: str, default: Any = None, params: dict[str, Any] | None = None) -> Any:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 404:
            return default
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"DataHub request failed: {url}: {exc}") from exc


def _package(ticker: str) -> dict:
    return _get(f"/api/data-package/{_normalize_ticker(ticker)}", {}) or {}


def _market_overview_package(overview_limit: int = 10, news_limit: int = 8) -> dict:
    params: dict[str, Any] = {"overview_limit": overview_limit, "news_limit": news_limit}
    return _get("/api/market/package", {}, params) or {}


def _market_package(ticker: str, trade_date: str | None = None, overview_limit: int = 10, news_limit: int = 8) -> dict:
    normalized = _normalize_ticker(ticker)
    params: dict[str, Any] = {"overview_limit": overview_limit, "news_limit": news_limit}
    if trade_date:
        params["trade_date"] = trade_date
    return _get(f"/api/market/package/{normalized}", {}, params) or {}


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _parse_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        return pd.to_datetime(value)
    except Exception:
        return None


def _filter_by_date(rows: list[dict], date_key: str, start_date: str | None, end_date: str | None) -> list[dict]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    filtered = []
    for row in rows:
        dt = _parse_date(row.get(date_key))
        if dt is None:
            continue
        if start is not None and dt < start:
            continue
        if end is not None and dt > end:
            continue
        filtered.append(row)
    return filtered


def _records_to_csv(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return ""
    df = pd.DataFrame(rows)
    cols = [col for col in columns if col in df.columns]
    return df[cols].to_csv(index=False)


def _records_to_markdown(rows: list[dict], columns: list[str] | None = None, max_rows: int = 10) -> str:
    if not rows:
        return ""
    df = pd.DataFrame(rows).head(max_rows)
    if columns:
        cols = [col for col in columns if col in df.columns]
        if cols:
            df = df[cols]
    if df.empty:
        return ""
    header = "| " + " | ".join(str(col) for col in df.columns) + " |"
    divider = "| " + " | ".join(["---"] * len(df.columns)) + " |"
    lines = [header, divider]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row.get(col)) for col in df.columns) + " |")
    return "\n".join(lines)


def _latest_metric(pkg: dict) -> dict:
    metrics = pkg.get("metrics") or []
    return metrics[0] if metrics else {}


def _raw_metric(pkg: dict) -> dict:
    return _latest_metric(pkg).get("raw") or {}


def _datahub_missing(name: str) -> str:
    return (
        f"# {name}\n"
        "DataHub currently does not provide this dataset. "
        "No direct upstream fallback was used because A-share data access is centralized through DataHub."
    )


def _format_market_package(package: dict) -> str:
    ticker = package.get("ticker") or "MARKET"
    generated_at = package.get("generated_at") or datetime.utcnow().isoformat()
    trade_date = package.get("trade_date") or "latest"
    lines = [
        f"# Market Package for {ticker}",
        "# Data source: FinDataHub",
        f"# Trade date: {_fmt(trade_date)}",
        f"# Generated at: {_fmt(generated_at)}",
        "",
    ]

    summary = package.get("summary") or []
    if summary:
        lines.append("## Summary")
        for item in summary[:8]:
            lines.append(f"- {item}")

    quality = package.get("quality") or {}
    if quality:
        lines.extend(
            [
                "",
                "## Quality",
                f"Status: {_fmt(quality.get('status'))}",
            ]
        )
        missing = quality.get("missing") or []
        if missing:
            lines.append(f"Missing: {', '.join(str(item) for item in missing)}")

    overview = package.get("overview") or {}
    breadth = overview.get("breadth") or {}
    fund_flow = overview.get("fund_flow") or {}
    indices = overview.get("indices") or []
    sectors = overview.get("sectors") or []
    events = overview.get("events") or []

    lines.extend(["", "## Market Overview"])
    if breadth:
        lines.append(
            f"Broadness: up={_fmt(breadth.get('up_count'))}, down={_fmt(breadth.get('down_count'))}, "
            f"limit_up={_fmt(breadth.get('limit_up_count'))}, limit_down={_fmt(breadth.get('limit_down_count'))}"
        )
    if fund_flow:
        lines.append(
            f"Capital flow: northbound={_fmt(fund_flow.get('northbound_net_amount'))}, main={_fmt(fund_flow.get('main_net_inflow'))}"
        )
    if indices:
        lines.append("")
        lines.append("Top indices:")
        lines.append(_records_to_markdown(indices, ["name", "price", "change_pct", "amount"], 5))
    if sectors:
        lines.append("")
        lines.append("Top sectors:")
        lines.append(_records_to_markdown(sectors, ["name", "price", "change_pct", "amount", "leader"], 5))
    if events:
        lines.append("")
        lines.append("Recent market events:")
        for event in events[:5]:
            lines.append(f"- {event.get('title')}: {event.get('summary') or ''}")

    hot_stocks = package.get("hot_stocks") or {}
    if hot_stocks:
        lines.extend(["", "## Hot Stocks"])
        if hot_stocks.get("rank"):
            lines.append("Rank:")
            lines.append(_records_to_markdown(hot_stocks.get("rank") or [], ["当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"], 10))
        if hot_stocks.get("up"):
            lines.append("")
            lines.append("Upward movers:")
            lines.append(_records_to_markdown(hot_stocks.get("up") or [], ["排名较昨日变动", "当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"], 10))

    northbound = package.get("northbound_flow") or {}
    history = northbound.get("history") or []
    if history:
        lines.extend(["", "## Northbound Flow"])
        lines.append(_records_to_markdown(history, ["日期", "当日成交净买额", "买入成交额", "卖出成交额", "历史累计净买额", "当日资金流入"], 10))

    if package.get("concept_blocks"):
        concept = package.get("concept_blocks") or {}
        profile = concept.get("profile") or {}
        lines.extend(["", "## Concept Blocks"])
        lines.append(f"Industry: {_fmt(concept.get('industry') or profile.get('行业'))}")
        lines.append(f"List date: {_fmt(concept.get('list_date') or profile.get('上市时间'))}")
        lines.append(f"Market cap: {_fmt(concept.get('market_cap') or profile.get('总市值'))}")
        if concept.get("top_concepts"):
            lines.append(_records_to_markdown(concept.get("top_concepts") or [], ["name", "change_pct", "constituent_rank"], 10))

    if package.get("fund_flow"):
        flow = package.get("fund_flow") or {}
        lines.extend(["", "## Fund Flow"])
        latest = (flow.get("history") or [None])[0]
        if latest:
            lines.append(
                f"Latest: date={_fmt(latest.get('日期'))}, main={_fmt(latest.get('主力净流入-净额'))}, "
                f"super_large={_fmt(latest.get('超大单净流入-净额'))}, large={_fmt(latest.get('大单净流入-净额'))}"
            )
        if flow.get("market_rank"):
            rank = flow["market_rank"]
            lines.append(f"Market rank: {_fmt(rank.get('今日排行榜-今日排名'))} / {_fmt(rank.get('所属板块'))}")

    if package.get("dragon_tiger_board"):
        board = package.get("dragon_tiger_board") or {}
        rows = board.get("rows") or []
        if rows:
            lines.extend(["", "## Dragon Tiger Board"])
            lines.append(_records_to_markdown(rows, ["上榜日", "解读", "龙虎榜净买额", "龙虎榜买入额", "龙虎榜卖出额", "换手率", "上榜原因"], 10))

    if package.get("lockup_expiry"):
        lockup = package.get("lockup_expiry") or {}
        queue = lockup.get("queue") or []
        detail = lockup.get("detail") or []
        if queue or detail:
            lines.extend(["", "## Lockup Expiry"])
            if queue:
                lines.append("Upcoming queue:")
                lines.append(_records_to_markdown(queue, ["解禁时间", "解禁股东数", "解禁数量", "实际解禁数量", "占流通市值比例", "限售股类型"], 10))
            if detail:
                lines.append("")
                lines.append("Detail:")
                lines.append(_records_to_markdown(detail, ["解禁时间", "限售股类型", "解禁数量", "实际解禁数量", "实际解禁市值", "占解禁前流通市值比例"], 10))

    if package.get("industry_comparison"):
        comp = package.get("industry_comparison") or {}
        rows = comp.get("rows") or []
        if rows:
            lines.extend(["", "## Industry Comparison"])
            lines.append(f"Industry: {_fmt(comp.get('industry_name'))}")
            target = comp.get("target") or {}
            if target:
                lines.append(f"Target: {target.get('名称')} change_pct={_fmt(target.get('涨跌幅'))}")
            lines.append(_records_to_markdown(rows, ["名称", "涨跌幅", "涨跌额", "成交额", "换手率", "市盈率-动态", "市净率"], 10))

    return "\n".join(line for line in lines if line is not None)


def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, 688017.SH)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    ticker = _normalize_ticker(symbol)
    try:
        pkg = _package(ticker)
        rows = _filter_by_date((pkg.get("daily_qfq") or pkg.get("daily") or []), "trade_date", start_date, end_date)
        if not rows:
            return f"No DataHub daily price data found for {ticker} between {start_date} and {end_date}."
        csv_out = _records_to_csv(rows, ["trade_date", "open", "high", "low", "close", "volume", "amount", "source"])
        return (
            f"# Stock data for {ticker} from {start_date} to {end_date}\n"
            f"# Data source: FinDataHub\n"
            f"# Total records: {len(rows)}\n\n"
            f"{csv_out}"
        )
    except Exception as exc:
        return f"Error retrieving DataHub stock data for {ticker}: {exc}"


_INDICATOR_MAP = {
    "close_50_sma": "ma60",
    "close_200_sma": "ma60",
    "close_10_ema": "ma10",
    "macd": "macd",
    "macds": "macd_signal",
    "macdh": "macd_hist",
    "rsi": "rsi14",
    "atr": "atr14",
    "boll": "boll_mid",
    "boll_mid": "boll_mid",
    "boll_ub": "boll_ub",
    "boll_lb": "boll_lb",
    "vwma": "vwma20",
    "vwma20": "vwma20",
    "ma5": "ma5",
    "ma10": "ma10",
    "ma20": "ma20",
    "ma60": "ma60",
}


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[str, "technical indicator"],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    ticker = _normalize_ticker(symbol)
    ind = indicator.lower().strip()
    column = _INDICATOR_MAP.get(ind)
    if not column:
        return f"Indicator {indicator} is not available in DataHub. Available: {sorted(_INDICATOR_MAP)}"
    try:
        pkg = _package(ticker)
        start = (pd.to_datetime(curr_date) - pd.Timedelta(days=look_back_days)).strftime("%Y-%m-%d")
        rows = _filter_by_date((pkg.get("indicators_qfq") or pkg.get("indicators") or []), "trade_date", start, curr_date)
        if not rows:
            return f"No DataHub indicator data found for {ticker}."
        lines = [f"## {indicator} values for {ticker} from {start} to {curr_date}", "", "# Data source: FinDataHub"]
        for row in rows:
            lines.append(f"{row.get('trade_date')}: {_fmt(row.get(column))}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub indicator data for {ticker}: {exc}"


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        pkg = _package(normalized)
        profile = pkg.get("profile") or {}
        snapshot = pkg.get("snapshot") or {}
        metric = _latest_metric(pkg)
        raw = metric.get("raw") or {}
        valuation = raw.get("tencent_valuation") or ((profile.get("raw") or {}).get("tencent_snapshot") or {}).get("valuation") or {}
        finance = raw.get("finance") or (profile.get("raw") or {}).get("finance") or {}

        lines = [
            f"# Company Fundamentals for {normalized}",
            "# Data source: FinDataHub",
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"Name: {_fmt(profile.get('name') or snapshot.get('name'))}",
            f"Industry: {_fmt(profile.get('industry'))}",
            f"Area: {_fmt(profile.get('area'))}",
            f"Market: {_fmt(profile.get('market'))}",
            f"List Date: {_fmt(profile.get('list_date'))}",
            f"Current Price: {_fmt(snapshot.get('price'))}",
            f"Change %: {_fmt(snapshot.get('change_pct'))}",
            f"PE (TTM): {_fmt(valuation.get('pe_ttm'))}",
            f"PE (Static): {_fmt(valuation.get('pe_static'))}",
            f"PB: {_fmt(valuation.get('pb'))}",
            f"Market Cap (100M CNY): {_fmt(valuation.get('market_cap_yi'))}",
            f"Float Market Cap (100M CNY): {_fmt(valuation.get('float_market_cap_yi'))}",
            f"Turnover Rate %: {_fmt(valuation.get('turnover_pct'))}",
            f"EPS: {_fmt(metric.get('eps') or finance.get('eps'))}",
            f"ROE: {_fmt(metric.get('roe') or finance.get('roe'))}",
            f"Debt to Assets: {_fmt(metric.get('debt_to_assets'))}",
        ]
        forecast = raw.get("profit_forecast_ths") or []
        if forecast:
            lines.extend(["", "## Consensus EPS Forecast"])
            for row in forecast[:8]:
                lines.append(" | ".join(f"{k}: {_fmt(v)}" for k, v in row.items()))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub fundamentals for {normalized}: {exc}"


def _statement_report(ticker: str, statement_type: str, title: str, freq: str = "quarterly", curr_date: str = None) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        pkg = _package(normalized)
        rows = [row for row in (pkg.get("statements") or []) if row.get("statement_type") == statement_type]
        if curr_date:
            rows = _filter_by_date(rows, "report_date", None, curr_date)
        if freq and freq.lower() == "annual":
            rows = [row for row in rows if str(row.get("report_date", ""))[5:7] == "12"]
        if not rows:
            return f"No DataHub {title} data found for {normalized}."
        flat = []
        for row in rows[:8]:
            raw = row.get("raw") or {}
            item = {"report_date": row.get("report_date"), "period": row.get("period"), "source": row.get("source")}
            item.update(raw)
            flat.append(item)
        return (
            f"# {title} for {normalized} ({freq})\n"
            "# Data source: FinDataHub\n\n"
            f"{pd.DataFrame(flat).to_csv(index=False)}"
        )
    except Exception as exc:
        return f"Error retrieving DataHub {title} for {normalized}: {exc}"


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_report(ticker, "balance", "Balance Sheet", freq, curr_date)


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_report(ticker, "cashflow", "Cash Flow", freq, curr_date)


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_report(ticker, "income", "Income Statement", freq, curr_date)


def get_news(
    ticker: Annotated[str, "A-stock code"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        payload = _get(
            "/api/news/search",
            {},
            {
                "ticker": normalized,
                "start_date": start_date,
                "end_date": end_date,
                "limit": 30,
                "include_web": True,
            },
        ) or {}
        rows = payload.get("items") or []
        if not rows:
            return f"No DataHub/MindSpider news found for {normalized} between {start_date} and {end_date}."
        meta = payload.get("meta") or {}
        providers = ", ".join(meta.get("providers") or ["FinDataHub"])
        lines = [f"# News for {normalized}", f"# Data source: {providers}", ""]
        for row in rows[:30]:
            lines.append(
                f"- {row.get('published_at') or row.get('fetched_at')}: {row.get('title')} "
                f"({row.get('source') or row.get('provider') or 'unknown'}) {row.get('url') or ''}\n"
                f"  {row.get('summary') or ''}\n"
                f"  relevance: {row.get('relevance_reason') or 'matched'}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub news for {normalized}: {exc}"


def get_global_news(
    curr_date: Annotated[str, "current date"],
    look_back_days: Annotated[int, "days back"] = 7,
    limit: Annotated[int, "max news count"] = 5,
) -> str:
    try:
        payload = _get(
            "/api/news/search",
            {},
            {"query": "A股 市场 热点 行业 主线", "end_date": curr_date, "limit": limit, "include_web": True},
        ) or {}
        news_rows = payload.get("items") or []
        rows = []
        for event in news_rows[:limit]:
            rows.append(
                f"- {event.get('published_at')}: {event.get('title')} "
                f"({event.get('source') or event.get('provider')}) {event.get('url') or ''}\n"
                f"  {event.get('summary') or ''}"
            )
        if not rows:
            overview = _get("/api/market/overview", {}) or {}
            indices = overview.get("indices") or []
            sectors = overview.get("sectors") or []
            for row in (indices[:2] + sectors[:2]):
                rows.append(
                    f"- {row.get('name')}: price={_fmt(row.get('price'))}, "
                    f"change_pct={_fmt(row.get('change_pct'))}, amount={_fmt(row.get('amount'))}"
                )
        if not rows:
            return _datahub_missing("Global / Market Context News")
        lines = [f"# Market Context from DataHub ({curr_date})", "# Data source: FinDataHub", ""]
        lines.extend(rows[:limit])
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub market context: {exc}"


def get_insider_transactions(ticker: Annotated[str, "A-stock code"]) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        pkg = _package(normalized)
        news = pkg.get("news") or []
        keywords = ("增持", "减持", "回购", "股东", "董监高")
        rows = [row for row in news if any(k in f"{row.get('title','')}{row.get('summary','')}" for k in keywords)]
        if not rows:
            return f"No DataHub shareholder/insider-related news found for {normalized}."
        lines = [f"# Shareholder / Insider Related News for {normalized}", "# Data source: FinDataHub", ""]
        for row in rows[:10]:
            lines.append(f"- {row.get('published_at')}: {row.get('title')} {row.get('url') or ''}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub insider-related news for {normalized}: {exc}"


def get_profit_forecast(ticker: Annotated[str, "A-stock code"]) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        forecast = (_raw_metric(_package(normalized)).get("profit_forecast_ths") or [])
        if not forecast:
            return f"No DataHub profit forecast found for {normalized}."
        return (
            f"# Profit Forecast for {normalized}\n"
            "# Data source: FinDataHub\n\n"
            f"{pd.DataFrame(forecast).to_csv(index=False)}"
        )
    except Exception as exc:
        return f"Error retrieving DataHub profit forecast for {normalized}: {exc}"


def get_hot_stocks(curr_date: Annotated[str, "Date in YYYY-MM-DD format, empty for today"] = "") -> str:
    try:
        package = _market_overview_package()
        hot = package.get("hot_stocks") or {}
        rank_rows = hot.get("rank") or []
        up_rows = hot.get("up") or []
        if not rank_rows and not up_rows:
            overview = package.get("overview") or {}
            rows = overview.get("sectors") or []
            if not rows:
                return _datahub_missing("Hot Stocks")
            lines = [f"# Strong Sectors from DataHub ({curr_date or datetime.now().strftime('%Y-%m-%d')})", "", _records_to_markdown(rows, ["name", "change_pct", "amount", "leader"], 20)]
            return "\n".join(lines)
        lines = [f"# Hot Stocks from DataHub ({curr_date or datetime.now().strftime('%Y-%m-%d')})", "# Data source: FinDataHub", ""]
        if rank_rows:
            lines.append("## Rank")
            lines.append(_records_to_markdown(rank_rows, ["当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"], 20))
        if up_rows:
            lines.append("")
            lines.append("## Up Moves")
            lines.append(_records_to_markdown(up_rows, ["排名较昨日变动", "当前排名", "代码", "股票名称", "最新价", "涨跌额", "涨跌幅"], 20))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub sector context: {exc}"


def get_northbound_flow(
    curr_date: Annotated[str, "Date in YYYY-MM-DD format"],
    include_history: Annotated[bool, "Include historical daily data"] = False,
) -> str:
    try:
        package = _market_overview_package()
        overview = package.get("overview") or {}
        fund_flow = overview.get("fund_flow") or {}
        northbound = package.get("northbound_flow") or {}
        history = northbound.get("history") or []
        if not fund_flow and not history:
            return _datahub_missing("Northbound Flow")
        lines = [f"# Northbound Flow ({curr_date})", "# Data source: FinDataHub", ""]
        if fund_flow:
            lines.append(f"北向资金净流入: {_fmt(fund_flow.get('northbound_net_amount'))}")
            lines.append(f"主力净流入: {_fmt(fund_flow.get('main_net_inflow'))}")
            lines.append(f"超大单净流入: {_fmt(fund_flow.get('super_large_net_inflow'))}")
            lines.append(f"大单净流入: {_fmt(fund_flow.get('large_net_inflow'))}")
            lines.append(f"中单净流入: {_fmt(fund_flow.get('medium_net_inflow'))}")
            lines.append(f"小单净流入: {_fmt(fund_flow.get('small_net_inflow'))}")
        if include_history and history:
            lines.append("")
            lines.append("History:")
            lines.append(_records_to_markdown(history, ["日期", "当日成交净买额", "买入成交额", "卖出成交额", "历史累计净买额", "当日资金流入", "当日余额", "持股市值", "领涨股", "领涨股-涨跌幅"], 20))
        elif history:
            latest = history[0]
            lines.append("")
            lines.append(
                f"Latest history: 日期={_fmt(latest.get('日期'))}, 当日成交净买额={_fmt(latest.get('当日成交净买额'))}, "
                f"当日资金流入={_fmt(latest.get('当日资金流入'))}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub northbound flow: {exc}"


def get_market_package(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date in YYYY-MM-DD format, empty for latest"] = "",
    overview_limit: Annotated[int, "Market overview rows to include"] = 10,
    news_limit: Annotated[int, "Market news/event rows to include"] = 8,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized, trade_date=curr_date or None, overview_limit=overview_limit, news_limit=news_limit)
        return _format_market_package(package)
    except Exception as exc:
        return f"Error retrieving DataHub market package for {normalized}: {exc}"


def get_concept_blocks(ticker: Annotated[str, "A-stock code"]) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized)
        concept = package.get("concept_blocks") or {}
        profile = concept.get("profile") or {}
        lines = [f"# Concept / Sector Blocks for {normalized}", "# Data source: FinDataHub", ""]
        lines.append(f"Industry: {_fmt(concept.get('industry') or profile.get('行业'))}")
        lines.append(f"List date: {_fmt(concept.get('list_date') or profile.get('上市时间'))}")
        lines.append(f"Market cap: {_fmt(concept.get('market_cap') or profile.get('总市值'))}")
        lines.append(f"Float market cap: {_fmt(concept.get('float_market_cap') or profile.get('流通市值'))}")
        if concept.get("sector"):
            lines.append(f"Sector: {_fmt(concept.get('sector'))}")
        top_concepts = concept.get("top_concepts") or []
        if top_concepts:
            lines.append("")
            lines.append("Top concepts:")
            for row in top_concepts[:10]:
                lines.append(f"- {row.get('name')}: change_pct={_fmt(row.get('change_pct'))}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub concept blocks for {normalized}: {exc}"


def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[bool, "Include historical daily fund flow"] = True,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized, trade_date=curr_date)
        flow = package.get("fund_flow") or {}
        history = flow.get("history") or []
        if not history:
            return _datahub_missing(f"Fund Flow for {normalized}")
        lines = [f"# Fund Flow for {normalized}", "# Data source: FinDataHub", ""]
        latest = history[0]
        lines.append(f"Latest date: {_fmt(latest.get('日期'))}")
        lines.append(f"主力净流入-净额: {_fmt(latest.get('主力净流入-净额'))}")
        lines.append(f"超大单净流入-净额: {_fmt(latest.get('超大单净流入-净额'))}")
        lines.append(f"大单净流入-净额: {_fmt(latest.get('大单净流入-净额'))}")
        lines.append(f"中单净流入-净额: {_fmt(latest.get('中单净流入-净额'))}")
        lines.append(f"小单净流入-净额: {_fmt(latest.get('小单净流入-净额'))}")
        if flow.get("market_rank"):
            rank = flow["market_rank"]
            lines.append(f"Market rank: {rank.get('今日排行榜-今日排名')} / {rank.get('所属板块')}")
        if include_history and len(history) > 1:
            lines.append("")
            lines.append("History:")
            lines.append(_records_to_markdown(history, ["日期", "收盘价", "涨跌幅", "主力净流入-净额", "超大单净流入-净额", "大单净流入-净额"], 20))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub fund flow for {normalized}: {exc}"


def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized, trade_date=trade_date)
        board = package.get("dragon_tiger_board") or {}
        rows = board.get("rows") or []
        if not rows:
            return _datahub_missing(f"Dragon Tiger Board for {normalized}")
        lines = [f"# Dragon Tiger Board for {normalized}", "# Data source: FinDataHub", ""]
        lines.append(_records_to_markdown(rows, ["上榜日", "解读", "龙虎榜净买额", "龙虎榜买入额", "龙虎榜卖出额", "换手率", "上榜原因"], 20))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub dragon tiger board for {normalized}: {exc}"


def get_lockup_expiry(
    ticker: str,
    trade_date: str,
    forward_days: int = 90,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized, trade_date=trade_date)
        lockup = package.get("lockup_expiry") or {}
        queue = lockup.get("queue") or []
        detail = lockup.get("detail") or []
        if not queue and not detail:
            return _datahub_missing(f"Lockup Expiry for {normalized}")
        lines = [f"# Lockup Expiry for {normalized}", "# Data source: FinDataHub", ""]
        if queue:
            lines.append("Upcoming queue:")
            lines.append(_records_to_markdown(queue, ["解禁时间", "解禁股东数", "解禁数量", "实际解禁数量", "占流通市值比例", "限售股类型"], 10))
        if detail:
            lines.append("")
            lines.append("Detail:")
            lines.append(_records_to_markdown(detail, ["解禁时间", "限售股类型", "解禁数量", "实际解禁数量", "实际解禁市值", "占解禁前流通市值比例"], 10))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub lockup expiry for {normalized}: {exc}"


def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    normalized = _normalize_ticker(ticker)
    try:
        package = _market_package(normalized, trade_date=trade_date)
        comp = package.get("industry_comparison") or {}
        rows = comp.get("rows") or []
        if not rows:
            return f"No DataHub industry comparison data found for {normalized}."
        lines = [f"# Industry Comparison for {normalized} on {trade_date}", "# Data source: FinDataHub", ""]
        lines.append(f"Industry: {_fmt(comp.get('industry_name'))}")
        target = comp.get("target") or {}
        if target:
            lines.append(f"Target: {target.get('名称')} change_pct={_fmt(target.get('涨跌幅'))}")
        lines.append("")
        lines.append(_records_to_markdown(rows, ["名称", "涨跌幅", "涨跌额", "成交额", "换手率", "市盈率-动态", "市净率"], top_n))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error retrieving DataHub industry comparison for {normalized}: {exc}"


def datahub_close_series(ticker: str) -> list[tuple[datetime, float]]:
    """Utility used outside LangChain tools for return calculation."""
    pkg = _package(ticker)
    rows = pkg.get("daily_qfq") or pkg.get("daily") or []
    series = []
    for row in rows:
        dt = _parse_date(row.get("trade_date"))
        close = row.get("close")
        if dt is not None and close is not None:
            series.append((dt.to_pydatetime(), float(close)))
    return series


def datahub_forward_return(ticker: str, trade_date: str, holding_days: int = 5) -> tuple[float | None, int | None]:
    start = _parse_date(trade_date)
    if start is None:
        return None, None
    end = start + timedelta(days=holding_days + 7)
    rows = [(dt, close) for dt, close in datahub_close_series(ticker) if start <= pd.Timestamp(dt) <= end]
    if len(rows) < 2:
        return None, None
    actual_days = min(holding_days, len(rows) - 1)
    first = rows[0][1]
    last = rows[actual_days][1]
    if first == 0:
        return None, None
    return (last - first) / first, actual_days
