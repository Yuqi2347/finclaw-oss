from datetime import date, datetime
from pydantic import BaseModel


class WatchlistCreate(BaseModel):
    ticker: str
    name: str | None = None
    list_name: str = "默认关注"
    status: str = "观察"
    reason: str | None = None


class PositionUpsert(BaseModel):
    ticker: str
    name: str | None = None
    quantity: float = 0
    cost_price: float = 0
    note: str | None = None


class PositionPatch(BaseModel):
    name: str | None = None
    quantity: float | None = None
    cost_price: float | None = None
    note: str | None = None


class DailyPlanUpsert(BaseModel):
    ticker: str
    plan_date: date
    bias: str | None = None
    expected_path: str | None = None
    support_levels: str | None = None
    resistance_levels: str | None = None
    stop_loss: float | None = None
    target_zone: str | None = None
    notes: str | None = None


class TriggerRuleCreate(BaseModel):
    ticker: str
    level: int = 1
    rule_type: str
    operator: str
    threshold: float | None = None
    description: str | None = None
    enabled: int = 1


class TriggerEventStatusUpdate(BaseModel):
    status: str


class RefreshRequest(BaseModel):
    ticker: str
    start_date: str | None = None
    end_date: str | None = None


class MarketPackageRefreshRequest(BaseModel):
    trade_date: str | None = None


class InstrumentIndexRefreshRequest(BaseModel):
    limit: int | None = None


class DashboardSummary(BaseModel):
    watchlist_count: int
    position_count: int
    trigger_count: int
    last_updated: datetime | None = None
