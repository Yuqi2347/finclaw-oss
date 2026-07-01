from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .. import models
from ..providers.symbol import normalize_a_share_ticker
from ..schemas import DailyPlanUpsert


class PlanService:
    def upsert_daily_plan(self, db: Session, payload: DailyPlanUpsert) -> models.DailyPlan:
        ticker = normalize_a_share_ticker(payload.ticker)
        plan = db.query(models.DailyPlan).filter(
            models.DailyPlan.ticker == ticker,
            models.DailyPlan.plan_date == payload.plan_date,
        ).first()
        if not plan:
            plan = models.DailyPlan(ticker=ticker, plan_date=payload.plan_date)
            db.add(plan)
        plan.bias = payload.bias
        plan.expected_path = payload.expected_path
        plan.support_levels = payload.support_levels
        plan.resistance_levels = payload.resistance_levels
        plan.stop_loss = payload.stop_loss
        plan.target_zone = payload.target_zone
        plan.notes = payload.notes
        db.commit()
        db.refresh(plan)
        return plan

    def list_daily_plans(self, db: Session, ticker: str | None = None, limit: int = 100) -> list[models.DailyPlan]:
        query = db.query(models.DailyPlan)
        if ticker:
            query = query.filter(models.DailyPlan.ticker == normalize_a_share_ticker(ticker))
        return query.order_by(models.DailyPlan.plan_date.desc()).limit(limit).all()

    def latest_daily_plan(self, db: Session, ticker: str, plan_date: date | None = None) -> models.DailyPlan | None:
        query = db.query(models.DailyPlan).filter(models.DailyPlan.ticker == normalize_a_share_ticker(ticker))
        if plan_date:
            query = query.filter(models.DailyPlan.plan_date <= plan_date)
        return query.order_by(models.DailyPlan.plan_date.desc()).first()

