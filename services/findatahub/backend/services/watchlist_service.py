from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from ..providers.symbol import normalize_a_share_ticker
from ..schemas import WatchlistCreate


class WatchlistService:
    def add_item(self, db: Session, payload: WatchlistCreate) -> models.WatchlistItem:
        ticker = normalize_a_share_ticker(payload.ticker)
        item = db.query(models.WatchlistItem).filter(
            models.WatchlistItem.ticker == ticker,
            models.WatchlistItem.list_name == payload.list_name,
        ).first()
        if not item:
            item = models.WatchlistItem(ticker=ticker, list_name=payload.list_name)
            db.add(item)
        item.name = payload.name
        item.status = payload.status
        item.reason = payload.reason
        db.commit()
        db.refresh(item)
        return item

    def list_items(self, db: Session) -> list[models.WatchlistItem]:
        return db.query(models.WatchlistItem).order_by(models.WatchlistItem.created_at.desc()).all()

    def delete_item(self, db: Session, ticker: str, list_name: str = "默认关注") -> bool:
        normalized = normalize_a_share_ticker(ticker)
        item = db.query(models.WatchlistItem).filter(
            models.WatchlistItem.ticker == normalized,
            models.WatchlistItem.list_name == list_name,
        ).first()
        if not item:
            return False
        db.delete(item)
        db.commit()
        return True

