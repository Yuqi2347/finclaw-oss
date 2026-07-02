from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _resolve_repo_path(value: str | None, default: Path) -> Path:
    raw = (value or "").strip()
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    datahub_mode: str
    datahub_mount_path: str
    datahub_base_url: str
    finclaw_api_base_url: str
    bettafish_root: Path
    tradingagents_root: Path
    python_executable: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_timeout: int
    llm_thinking: str
    run_lock_ttl_seconds: int
    web_search_enabled: bool
    search_provider_order: str
    tavily_api_key: str
    brave_search_api_key: str
    exa_api_key: str
    serpapi_api_key: str
    serper_api_key: str
    you_api_key: str
    web_search_max_sources: int
    web_fetch_timeout: int
    web_total_timeout: int

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def llm_thinking_payload(self) -> dict[str, str] | None:
        model = (self.llm_model or "").strip().lower()
        if "flash" in model:
            return None
        value = (self.llm_thinking or "").strip()
        if not value or value.lower() in {"0", "false", "none", "off", "disabled"}:
            return None
        return {"type": value}

    @property
    def llm_request_thinking_payload(self) -> dict[str, str] | None:
        enabled_payload = self.llm_thinking_payload
        if enabled_payload is not None:
            return enabled_payload
        if self._uses_deepseek_api:
            return {"type": "disabled"}
        return None

    @property
    def _uses_deepseek_api(self) -> bool:
        base_url = (self.llm_base_url or "").lower()
        model = (self.llm_model or "").lower()
        return "deepseek" in base_url or model.startswith("deepseek-")


def load_settings() -> Settings:
    datahub_mode = os.getenv("FINCLAW_DATAHUB_MODE", "embedded").strip().lower()
    datahub_mount_path = os.getenv("FINCLAW_DATAHUB_MOUNT_PATH", "/datahub").strip() or "/datahub"
    if not datahub_mount_path.startswith("/"):
        datahub_mount_path = f"/{datahub_mount_path}"
    finclaw_api_base_url = os.getenv("FINCLAW_API_BASE_URL", "http://127.0.0.1:8800").rstrip("/")
    if datahub_mode == "embedded":
        datahub_base_url = f"{finclaw_api_base_url}{datahub_mount_path}"
    else:
        datahub_base_url = os.getenv("DATAHUB_BASE_URL", "http://127.0.0.1:8700").rstrip("/")
    return Settings(
        project_root=PROJECT_ROOT,
        datahub_mode=datahub_mode,
        datahub_mount_path=datahub_mount_path,
        datahub_base_url=datahub_base_url,
        finclaw_api_base_url=finclaw_api_base_url,
        bettafish_root=_resolve_repo_path(os.getenv("BETTAFISH_ROOT"), PROJECT_ROOT / "capabilities" / "bettafish"),
        tradingagents_root=_resolve_repo_path(os.getenv("TRADINGAGENTS_ROOT"), PROJECT_ROOT / "capabilities" / "tradingagents_astock"),
        python_executable=os.getenv("FINCLAW_PYTHON", "python"),
        llm_api_key=os.getenv("FINCLAW_LLM_API_KEY", ""),
        llm_base_url=os.getenv("FINCLAW_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("FINCLAW_LLM_MODEL", "gpt-4.1-mini"),
        llm_temperature=_get_float("FINCLAW_LLM_TEMPERATURE", 0.2),
        llm_timeout=_get_int("FINCLAW_LLM_TIMEOUT", 120),
        llm_thinking=os.getenv("FINCLAW_LLM_THINKING", "disabled"),
        run_lock_ttl_seconds=_get_int("FINCLAW_RUN_LOCK_TTL_SECONDS", 180),
        web_search_enabled=os.getenv("FINCLAW_WEB_SEARCH_ENABLED", "true").strip().lower() not in {"0", "false", "off", "disabled"},
        search_provider_order=os.getenv("FINCLAW_SEARCH_PROVIDER_ORDER", "tavily,exa,brave"),
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        brave_search_api_key=os.getenv("BRAVE_SEARCH_API_KEY", ""),
        exa_api_key=os.getenv("EXA_API_KEY", ""),
        serpapi_api_key=os.getenv("SERPAPI_API_KEY", ""),
        serper_api_key=os.getenv("SERPER_API_KEY", ""),
        you_api_key=os.getenv("YOU_API_KEY", ""),
        web_search_max_sources=_get_int("FINCLAW_WEB_SEARCH_MAX_SOURCES", 4),
        web_fetch_timeout=_get_int("FINCLAW_WEB_FETCH_TIMEOUT", 5),
        web_total_timeout=_get_int("FINCLAW_WEB_TOTAL_TIMEOUT", 10),
    )


settings = load_settings()
