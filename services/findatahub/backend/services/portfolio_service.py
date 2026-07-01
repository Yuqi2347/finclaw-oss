from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from ..providers.symbol import normalize_a_share_ticker
from ..schemas import PositionPatch, PositionUpsert


class PortfolioService:
    def upsert_position(self, db: Session, payload: PositionUpsert) -> models.Position | dict[str, object]:
        ticker = normalize_a_share_ticker(payload.ticker)
        if payload.quantity <= 0:
            existing = self._find_position(db, ticker)
            if existing is None:
                return {
                    "status": "ignored",
                    "ticker": ticker,
                    "reason": "quantity is not positive and no active position exists",
                }
            db.delete(existing)
            db.commit()
            return {
                "status": "deleted",
                "ticker": ticker,
                "reason": "quantity is not positive",
            }
        position = db.query(models.Position).filter(models.Position.ticker == ticker).first()
        if not position:
            position = models.Position(ticker=ticker)
            db.add(position)
        position.name = payload.name
        position.quantity = payload.quantity
        position.cost_price = payload.cost_price
        position.note = payload.note
        db.commit()
        db.refresh(position)
        return position

    def list_positions(self, db: Session) -> list[models.Position]:
        return (
            db.query(models.Position)
            .filter(models.Position.quantity > 0)
            .order_by(models.Position.updated_at.desc())
            .all()
        )

    def patch_position(self, db: Session, ticker: str, payload: PositionPatch) -> models.Position | dict[str, object] | None:
        normalized = normalize_a_share_ticker(ticker)
        position = self._find_position(db, normalized)
        if not position:
            return None
        updates = payload.model_dump(exclude_unset=True)
        if "quantity" in updates and updates["quantity"] is not None and updates["quantity"] <= 0:
            db.delete(position)
            db.commit()
            return {
                "status": "deleted",
                "ticker": normalized,
                "reason": "quantity is not positive",
            }
        for key, value in updates.items():
            setattr(position, key, value)
        db.commit()
        db.refresh(position)
        return position

    def delete_position(self, db: Session, ticker: str) -> bool:
        normalized = normalize_a_share_ticker(ticker)
        position = self._find_position(db, normalized)
        if position is None:
            return False
        db.delete(position)
        db.commit()
        return True

    def portfolio_summary(self, db: Session) -> list[dict]:
        positions = self.list_positions(db)
        rows: list[dict] = []
        for position in positions:
            snapshot = db.query(models.PriceRealtimeSnapshot).filter(
                models.PriceRealtimeSnapshot.ticker == position.ticker
            ).first()
            current_price = snapshot.price if snapshot else None
            day_change_pct = snapshot.change_pct if snapshot else None
            day_change_amount = snapshot.change_amount if snapshot else None
            market_value = None
            cost_value = position.quantity * position.cost_price
            pnl = None
            pnl_pct = None
            day_pnl = None
            if current_price is not None:
                market_value = position.quantity * current_price
                pnl = market_value - cost_value
                if cost_value:
                    pnl_pct = pnl / cost_value * 100
            if day_change_amount is not None:
                day_pnl = position.quantity * day_change_amount
            rows.append(
                {
                    "ticker": position.ticker,
                    "name": position.name or (snapshot.name if snapshot else None),
                    "quantity": position.quantity,
                    "cost_price": position.cost_price,
                    "current_price": current_price,
                    "day_change_pct": day_change_pct,
                    "day_change_amount": day_change_amount,
                    "day_pnl": day_pnl,
                    "market_value": market_value,
                    "cost_value": cost_value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "note": position.note,
                    "updated_at": (
                        snapshot.updated_at.isoformat()
                        if snapshot and snapshot.updated_at
                        else (position.updated_at.isoformat() if position.updated_at else None)
                    ),
                }
            )
        return rows

    def _find_position(self, db: Session, ticker: str) -> models.Position | None:
        return (
            db.query(models.Position)
            .filter(models.Position.ticker == ticker, models.Position.quantity > 0)
            .first()
        )
