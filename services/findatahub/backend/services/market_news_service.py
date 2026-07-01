from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models
from ..json_utils import sanitize_json_value

logger = logging.getLogger(__name__)

BASE_URL = "https://newsnow.busiyi.world"

SOURCE_CONFIGS: dict[str, dict[str, Any]] = {
    "cls-hot": {"name": "财联社", "tier": "primary", "source_type": "financial_wire", "default_limit": 30},
    "wallstreetcn": {"name": "华尔街见闻", "tier": "primary", "source_type": "financial_wire", "default_limit": 30},
    "thepaper": {"name": "澎湃", "tier": "primary", "source_type": "general_news", "default_limit": 24},
    "toutiao": {"name": "头条", "tier": "primary", "source_type": "general_news", "default_limit": 24},
    "xueqiu": {"name": "雪球", "tier": "primary", "source_type": "investor_community", "default_limit": 24},
    "weibo": {"name": "微博", "tier": "secondary", "source_type": "social_heat", "default_limit": 18},
    "zhihu": {"name": "知乎", "tier": "secondary", "source_type": "social_heat", "default_limit": 16},
    "douyin": {"name": "抖音", "tier": "secondary", "source_type": "social_heat", "default_limit": 16},
}

DEFAULT_MARKET_SOURCES = list(SOURCE_CONFIGS)
FETCH_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class MarketNewsService:
    def get_sidebar_snapshot(self, db: Session, limit: int = 6) -> dict[str, Any]:
        limit = max(1, min(int(limit or 6), 30))
        snapshot_date = db.query(func.max(models.MarketNewsArticle.crawl_date)).scalar()
        meta = self._build_meta(db, snapshot_date)
        if snapshot_date is None:
            return {"items": [], "meta": meta}
        rows = (
            db.query(models.MarketNewsArticle)
            .filter(models.MarketNewsArticle.crawl_date == snapshot_date)
            .order_by(
                models.MarketNewsArticle.final_score.desc().nullslast(),
                models.MarketNewsArticle.rank_position.asc().nullslast(),
                models.MarketNewsArticle.id.asc(),
            )
            .limit(limit)
            .all()
        )
        return {"items": [self._serialize_sidebar_item(row) for row in rows], "meta": meta}

    def refresh_snapshot(self, db: Session, *, force: bool = False, limit: int = 6) -> dict[str, Any]:
        today = date.today()
        self.cleanup_old_snapshots(db, keep_date=today)
        if not force and not self._snapshot_is_stale(db):
            return {
                "started": False,
                "status": "fresh",
                "message": "新闻快照仍在有效期内，跳过自动刷新",
                "snapshot": self.get_sidebar_snapshot(db, limit=limit),
            }

        items = asyncio.run(self._collect_market_news(today))
        if not items:
            return {
                "started": False,
                "status": "empty",
                "message": "新闻源未返回可用快照，已清理过期新闻",
                "snapshot": self.get_sidebar_snapshot(db, limit=limit),
            }

        self._replace_snapshot(db, today, items)
        return {
            "started": False,
            "status": "ok",
            "message": f"新闻快照已刷新，共 {len(items)} 条",
            "snapshot": self.get_sidebar_snapshot(db, limit=limit),
        }

    def cleanup_old_snapshots(self, db: Session, *, keep_date: date | None = None) -> int:
        keep_date = keep_date or date.today()
        deleted = db.query(models.MarketNewsArticle).filter(models.MarketNewsArticle.crawl_date < keep_date).delete()
        db.commit()
        return int(deleted or 0)

    def search_market_news(
        self,
        db: Session,
        terms: list[str],
        start_date: str | None,
        end_date: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = db.query(models.MarketNewsArticle)
        start_day = _parse_date(start_date)
        end_day = _parse_date(end_date)
        if start_day:
            query = query.filter(models.MarketNewsArticle.crawl_date >= start_day)
        if end_day:
            query = query.filter(models.MarketNewsArticle.crawl_date <= end_day)
        if terms:
            filters = []
            for term in terms[:8]:
                pattern = f"%{term}%"
                filters.append(models.MarketNewsArticle.title.like(pattern))
                filters.append(models.MarketNewsArticle.summary.like(pattern))
            if filters:
                query = query.filter(or_(*filters))
        rows = (
            query.order_by(
                models.MarketNewsArticle.crawl_date.desc(),
                models.MarketNewsArticle.final_score.desc().nullslast(),
                models.MarketNewsArticle.rank_position.asc().nullslast(),
            )
            .limit(max(1, min(limit * 4, 100)))
            .all()
        )
        return [self._serialize_search_item(row, terms) for row in rows]

    async def _collect_market_news(self, extract_date: date) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(4)

        async def fetch(source: str) -> dict[str, Any]:
            async with semaphore:
                return await self._fetch_source(source)

        results = await asyncio.gather(*(fetch(source) for source in DEFAULT_MARKET_SOURCES))
        normalized: list[dict[str, Any]] = []
        for result in results:
            if result.get("status") != "success":
                logger.warning("Market news source failed: %s", result.get("error") or result.get("source"))
                continue
            source = str(result.get("source") or "")
            config = SOURCE_CONFIGS.get(source, {})
            raw_items = ((result.get("data") or {}).get("items") or []) if isinstance(result.get("data"), dict) else []
            item_limit = int(config.get("default_limit", len(raw_items) or 0))
            for rank, item in enumerate(raw_items[:item_limit], start=1):
                mapped = self._normalize_item(item, source, rank, extract_date)
                if mapped:
                    normalized.append(mapped)
        return self._dedupe_and_rank(normalized)

    async def _fetch_source(self, source: str) -> dict[str, Any]:
        url = f"{BASE_URL}/api/s?id={source}&latest"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Referer": BASE_URL,
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for attempt in range(1, 5):
                try:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    return {"source": source, "status": "success", "data": response.json()}
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    if status_code not in FETCH_RETRYABLE_STATUS_CODES or attempt == 4:
                        return {"source": source, "status": "http_error", "error": f"{status_code}: {url}"}
                except Exception as exc:
                    if attempt == 4:
                        return {"source": source, "status": "error", "error": f"{url} - {exc}"}
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        return {"source": source, "status": "error", "error": f"failed: {url}"}

    def _replace_snapshot(self, db: Session, crawl_date: date, items: list[dict[str, Any]]) -> None:
        db.query(models.MarketNewsArticle).filter(models.MarketNewsArticle.crawl_date == crawl_date).delete()
        now = datetime.utcnow()
        for item in items:
            extra = item.get("extra_info") if isinstance(item.get("extra_info"), dict) else {}
            db.add(
                models.MarketNewsArticle(
                    news_id=str(item["news_id"]),
                    provider="newsnow",
                    source_platform=str(item.get("source_platform") or "unknown"),
                    source_label=str(item.get("source_label") or ""),
                    title=str(item.get("title") or "")[:512],
                    summary=str(item.get("summary") or "")[:4000],
                    url=str(item.get("url") or "") or None,
                    crawl_date=crawl_date,
                    rank_position=_safe_int(item.get("rank_position")),
                    published_at_text=str(extra.get("published_at_text") or "")[:128] or None,
                    category=extra.get("category"),
                    event_type=extra.get("event_type"),
                    confidence=_safe_float(extra.get("confidence")),
                    final_score=_safe_float(extra.get("final_score")),
                    raw_payload=sanitize_json_value(item.get("raw_payload") or {}),
                    fetched_at=now,
                    updated_at=now,
                )
            )
        db.commit()

    def _snapshot_is_stale(self, db: Session) -> bool:
        latest_updated = db.query(func.max(models.MarketNewsArticle.updated_at)).scalar()
        latest_date = db.query(func.max(models.MarketNewsArticle.crawl_date)).scalar()
        if latest_date != date.today() or latest_updated is None:
            return True
        age_seconds = (datetime.now(timezone.utc) - latest_updated.replace(tzinfo=timezone.utc)).total_seconds()
        return age_seconds >= 30 * 60

    def _build_meta(self, db: Session, snapshot_date: date | None) -> dict[str, Any]:
        latest_updated = db.query(func.max(models.MarketNewsArticle.updated_at)).scalar()
        item_count = 0
        if snapshot_date is not None:
            item_count = db.query(models.MarketNewsArticle).filter(models.MarketNewsArticle.crawl_date == snapshot_date).count()
        return {
            "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
            "updated_at": latest_updated.isoformat() if latest_updated else None,
            "refreshing": False,
            "last_refresh_requested_at": None,
            "last_refresh_finished_at": latest_updated.isoformat() if latest_updated else None,
            "last_refresh_error": None,
            "item_count": item_count,
            "provider": "findatahub",
        }

    @staticmethod
    def _normalize_item(item: Any, source: str, rank: int, extract_date: date) -> dict[str, Any] | None:
        payload = item if isinstance(item, dict) else {"value": str(item)}
        extra_payload = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        title = _normalize_text(_first_non_empty(payload.get("title"), payload.get("name"), payload.get("text"), payload.get("desc"), payload.get("value")))
        if not title:
            return None
        url = _first_non_empty(payload.get("url"), payload.get("link"), payload.get("share_url"), payload.get("jump_url"))
        summary = _normalize_text(
            _first_non_empty(
                payload.get("summary"),
                payload.get("description"),
                payload.get("digest"),
                payload.get("content"),
                payload.get("subtitle"),
                extra_payload.get("summary"),
                extra_payload.get("description"),
                extra_payload.get("info"),
            )
        )
        published_at_text = _normalize_text(
            _first_non_empty(
                payload.get("published_at"),
                payload.get("publish_time"),
                payload.get("ctime"),
                payload.get("created_at"),
                payload.get("time"),
                payload.get("date"),
                extra_payload.get("published_at"),
                extra_payload.get("publish_time"),
                extra_payload.get("time"),
                extra_payload.get("date"),
            )
        )
        published_at_text = _normalize_published_at_text(published_at_text)
        dedupe_basis = f"{title}|{url or ''}"
        dedupe_key = hashlib.sha1(dedupe_basis.encode("utf-8")).hexdigest()[:24]
        news_id = f"{extract_date.strftime('%Y%m%d')}_{source}_{dedupe_key}"[:160]
        raw_payload = dict(payload)
        raw_payload["_finclaw_detail_meta"] = {"published_at_text": published_at_text}
        final_score = _score_news(source, rank, title, summary)
        return {
            "news_id": news_id,
            "source_platform": source,
            "source_label": SOURCE_CONFIGS.get(source, {}).get("name", source),
            "title": title[:512],
            "summary": summary[:4000] or title[:512],
            "url": str(url or "").strip(),
            "rank_position": rank,
            "dedupe_key": dedupe_key,
            "extra_info": {
                "published_at_text": published_at_text[:128],
                "category": _infer_category(title, summary),
                "event_type": _infer_event_type(title, summary),
                "final_score": final_score,
            },
            "raw_payload": raw_payload,
        }

    @staticmethod
    def _dedupe_and_rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        winners: dict[str, dict[str, Any]] = {}
        for item in items:
            key = str(item.get("dedupe_key") or item.get("news_id"))
            existing = winners.get(key)
            if existing is None or _sort_key(item) < _sort_key(existing):
                winners[key] = item
        ranked = list(winners.values())
        ranked.sort(key=_sort_key)
        return ranked

    @staticmethod
    def _serialize_sidebar_item(row: models.MarketNewsArticle) -> dict[str, Any]:
        tags = []
        for value in (row.category, row.event_type):
            if value:
                tags.append(value)
        return {
            "id": row.news_id,
            "title": row.title,
            "summary": row.summary or row.title,
            "url": row.url or "",
            "source_platform": row.source_platform,
            "source_label": row.source_label or row.source_platform,
            "published_at_text": row.published_at_text or "",
            "snapshot_date": row.crawl_date.isoformat(),
            "rank_position": row.rank_position,
            "detail_level": None,
            "category": row.category,
            "event_type": row.event_type,
            "tags": tags[:3],
            "confidence": row.confidence,
            "final_score": row.final_score,
        }

    @staticmethod
    def _serialize_search_item(row: models.MarketNewsArticle, terms: list[str]) -> dict[str, Any]:
        matched, matched_index = _first_matched_term(f"{row.title}\n{row.summary or ''}", terms)
        return {
            "ticker": None,
            "title": row.title,
            "summary": row.summary or row.title,
            "source": row.source_label or row.source_platform,
            "url": row.url,
            "published_at": row.crawl_date.isoformat(),
            "fetched_at": row.updated_at.isoformat() if row.updated_at else None,
            "provider": "datahub_market_news",
            "relevance_reason": f"matched:{matched}" if matched else "market_news",
            "relevance_score": max(10, 100 - matched_index * 10) if matched is not None else 10,
            "rank_position": row.rank_position,
            "category": row.category,
            "event_type": row.event_type,
            "confidence": row.confidence,
            "final_score": row.final_score,
        }


def _sort_key(item: dict[str, Any]) -> tuple[float, int, str]:
    extra = item.get("extra_info") if isinstance(item.get("extra_info"), dict) else {}
    score = _safe_float(extra.get("final_score"))
    rank = _safe_int(item.get("rank_position")) or 999999
    return (-(score or 0.0), rank, str(item.get("title") or ""))


def _score_news(source: str, rank: int, title: str, summary: str) -> float:
    tier_boost = 30 if SOURCE_CONFIGS.get(source, {}).get("tier") == "primary" else 15
    content_boost = min(len(summary or title), 160) / 16
    rank_score = max(0, 60 - min(rank, 60))
    return round(tier_boost + rank_score + content_boost, 3)


def _infer_category(title: str, summary: str) -> str | None:
    text = f"{title} {summary}"
    if any(token in text for token in ("央行", "政策", "财政", "发改委", "关税", "利率")):
        return "政策宏观"
    if any(token in text for token in ("A股", "港股", "指数", "涨停", "成交", "资金")):
        return "资本市场"
    if any(token in text for token in ("产业", "供应链", "AI", "半导体", "新能源", "汽车")):
        return "行业链"
    if any(token in text for token in ("公司", "业绩", "订单", "公告")):
        return "公司信号"
    return None


def _infer_event_type(title: str, summary: str) -> str | None:
    text = f"{title} {summary}"
    if any(token in text for token in ("大涨", "大跌", "跳水", "拉升", "涨停")):
        return "市场波动"
    if any(token in text for token in ("政策", "发布", "通知", "监管")):
        return "政策"
    if any(token in text for token in ("订单", "业绩", "减持", "增持", "公告")):
        return "公司信号"
    return None


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_published_at_text(value: str) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{10,13}", text):
        try:
            numeric = int(text)
            timestamp = numeric / 1000 if len(text) == 13 else numeric
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return text
    return text


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _first_matched_term(text: str, terms: list[str]) -> tuple[str | None, int]:
    for index, term in enumerate(terms):
        if term and term in text:
            return term, index
    return None, 999
