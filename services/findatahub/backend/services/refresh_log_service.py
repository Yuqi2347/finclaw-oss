from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .. import models
from ..json_utils import sanitize_json_dict
from ..providers.symbol import normalize_a_share_ticker


class RefreshLogService:
    def log(
        self,
        db: Session,
        job_type: str,
        status: str,
        ticker: str | None = None,
        target_scope: str | None = None,
        source: str | None = None,
        message: str | None = None,
        duration_ms: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        raw: dict | None = None,
    ) -> models.RefreshLog:
        normalized = normalize_a_share_ticker(ticker) if ticker else None
        row = models.RefreshLog(
            ticker=normalized,
            job_type=job_type,
            status=status,
            target_scope=target_scope,
            source=source,
            error_code=error_code,
            message=message,
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
            raw=sanitize_json_dict(raw),
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def latest_error(self, db: Session, ticker: str, job_type: str | None = None) -> models.RefreshLog | None:
        normalized = normalize_a_share_ticker(ticker)
        query = db.query(models.RefreshLog).filter(
            models.RefreshLog.ticker == normalized,
            models.RefreshLog.status == "failed",
        )
        if job_type:
            query = query.filter(models.RefreshLog.job_type == job_type)
        return query.order_by(models.RefreshLog.created_at.desc()).first()

    def list_logs(self, db: Session, ticker: str | None = None, limit: int = 100) -> list[models.RefreshLog]:
        query = db.query(models.RefreshLog)
        if ticker:
            query = query.filter(models.RefreshLog.ticker == normalize_a_share_ticker(ticker))
        return query.order_by(models.RefreshLog.created_at.desc()).limit(limit).all()
