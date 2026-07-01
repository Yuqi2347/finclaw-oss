from typing import Annotated

from .a_stock import (
    get_stock_data as get_astock_stock_data,
    get_indicators as get_astock_indicators,
    get_fundamentals as get_astock_fundamentals,
    get_balance_sheet as get_astock_balance_sheet,
    get_cashflow as get_astock_cashflow,
    get_income_statement as get_astock_income_statement,
    get_news as get_astock_news,
    get_global_news as get_astock_global_news,
    get_insider_transactions as get_astock_insider_transactions,
    get_profit_forecast as get_astock_profit_forecast,
    get_market_package as get_astock_market_package,
    get_hot_stocks as get_astock_hot_stocks,
    get_northbound_flow as get_astock_northbound_flow,
    get_concept_blocks as get_astock_concept_blocks,
    get_fund_flow as get_astock_fund_flow,
    get_dragon_tiger_board as get_astock_dragon_tiger_board,
    get_lockup_expiry as get_astock_lockup_expiry,
    get_industry_comparison as get_astock_industry_comparison,
)

# Configuration and routing logic
from .config import get_config

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "signal_data": {
        "description": "A-stock signal layer (topic attribution, capital flow, consensus forecast, market package)",
        "tools": [
            "get_profit_forecast",
            "get_market_package",
            "get_hot_stocks",
            "get_northbound_flow",
            "get_concept_blocks",
            "get_fund_flow",
            "get_dragon_tiger_board",
            "get_lockup_expiry",
            "get_industry_comparison",
        ]
    }
}

VENDOR_LIST = [
    "a_stock",
]

ASTOCK_BOUND_TICKER_METHODS = {
    "get_stock_data",
    "get_indicators",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_insider_transactions",
    "get_profit_forecast",
    "get_market_package",
    "get_concept_blocks",
    "get_fund_flow",
    "get_dragon_tiger_board",
    "get_lockup_expiry",
    "get_industry_comparison",
}

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "a_stock": get_astock_stock_data,
    },
    # technical_indicators
    "get_indicators": {
        "a_stock": get_astock_indicators,
    },
    # fundamental_data
    "get_fundamentals": {
        "a_stock": get_astock_fundamentals,
    },
    "get_balance_sheet": {
        "a_stock": get_astock_balance_sheet,
    },
    "get_cashflow": {
        "a_stock": get_astock_cashflow,
    },
    "get_income_statement": {
        "a_stock": get_astock_income_statement,
    },
    # news_data
    "get_news": {
        "a_stock": get_astock_news,
    },
    "get_global_news": {
        "a_stock": get_astock_global_news,
    },
    "get_insider_transactions": {
        "a_stock": get_astock_insider_transactions,
    },
    # signal_data (A-stock only)
    "get_profit_forecast": {
        "a_stock": get_astock_profit_forecast,
    },
    "get_market_package": {
        "a_stock": get_astock_market_package,
    },
    "get_hot_stocks": {
        "a_stock": get_astock_hot_stocks,
    },
    "get_northbound_flow": {
        "a_stock": get_astock_northbound_flow,
    },
    "get_concept_blocks": {
        "a_stock": get_astock_concept_blocks,
    },
    "get_fund_flow": {
        "a_stock": get_astock_fund_flow,
    },
    "get_dragon_tiger_board": {
        "a_stock": get_astock_dragon_tiger_board,
    },
    "get_lockup_expiry": {
        "a_stock": get_astock_lockup_expiry,
    },
    "get_industry_comparison": {
        "a_stock": get_astock_industry_comparison,
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to the A-share DataHub vendor."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]
    config = get_config()

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    for vendor in primary_vendors:
        if vendor not in VENDOR_METHODS[method]:
            continue

        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        routed_args = _bind_astock_ticker(method, vendor, args, config)
        return impl_func(*routed_args, **kwargs)

    raise RuntimeError(f"No available vendor for '{method}'")


def _bind_astock_ticker(method: str, vendor: str, args: tuple, config: dict) -> tuple:
    if vendor != "a_stock" or method not in ASTOCK_BOUND_TICKER_METHODS:
        return args
    active_ticker = config.get("active_ticker")
    if not active_ticker or not args:
        return args
    return (active_ticker, *args[1:])
