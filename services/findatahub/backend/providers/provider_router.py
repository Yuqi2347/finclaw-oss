from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from .tushare_provider import TushareProvider


SUPPORTED_TUSHARE_APIS: frozenset[str] = frozenset(
    {
        "stock_basic",
        "stock_company",
        "trade_cal",
        "daily",
        "adj_factor",
        "daily_basic",
        "moneyflow",
        "stk_limit",
        "fina_indicator",
        "income",
        "balancesheet",
        "cashflow",
        "forecast",
        "express",
        "dividend",
        "disclosure_date",
        "index_basic",
        "index_daily",
        "daily_info",
        "moneyflow_hsgt",
        "cn_gdp",
        "cn_cpi",
        "cn_ppi",
        "cn_m",
        "cn_pmi",
        "shibor",
    }
)


@dataclass
class ProviderRouter:
    profile: str = settings.provider_profile

    def __post_init__(self) -> None:
        self.profile = (self.profile or "free").strip().lower()
        self.tushare = TushareProvider()

    @property
    def use_tushare(self) -> bool:
        return self.profile == "tushare" and self.tushare.available

    def require_tushare(self, api_name: str) -> TushareProvider:
        if api_name not in SUPPORTED_TUSHARE_APIS:
            raise ValueError(f"Tushare api disabled by FinDataHub capability map: {api_name}")
        if not self.use_tushare:
            raise RuntimeError("Tushare provider profile is not enabled or token is missing")
        return self.tushare
