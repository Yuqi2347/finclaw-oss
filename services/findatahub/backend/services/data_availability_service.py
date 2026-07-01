from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import models


class DataAvailabilityService:
    def mark(
        self,
        db: Session,
        *,
        scope: str,
        key: str,
        dataset: str,
        provider: str,
        status: str,
        row_count: int = 0,
        as_of: str | None = None,
        missing_reason: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> models.DataAvailability:
        row = (
            db.query(models.DataAvailability)
            .filter(
                models.DataAvailability.scope == scope,
                models.DataAvailability.key == key,
                models.DataAvailability.dataset == dataset,
            )
            .first()
        )
        if not row:
            row = models.DataAvailability(scope=scope, key=key, dataset=dataset)
            db.add(row)
        row.provider = provider
        row.status = status
        row.row_count = row_count
        row.as_of = as_of
        row.missing_reason = missing_reason
        row.raw = raw
        row.fetched_at = datetime.utcnow()
        db.flush()
        return row

    def latest_for_key(self, db: Session, scope: str, key: str) -> list[models.DataAvailability]:
        return (
            db.query(models.DataAvailability)
            .filter(models.DataAvailability.scope == scope, models.DataAvailability.key == key)
            .order_by(models.DataAvailability.dataset.asc())
            .all()
        )


class ProviderUsageService:
    def log(
        self,
        db: Session,
        *,
        provider: str,
        api_name: str,
        status: str,
        row_count: int = 0,
        duration_ms: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> models.ProviderUsageLog:
        row = models.ProviderUsageLog(
            provider=provider,
            api_name=api_name,
            status=status,
            row_count=row_count,
            duration_ms=duration_ms,
            error_code=error_code,
            message=message,
            request=request,
        )
        db.add(row)
        db.flush()
        return row
