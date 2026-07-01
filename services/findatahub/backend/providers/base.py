from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    api_name: str
    rows: list[dict[str, Any]]
    fields: list[str]
    duration_ms: int
    status: str = "ok"
    error_code: str | None = None
    message: str | None = None


class ProviderError(RuntimeError):
    def __init__(self, provider: str, api_name: str, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.api_name = api_name
        self.error_code = error_code
