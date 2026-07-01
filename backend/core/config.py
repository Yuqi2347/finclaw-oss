from __future__ import annotations

from backend.core.env import settings


PROJECT_ROOT = settings.project_root
FINAGENT_ROOT = settings.finagent_root

DATA_DIR = PROJECT_ROOT / "backend" / "data"
ASSISTANT_DB = DATA_DIR / "finclaw.sqlite"

DATAHUB_BASE_URL = settings.datahub_base_url
FINCLAW_API_BASE_URL = settings.finclaw_api_base_url

BETTAFISH_ROOT = settings.bettafish_root
TRADINGAGENTS_ROOT = settings.tradingagents_root

BETTAFISH_MARKET_REPORT_ROOT = BETTAFISH_ROOT / "runtime" / "market_discovery"
TRADING_REPORT_ROOT = TRADINGAGENTS_ROOT / "runtime"

DEFAULT_PYTHON = settings.python_executable
