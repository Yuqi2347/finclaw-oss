from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..config import settings

logger = logging.getLogger(__name__)


class RuntimeMaintenanceService:
    """DataHub retention policy for volatile provider/runtime rows."""

    def run_once(self, db: Session) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        try:
            summary.update(self.cleanup_rows(db))
            db.commit()
            summary["vacuum"] = self.maybe_vacuum(db, deleted_rows=sum(int(v or 0) for v in summary.values()))
        except Exception:
            db.rollback()
            raise
        logger.info("FinDataHub runtime maintenance completed: %s", summary)
        return summary

    def cleanup_rows(self, db: Session) -> dict[str, int]:
        now = datetime.utcnow()
        news_cutoff = now - timedelta(days=14)
        market_news_cutoff = date.today() - timedelta(days=2)
        log_cutoff = now - timedelta(days=30)
        return {
            "news_articles": int(
                db.query(models.NewsArticle)
                .filter(models.NewsArticle.fetched_at < news_cutoff)
                .delete(synchronize_session=False)
                or 0
            ),
            "market_news_articles": int(
                db.query(models.MarketNewsArticle)
                .filter(models.MarketNewsArticle.crawl_date < market_news_cutoff)
                .delete(synchronize_session=False)
                or 0
            ),
            "provider_usage_logs": int(
                db.query(models.ProviderUsageLog)
                .filter(models.ProviderUsageLog.created_at < log_cutoff)
                .delete(synchronize_session=False)
                or 0
            ),
            "refresh_logs": int(
                db.query(models.RefreshLog)
                .filter(models.RefreshLog.created_at < log_cutoff)
                .delete(synchronize_session=False)
                or 0
            ),
            "trigger_events": int(
                db.query(models.TriggerEvent)
                .filter(models.TriggerEvent.created_at < log_cutoff)
                .delete(synchronize_session=False)
                or 0
            ),
        }

    def maybe_vacuum(self, db: Session, deleted_rows: int) -> str:
        db_path = _sqlite_db_path()
        if db_path is None or not db_path.exists() or db_path.stat().st_size < 100 * 1024 * 1024:
            return "skipped"
        if deleted_rows < 1000:
            return "skipped"
        marker = db_path.parent / ".last_vacuum"
        if marker.exists():
            last_vacuum = datetime.fromtimestamp(marker.stat().st_mtime)
            if datetime.now() - last_vacuum < timedelta(days=30):
                return "skipped"
        db.commit()
        with sqlite3.connect(db_path) as conn:
            conn.execute("VACUUM")
        marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        return "done"


def _sqlite_db_path() -> Path | None:
    if not settings.db_url.startswith("sqlite:///"):
        return None
    return Path(settings.db_url.replace("sqlite:///", "", 1))


runtime_maintenance_service = RuntimeMaintenanceService()
