from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..providers.symbol import normalize_a_share_ticker
from ..schemas import TriggerRuleCreate


class TriggerService:
    dedupe_minutes = 60

    def create_rule(self, db: Session, payload: TriggerRuleCreate) -> models.TriggerRule:
        rule = models.TriggerRule(
            ticker=normalize_a_share_ticker(payload.ticker),
            level=payload.level,
            rule_type=payload.rule_type,
            operator=payload.operator,
            threshold=payload.threshold,
            description=payload.description,
            enabled=payload.enabled,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule

    def evaluate_ticker(self, db: Session, ticker: str) -> list[models.TriggerEvent]:
        normalized = normalize_a_share_ticker(ticker)
        snapshot = db.query(models.PriceRealtimeSnapshot).filter(
            models.PriceRealtimeSnapshot.ticker == normalized
        ).first()
        if not snapshot or snapshot.price is None:
            return []

        rules = db.query(models.TriggerRule).filter(
            models.TriggerRule.ticker == normalized,
            models.TriggerRule.enabled == 1,
        ).all()
        events: list[models.TriggerEvent] = []
        for rule in rules:
            if rule.threshold is None:
                continue
            current = _current_value(snapshot, rule.rule_type)
            if current is None:
                continue
            if _matches(current, rule.operator, rule.threshold):
                event = self._create_event_once(
                    db=db,
                    ticker=normalized,
                    level=rule.level,
                    event_type=rule.rule_type,
                    message=rule.description or f"{rule.rule_type} {rule.operator} {rule.threshold}",
                    price=snapshot.price,
                )
                if event:
                    events.append(event)
        plan_events = self._evaluate_latest_plan(db, normalized, snapshot)
        events.extend(plan_events)
        db.commit()
        for event in events:
            db.refresh(event)
        return events

    def list_events(self, db: Session, limit: int = 100) -> list[models.TriggerEvent]:
        return db.query(models.TriggerEvent).order_by(models.TriggerEvent.created_at.desc()).limit(limit).all()

    def update_event_status(self, db: Session, event_id: int, status: str) -> models.TriggerEvent | None:
        event = db.query(models.TriggerEvent).filter(models.TriggerEvent.id == event_id).first()
        if not event:
            return None
        event.status = status
        db.commit()
        db.refresh(event)
        return event

    def _create_event_once(
        self,
        db: Session,
        ticker: str,
        level: int,
        event_type: str,
        message: str,
        price: float | None,
    ) -> models.TriggerEvent | None:
        since = datetime.utcnow() - timedelta(minutes=self.dedupe_minutes)
        existing = db.query(models.TriggerEvent).filter(
            models.TriggerEvent.ticker == ticker,
            models.TriggerEvent.level == level,
            models.TriggerEvent.event_type == event_type,
            models.TriggerEvent.status.in_(["new", "acknowledged", "need_bettafish", "need_tradingagents"]),
            models.TriggerEvent.created_at >= since,
        ).first()
        if existing:
            return None
        event = models.TriggerEvent(
            ticker=ticker,
            level=level,
            event_type=event_type,
            message=message,
            price=price,
        )
        db.add(event)
        return event

    def _evaluate_latest_plan(
        self,
        db: Session,
        ticker: str,
        snapshot: models.PriceRealtimeSnapshot,
    ) -> list[models.TriggerEvent]:
        plan = db.query(models.DailyPlan).filter(
            models.DailyPlan.ticker == ticker
        ).order_by(models.DailyPlan.plan_date.desc()).first()
        if not plan or snapshot.price is None:
            return []

        events: list[models.TriggerEvent] = []
        supports = _parse_levels(plan.support_levels)
        resistances = _parse_levels(plan.resistance_levels)

        if plan.stop_loss is not None and snapshot.price <= plan.stop_loss:
            event = self._create_event_once(
                db=db,
                ticker=ticker,
                level=3,
                event_type="stop_loss",
                message=f"价格 {snapshot.price:.2f} 已触及/跌破止损位 {plan.stop_loss:.2f}",
                price=snapshot.price,
            )
            if event:
                events.append(event)
        elif supports and snapshot.price <= max(supports):
            event = self._create_event_once(
                db=db,
                ticker=ticker,
                level=2,
                event_type="support_break",
                message=f"价格 {snapshot.price:.2f} 已触及/跌破最近支撑 {max(supports):.2f}",
                price=snapshot.price,
            )
            if event:
                events.append(event)

        if resistances and snapshot.price >= min(resistances):
            event = self._create_event_once(
                db=db,
                ticker=ticker,
                level=2,
                event_type="resistance_break",
                message=f"价格 {snapshot.price:.2f} 已触及/突破最近压力 {min(resistances):.2f}",
                price=snapshot.price,
            )
            if event:
                events.append(event)
        return events


def _current_value(snapshot: models.PriceRealtimeSnapshot, rule_type: str) -> float | None:
    if rule_type == "price":
        return snapshot.price
    if rule_type == "change_pct":
        return snapshot.change_pct
    if rule_type == "volume":
        return snapshot.volume
    return None


def _matches(current: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return current > threshold
    if operator == ">=":
        return current >= threshold
    if operator == "<":
        return current < threshold
    if operator == "<=":
        return current <= threshold
    if operator == "==":
        return current == threshold
    return False


def _parse_levels(raw: str | None) -> list[float]:
    if not raw:
        return []
    values: list[float] = []
    for part in raw.replace("，", ",").replace("/", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.append(float(item))
        except ValueError:
            continue
    return sorted(values)
