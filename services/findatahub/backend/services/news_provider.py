from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from .. import models
from ..config import REPO_ROOT
from ..providers.symbol import normalize_a_share_ticker
from .market_news_service import MarketNewsService

logger = logging.getLogger(__name__)

_FINCLAW_ENV = REPO_ROOT / ".env"


def _prime_web_search_env() -> None:
    _load_env_keys(
        _FINCLAW_ENV,
        (
            "TAVILY_API_KEY",
            "FINCLAW_WEB_SEARCH_ENABLED",
            "FINCLAW_WEB_FETCH_TIMEOUT",
            "FINCLAW_WEB_SEARCH_MAX_SOURCES",
        ),
    )


def _load_env_keys(path: Path, prefixes: tuple[str, ...]) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key.startswith(prefixes):
                continue
            cleaned = value.strip().strip('"').strip("'")
            if cleaned and not os.environ.get(key):
                os.environ[key] = cleaned
    except Exception as exc:
        logger.warning("Failed to load env keys from %s: %s", path, exc)


_prime_web_search_env()


class NewsProvider:
    """Unified local news provider.

    Primary source is cached DataHub ticker news plus DataHub market news.
    Upstream web search remains a FinClaw tool; DataHub only returns locally
    available, cached news so data-package calls stay deterministic and fast.
    """

    def __init__(self) -> None:
        self.market_news = MarketNewsService()

    def search(
        self,
        db: Session,
        *,
        ticker: str | None = None,
        query: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        include_web: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_a_share_ticker(ticker) if ticker else None
        limit = max(1, min(int(limit or 20), 50))
        terms = self._build_terms(db, normalized, query)

        items: list[dict[str, Any]] = []
        items.extend(self._cached_ticker_news(db, normalized, start_date, end_date, limit) if normalized else [])
        items.extend(self.market_news.search_market_news(db, terms, start_date, end_date, limit))
        if include_web:
            items.extend(self._web_search_news(terms, limit=max(1, min(3, limit))))

        merged = self._dedupe(items)[:limit]
        return {
            "items": merged,
            "meta": {
                "ticker": normalized,
                "query": query,
                "terms": terms,
                "providers": self._providers(merged),
                "web_search_enabled": include_web,
            },
        }

    def _build_terms(self, db: Session, ticker: str | None, query: str | None) -> list[str]:
        terms: list[str] = []
        for value in (query, ticker):
            if value and str(value).strip():
                terms.append(str(value).strip())
        if ticker:
            profile = db.query(models.CompanyProfile).filter(models.CompanyProfile.ticker == ticker).first()
            instrument = db.query(models.Instrument).filter(models.Instrument.ticker == ticker).first()
            for value in (
                getattr(profile, "name", None),
                getattr(profile, "industry", None),
                getattr(instrument, "name", None),
                getattr(instrument, "industry", None),
                getattr(instrument, "sector", None),
            ):
                if value and str(value).strip():
                    terms.append(str(value).strip())
        seen: set[str] = set()
        unique: list[str] = []
        for term in terms:
            cleaned = term.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(cleaned[:80])
        return unique[:8]

    def _cached_ticker_news(
        self,
        db: Session,
        ticker: str,
        start_date: str | None,
        end_date: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = db.query(models.NewsArticle).filter(models.NewsArticle.ticker == ticker)
        start_dt = _parse_datetime(start_date)
        end_dt = _parse_datetime(end_date)
        if start_dt:
            query = query.filter(models.NewsArticle.published_at >= start_dt)
        if end_dt:
            query = query.filter(models.NewsArticle.published_at <= end_dt)
        rows = (
            query.order_by(models.NewsArticle.published_at.desc().nullslast(), models.NewsArticle.fetched_at.desc())
            .limit(limit)
            .all()
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "ticker": ticker,
                    "title": row.title,
                    "summary": row.summary,
                    "source": row.source or "DataHub",
                    "url": row.url,
                    "published_at": _iso(row.published_at or row.fetched_at),
                    "fetched_at": _iso(row.fetched_at),
                    "provider": "datahub_cache",
                    "relevance_reason": "cached_ticker_news",
                    "relevance_score": 120,
                }
            )
        return items

    def _web_search_news(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        enabled = os.getenv("FINCLAW_WEB_SEARCH_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "off",
            "disabled",
        }
        if not enabled or not api_key or not terms:
            return []
        query = " ".join(terms[:4]) + " 最新 新闻 公告"
        timeout = _safe_float(os.getenv("FINCLAW_WEB_FETCH_TIMEOUT")) or 5.0
        max_results = max(1, min(limit, int(_safe_float(os.getenv("FINCLAW_WEB_SEARCH_MAX_SOURCES")) or 3), 5))
        payload = {
            "api_key": api_key,
            "query": query[:240],
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            resp = requests.post("https://api.tavily.com/search", json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Tavily news fallback failed: %s", exc)
            return []

        items: list[dict[str, Any]] = []
        for row in (data.get("results") or [])[:max_results]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "ticker": None,
                    "title": title,
                    "summary": str(row.get("content") or "").strip()[:1000] or title,
                    "source": _domain_label(row.get("url")),
                    "url": row.get("url"),
                    "published_at": row.get("published_date"),
                    "fetched_at": datetime.utcnow().isoformat(),
                    "provider": "web_search",
                    "relevance_reason": "web_search_fallback",
                    "relevance_score": 60,
                    "final_score": row.get("score"),
                }
            )
        return items

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in items:
            key = str(item.get("url") or item.get("title") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        merged.sort(
            key=lambda row: (
                _safe_float(row.get("relevance_score")),
                str(row.get("published_at") or ""),
                _safe_float(row.get("final_score")),
            ),
            reverse=True,
        )
        return merged

    @staticmethod
    def _providers(items: list[dict[str, Any]]) -> list[str]:
        providers = sorted({str(item.get("provider") or "") for item in items if item.get("provider")})
        return providers


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _domain_label(url: Any) -> str:
    try:
        netloc = urlparse(str(url or "")).netloc
        return netloc.replace("www.", "") or "web"
    except Exception:
        return "web"


def _safe_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
