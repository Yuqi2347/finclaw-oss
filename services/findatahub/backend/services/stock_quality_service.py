from __future__ import annotations

from datetime import datetime

from .. import models


class StockQualityService:
    def summarize_daily_series(
        self,
        rows: list[models.PriceDaily],
        snapshot: models.PriceRealtimeSnapshot | None = None,
    ) -> dict:
        ordered_rows = sorted(rows, key=lambda row: (row.trade_date, row.id))
        sources = sorted({str(row.source or "").strip() for row in ordered_rows if str(row.source or "").strip()})
        warnings: list[str] = []
        blocking_issues: list[str] = []

        if not ordered_rows:
            blocking_issues.append("no_daily_rows")
            return self._empty_daily_quality(snapshot, sources, warnings, blocking_issues)

        for row in ordered_rows:
            prices = [row.open, row.high, row.low, row.close]
            if any(value is None for value in prices):
                blocking_issues.append("missing_ohlc")
                break
            if any(value is not None and value <= 0 for value in prices):
                blocking_issues.append("non_positive_price")
                break
            if row.low is not None and row.high is not None and row.low > row.high:
                blocking_issues.append("invalid_ohlc")
                break
            if row.open is not None and row.close is not None and row.high is not None and row.low is not None:
                if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
                    blocking_issues.append("invalid_ohlc")
                    break

        if len(sources) > 1:
            warnings.append(f"mixed_daily_sources:{','.join(sources)}")

        latest_row = ordered_rows[-1]
        last_refreshed_at = max((row.created_at for row in ordered_rows if row.created_at is not None), default=None)
        if latest_row.trade_date:
            age_days = (datetime.utcnow().date() - latest_row.trade_date).days
            if age_days > 10:
                warnings.append(f"daily_series_stale:{age_days}d")

        if snapshot is None:
            warnings.append("missing_snapshot")

        adjustment = ordered_rows[0].adjustment if ordered_rows else "unknown"
        status = "blocked" if blocking_issues else ("warn" if warnings else "ready")
        return {
            "status": status,
            "row_count": len(ordered_rows),
            "sources": sources,
            "adjustment": adjustment,
            "warnings": warnings,
            "blocking_issues": blocking_issues,
            "earliest_trade_date": ordered_rows[0].trade_date.isoformat() if ordered_rows[0].trade_date else None,
            "latest_trade_date": latest_row.trade_date.isoformat() if latest_row.trade_date else None,
            "latest_close": latest_row.close,
            "last_refreshed_at": last_refreshed_at.isoformat() if last_refreshed_at else None,
        }

    def summarize_package_quality(
        self,
        daily_by_adjustment: dict[str, list[models.PriceDaily]],
        indicators_by_adjustment: dict[str, list[models.TechnicalIndicator]],
        snapshot: models.PriceRealtimeSnapshot | None,
    ) -> dict:
        daily_quality = {
            adjustment: self.summarize_daily_series(rows, snapshot)
            for adjustment, rows in daily_by_adjustment.items()
        }
        indicator_coverage = {}
        indicator_warnings: list[str] = []
        for adjustment, rows in indicators_by_adjustment.items():
            indicator_coverage[adjustment] = {
                "row_count": len(rows),
                "latest_trade_date": rows[-1].trade_date.isoformat() if rows else None,
            }
            if not rows:
                indicator_warnings.append(f"missing_indicators:{adjustment}")

        preferred = daily_quality.get("qfq") or daily_quality.get("raw") or {"status": "blocked", "warnings": ["no_series"]}
        warnings = list(preferred.get("warnings") or [])
        warnings.extend(indicator_warnings)
        blocking_issues = list(preferred.get("blocking_issues") or [])
        if "qfq" not in daily_by_adjustment:
            warnings.append("missing_adjustment:qfq")
        if "raw" not in daily_by_adjustment:
            warnings.append("missing_adjustment:raw")

        status = "blocked" if blocking_issues else ("warn" if warnings else "ready")
        return {
            "status": status,
            "warnings": warnings,
            "blocking_issues": blocking_issues,
            "daily": daily_quality,
            "indicators": indicator_coverage,
        }

    def _empty_daily_quality(
        self,
        snapshot: models.PriceRealtimeSnapshot | None,
        sources: list[str],
        warnings: list[str],
        blocking_issues: list[str],
    ) -> dict:
        if snapshot is None:
            warnings.append("missing_snapshot")
        return {
            "status": "blocked",
            "row_count": 0,
            "sources": sources,
            "adjustment": "unknown",
            "warnings": warnings,
            "blocking_issues": blocking_issues,
            "earliest_trade_date": None,
            "latest_trade_date": None,
            "latest_close": None,
            "last_refreshed_at": None,
        }
