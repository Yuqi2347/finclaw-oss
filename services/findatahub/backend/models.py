from datetime import datetime, date
from sqlalchemy import Date, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    market: Mapped[str] = mapped_column(String(32), default="A股")
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    list_name: Mapped[str] = mapped_column(String(64), default="默认关注")
    status: Mapped[str] = mapped_column(String(32), default="观察")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "list_name", name="uq_watchlist_ticker_list"),)


class PriceDaily(Base):
    __tablename__ = "price_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    adjustment: Mapped[str] = mapped_column(String(16), default="qfq", index=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "trade_date", "adjustment", name="uq_daily_ticker_date_adj"),)


class ValuationDaily(Base):
    __tablename__ = "valuation_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ps: Mapped[float | None] = mapped_column(Float, nullable=True)
    ps_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dv_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_mv: Mapped[float | None] = mapped_column(Float, nullable=True)
    circ_mv: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "trade_date", name="uq_valuation_ticker_date"),)


class MoneyflowDaily(Base):
    __tablename__ = "moneyflow_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    buy_sm_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_sm_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_md_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_md_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_lg_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_lg_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_elg_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_elg_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_mf_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "trade_date", name="uq_moneyflow_ticker_date"),)


class LimitPriceDaily(Base):
    __tablename__ = "limit_price_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    up_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    down_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "trade_date", name="uq_limit_price_ticker_date"),)


class IndexDaily(Base):
    __tablename__ = "index_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("symbol", "trade_date", name="uq_index_daily_symbol_date"),)


class PriceRealtimeSnapshot(Base):
    __tablename__ = "price_realtime_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TechnicalIndicator(Base):
    __tablename__ = "technical_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    adjustment: Mapped[str] = mapped_column(String(16), default="qfq", index=True)
    ma5: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma60: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi14: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_hist: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr14: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_ub: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_lb: Mapped[float | None] = mapped_column(Float, nullable=True)
    vwma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "trade_date", "adjustment", name="uq_indicator_ticker_date_adj"),)


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketNewsArticle(Base):
    __tablename__ = "market_news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    news_id: Mapped[str] = mapped_column(String(160), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="newsnow")
    source_platform: Mapped[str] = mapped_column(String(64), index=True)
    source_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawl_date: Mapped[date] = mapped_column(Date, index=True)
    rank_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("news_id", "crawl_date", name="uq_market_news_id_date"),)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    cost_price: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyPlan(Base):
    __tablename__ = "daily_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    plan_date: Mapped[date] = mapped_column(Date, index=True)
    bias: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_levels: Mapped[str | None] = mapped_column(Text, nullable=True)
    resistance_levels: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_zone: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "plan_date", name="uq_daily_plan_ticker_date"),)


class TriggerRule(Base):
    __tablename__ = "trigger_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    rule_type: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(16))
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TriggerEvent(Base):
    __tablename__ = "trigger_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    area: Mapped[str | None] = mapped_column(String(128), nullable=True)
    market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    list_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FundamentalMetric(Base):
    __tablename__ = "fundamental_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    period: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "report_date", name="uq_fundamental_ticker_report_date"),)


class FinancialStatement(Base):
    __tablename__ = "financial_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    statement_type: Mapped[str] = mapped_column(String(32), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    period: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("ticker", "statement_type", "report_date", name="uq_statement_ticker_type_date"),)


class DataAvailability(Base):
    __tablename__ = "data_availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="unknown")
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    as_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (UniqueConstraint("scope", "key", "dataset", name="uq_data_availability_scope_key_dataset"),)


class ProviderUsageLog(Base):
    __tablename__ = "provider_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    api_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("symbol", "category", name="uq_market_snapshot_symbol_category"),)


class MarketBreadthSnapshot(Base):
    __tablename__ = "market_breadth_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(32), default="A股", index=True)
    up_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    down_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flat_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_up_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_down_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strong_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weak_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    median_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_amount_billion: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MarketFundFlowSnapshot(Base):
    __tablename__ = "market_fund_flow_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(32), default="A股", index=True)
    northbound_net_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    main_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    super_large_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    large_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    medium_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    small_net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ThemeSnapshot(Base):
    __tablename__ = "theme_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    theme_code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="concept", index=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    leader: Mapped[str | None] = mapped_column(String(128), nullable=True)
    heat_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("theme_code", "category", name="uq_theme_snapshot_code_category"),)


class MarketEvent(Base):
    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    source: Mapped[str] = mapped_column(String(32), default="system")
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SpecialTradingData(Base):
    __tablename__ = "special_trading_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    data_type: Mapped[str] = mapped_column(String(64), index=True)
    trade_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RefreshLog(Base):
    __tablename__ = "refresh_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    target_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
