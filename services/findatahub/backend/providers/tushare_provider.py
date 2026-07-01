from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from ..config import settings
from .base import ProviderError, ProviderResult


class TushareProvider:
    source = "tushare"

    def __init__(
        self,
        token: str | None = None,
        api_url: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.token = (token if token is not None else settings.tushare_pro_token).strip()
        self.api_url = api_url or settings.tushare_api_url
        self.timeout = timeout or settings.tushare_timeout
        self.session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.token)

    def call(
        self,
        api_name: str,
        params: dict[str, Any] | None = None,
        fields: str | list[str] | None = None,
    ) -> ProviderResult:
        if not self.token:
            raise ProviderError(self.source, api_name, "missing FINDATAHUB_TUSHARE_PRO_TOKEN", "missing_token")

        started_at = datetime.utcnow()
        field_text = ",".join(fields) if isinstance(fields, list) else (fields or "")
        payload = {
            "api_name": api_name,
            "token": self.token,
            "params": params or {},
            "fields": field_text,
        }
        try:
            response = self.session.post(self.api_url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ProviderError(self.source, api_name, f"transport error: {exc}", "transport") from exc

        duration_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        code = body.get("code")
        if code != 0:
            msg = str(body.get("msg") or "unknown tushare error")
            raise ProviderError(self.source, api_name, msg, str(code))

        data = body.get("data") or {}
        result_fields = [str(item) for item in (data.get("fields") or [])]
        rows = [dict(zip(result_fields, item)) for item in (data.get("items") or [])]
        return ProviderResult(
            provider=self.source,
            api_name=api_name,
            rows=rows,
            fields=result_fields,
            duration_ms=duration_ms,
            status="ok" if rows else "empty",
        )


def ts_code_to_ticker(ts_code: str) -> str:
    value = str(ts_code or "").strip().upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value
    return value
