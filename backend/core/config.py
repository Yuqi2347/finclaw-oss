from __future__ import annotations

from backend.core.env import settings


PROJECT_ROOT = settings.project_root
RUNTIME_DIR = settings.runtime_dir

DATA_DIR = RUNTIME_DIR / "finclaw" / "data"
ASSISTANT_DB = DATA_DIR / "finclaw.sqlite"

DATAHUB_BASE_URL = settings.datahub_base_url
FINCLAW_API_BASE_URL = settings.finclaw_api_base_url

BETTAFISH_ROOT = settings.bettafish_root
TRADINGAGENTS_ROOT = settings.tradingagents_root
CAPABILITY_RUNTIME_DIR = RUNTIME_DIR / "capabilities"

BETTAFISH_MARKET_REPORT_ROOT = CAPABILITY_RUNTIME_DIR / "themeradar" / "market_discovery"
TRADING_REPORT_ROOT = CAPABILITY_RUNTIME_DIR / "equityscope" / "logs"

DEFAULT_PYTHON = settings.python_executable
