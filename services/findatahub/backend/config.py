from pathlib import Path
import os

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

from .providers.network import clear_proxy_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parents[1]


def _resolve_repo_path(value: str | None, default: Path) -> Path:
    raw = (value or "").strip()
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _env_files() -> tuple[Path, ...]:
    candidates: list[Path] = []
    for name in ("FINDATAHUB_ENV_FILE", "FINCLAW_ENV_FILE"):
        raw = os.getenv(name, "").strip()
        if raw:
            candidates.append(Path(raw).expanduser().resolve())
    candidates.extend([
        REPO_ROOT / ".env",
        PROJECT_ROOT / ".env",
    ])
    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return tuple(result)


class Settings(BaseSettings):
    runtime_dir: str = Field(
        default=str(REPO_ROOT / "runtime"),
        validation_alias=AliasChoices("FINCLAW_RUNTIME_DIR", "FINDATAHUB_RUNTIME_DIR"),
    )
    db_url: str = ""
    api_title: str = "FinDataHub"
    disable_proxy: bool = True
    provider_profile: str = "free"
    stock_daily_limit: int = 240
    stock_news_limit: int = 30
    market_theme_limit: int = 50
    market_event_limit: int = 20
    tushare_pro_token: str = ""
    tushare_api_url: str = "http://api.tushare.pro"
    tushare_timeout: int = 20

    class Config:
        env_file = _env_files()
        env_prefix = "FINDATAHUB_"
        extra = "ignore"

    def model_post_init(self, __context) -> None:
        runtime_dir = _resolve_repo_path(self.runtime_dir, REPO_ROOT / "runtime")
        self.runtime_dir = str(runtime_dir)
        if not self.db_url:
            db_path = runtime_dir / "findatahub" / "data" / "findatahub.sqlite"
            self.db_url = f"sqlite:///{db_path.as_posix()}"


settings = Settings()
if not settings.tushare_pro_token:
    settings.tushare_pro_token = os.getenv("TUSHARE_TOKEN", "")


def apply_network_settings() -> None:
    if not settings.disable_proxy:
        return
    clear_proxy_env()
