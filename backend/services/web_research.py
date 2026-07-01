from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests

from backend.core.env import settings


MAX_EXCERPT_CHARS = 550
MAX_QUERY_CHARS = 240
MAX_TOTAL_SOURCES = 8


def web_research(
    query: str | None = None,
    intent: str = "verify_claim",
    recency: str = "any",
    max_sources: int | None = None,
    queries: list[Any] | None = None,
    max_sources_per_query: int | None = None,
    total_source_budget: int | None = None,
    source_policy: str = "finance_first",
) -> dict[str, Any]:
    if not settings.web_search_enabled:
        return _error("disabled", _clean_text(query or ""), intent, "web search is disabled")
    specs = _normalize_query_specs(query, queries, intent, recency, source_policy, max_sources, max_sources_per_query)
    if not specs:
        return _error("invalid_query", _clean_text(query or ""), intent, "query or queries is required")

    total_timeout = max(3.0, min(float(settings.web_total_timeout or 12), 30.0))
    deadline = time.monotonic() + total_timeout
    started_at = time.monotonic()
    query_results: list[dict[str, Any]] = []
    timed_out = False

    if len(specs) == 1:
        query_results.append(_run_single_query(specs[0], deadline))
    else:
        executor = ThreadPoolExecutor(max_workers=min(len(specs), 4))
        try:
            futures = [executor.submit(_run_single_query, spec, deadline) for spec in specs]
            try:
                for future in as_completed(futures, timeout=total_timeout):
                    query_results.append(future.result())
            except TimeoutError:
                timed_out = True
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    query_results.sort(key=lambda item: item.get("index", 0))
    source_budget = _total_source_budget(total_source_budget, specs)
    sources, url_to_source_id = _merge_query_sources(query_results, source_budget)
    claims = _build_claims(query_results, url_to_source_id)
    attempts = [
        {"query": item.get("query"), "attempts": item.get("provider_attempts", [])}
        for item in query_results
    ]
    if not sources:
        flat_attempts = [attempt for item in query_results for attempt in item.get("provider_attempts", [])]
        status = "not_configured" if flat_attempts and all(item["status"] == "not_configured" for item in flat_attempts) else "error"
        return {
            "status": status,
            "query": specs[0]["query"] if len(specs) == 1 else "",
            "queries": [spec["query"] for spec in specs],
            "intent": intent,
            "answerable": False,
            "sources": [],
            "claims": claims or [{"claim": specs[0]["query"], "verdict": "insufficient", "source_ids": [], "confidence": "low"}],
            "provider_attempts": attempts,
            "query_summaries": _compact_query_results(query_results),
            "stopped_reason": "total_timeout" if timed_out else "no_sources",
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }

    return {
        "status": "ok",
        "query": specs[0]["query"] if len(specs) == 1 else "",
        "queries": [spec["query"] for spec in specs],
        "intent": intent,
        "answerable": True,
        "sources": sources,
        "claims": claims,
        "provider_attempts": attempts,
        "query_summaries": _compact_query_results(query_results),
        "stopped_reason": "total_timeout_partial" if timed_out else "completed",
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "usage_note": "Use only these returned sources for inline citations such as [1] and [2].",
    }


def _normalize_query_specs(
    query: str | None,
    queries: list[Any] | None,
    intent: str,
    recency: str,
    source_policy: str,
    max_sources: int | None,
    max_sources_per_query: int | None,
) -> list[dict[str, Any]]:
    raw_items: list[Any] = queries if isinstance(queries, list) and queries else [query]
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items[:4]):
        if isinstance(item, dict):
            item_query = _clean_text(str(item.get("query") or ""))[:MAX_QUERY_CHARS]
            item_intent = str(item.get("intent") or intent or "verify_claim")
            item_recency = str(item.get("recency") or recency or "any")
            item_policy = str(item.get("source_policy") or source_policy or "finance_first")
            item_limit = item.get("max_sources") or item.get("max_sources_per_query")
        else:
            item_query = _clean_text(str(item or ""))[:MAX_QUERY_CHARS]
            item_intent = intent or "verify_claim"
            item_recency = recency or "any"
            item_policy = source_policy or "finance_first"
            item_limit = None
        if not item_query:
            continue
        requested_limit = max_sources_per_query or item_limit or max_sources or _default_sources_for_intent(item_intent)
        limit = max(1, min(int(requested_limit or 3), 5, settings.web_search_max_sources or 5))
        specs.append(
            {
                "index": index,
                "query": item_query,
                "intent": item_intent,
                "recency": item_recency,
                "source_policy": item_policy,
                "limit": limit,
            }
        )
    return specs


def _default_sources_for_intent(intent: str) -> int:
    value = str(intent or "").lower()
    if value in {"verify_claim", "current_fact", "source_lookup"}:
        return 3
    return 4


def _total_source_budget(total_source_budget: int | None, specs: list[dict[str, Any]]) -> int:
    if isinstance(total_source_budget, int) and total_source_budget > 0:
        return max(1, min(total_source_budget, MAX_TOTAL_SOURCES))
    if len(specs) <= 1:
        return max(1, min(specs[0].get("limit", 4), 5))
    return max(3, min(MAX_TOTAL_SOURCES, sum(int(spec.get("limit", 3)) for spec in specs)))


def _run_single_query(spec: dict[str, Any], deadline: float) -> dict[str, Any]:
    started_at = time.monotonic()
    provider_order = _provider_order()
    attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    stopped_reason = ""

    for provider in provider_order:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stopped_reason = "total_timeout"
            break
        try:
            request_timeout = max(1.0, min(float(settings.web_fetch_timeout or 8), remaining))
            provider_results = _search_provider(provider, spec["query"], spec["recency"], spec["limit"], request_timeout)
            attempts.append({"provider": provider, "status": "ok", "count": len(provider_results)})
            results.extend(provider_results)
            results = _dedupe_and_rank(results, spec["source_policy"])
            if results:
                stopped_reason = "provider_success"
                break
        except _MissingKey:
            attempts.append({"provider": provider, "status": "not_configured"})
        except Exception as exc:
            attempts.append({"provider": provider, "status": "error", "error": str(exc)[:180]})

    sources = _build_sources(results[: spec["limit"]])
    return {
        "index": spec["index"],
        "query": spec["query"],
        "intent": spec["intent"],
        "recency": spec["recency"],
        "source_policy": spec["source_policy"],
        "sources": sources,
        "provider_attempts": attempts,
        "stopped_reason": stopped_reason or "completed",
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


def _merge_query_sources(query_results: list[dict[str, Any]], total_budget: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    url_to_source_id: dict[str, str] = {}
    for result in query_results:
        for source in result.get("sources", []):
            if not isinstance(source, dict) or not source.get("url"):
                continue
            key = _dedupe_key(source)
            if key in seen:
                url_to_source_id[str(source.get("url"))] = next((item["source_id"] for item in merged if _dedupe_key(item) == key), "")
                continue
            if len(merged) >= total_budget:
                continue
            seen.add(key)
            marker = len(merged) + 1
            normalized = dict(source)
            normalized["marker"] = marker
            normalized["source_id"] = f"src_{marker}"
            merged.append(normalized)
            url_to_source_id[str(source.get("url"))] = normalized["source_id"]
    return merged, url_to_source_id


def _build_claims(query_results: list[dict[str, Any]], url_to_source_id: dict[str, str]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for result in query_results:
        source_ids = []
        for source in result.get("sources", []):
            source_id = url_to_source_id.get(str(source.get("url")))
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
        claims.append(
            {
                "claim": result.get("query", ""),
                "verdict": "sources_found" if source_ids else "insufficient",
                "source_ids": source_ids[:3],
                "confidence": "medium" if source_ids else "low",
            }
        )
    return claims


def _compact_query_results(query_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for item in query_results:
        compacted.append(
            {
                "query": item.get("query"),
                "intent": item.get("intent"),
                "source_count": len(item.get("sources") or []),
                "provider_attempts": item.get("provider_attempts", []),
                "stopped_reason": item.get("stopped_reason"),
                "elapsed_ms": item.get("elapsed_ms"),
            }
        )
    return compacted


class _MissingKey(Exception):
    pass


def _provider_order() -> list[str]:
    raw = settings.search_provider_order or "tavily,brave,exa,serpapi,you"
    order = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return order or ["tavily", "brave", "exa", "serpapi", "you"]


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _search_provider(provider: str, query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if provider == "tavily":
        return _search_tavily(query, recency, limit, timeout)
    if provider == "brave":
        return _search_brave(query, recency, limit, timeout)
    if provider == "exa":
        return _search_exa(query, recency, limit, timeout)
    if provider == "serpapi":
        return _search_serpapi(query, recency, limit, timeout)
    if provider == "serper":
        return _search_serper(query, recency, limit, timeout)
    if provider == "you":
        return _search_you(query, recency, limit, timeout)
    return []


def _search_tavily(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.tavily_api_key:
        raise _MissingKey()
    payload: dict[str, Any] = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": limit,
        "include_answer": False,
        "include_raw_content": False,
        "search_depth": "basic",
    }
    days = _recency_days(recency)
    if days:
        payload["days"] = days
    response = _session().post(
        "https://api.tavily.com/search",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    return [
        _normalize_result(
            title=item.get("title"),
            url=item.get("url"),
            content=item.get("content") or item.get("raw_content"),
            published_at=item.get("published_date"),
            provider="tavily",
            score=item.get("score"),
        )
        for item in rows or []
    ]


def _search_brave(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.brave_search_api_key:
        raise _MissingKey()
    params: dict[str, Any] = {"q": query, "count": min(limit, 10), "text_decorations": False}
    freshness = _brave_freshness(recency)
    if freshness:
        params["freshness"] = freshness
    response = _session().get(
        "https://api.search.brave.com/res/v1/web/search",
        params=params,
        headers={"Accept": "application/json", "X-Subscription-Token": settings.brave_search_api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows = (((data or {}).get("web") or {}).get("results") or []) if isinstance(data, dict) else []
    return [
        _normalize_result(
            title=item.get("title"),
            url=item.get("url"),
            content=item.get("description") or item.get("extra_snippets"),
            published_at=item.get("age"),
            provider="brave",
        )
        for item in rows
    ]


def _search_exa(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.exa_api_key:
        raise _MissingKey()
    payload: dict[str, Any] = {
        "query": query,
        "numResults": limit,
        "contents": {"text": {"maxCharacters": MAX_EXCERPT_CHARS}},
    }
    start = _published_after(recency)
    if start:
        payload["startPublishedDate"] = start
    response = _session().post(
        "https://api.exa.ai/search",
        json=payload,
        headers={"x-api-key": settings.exa_api_key, "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    return [
        _normalize_result(
            title=item.get("title"),
            url=item.get("url"),
            content=item.get("text") or item.get("summary"),
            published_at=item.get("publishedDate"),
            provider="exa",
            score=item.get("score"),
        )
        for item in rows or []
    ]


def _search_serpapi(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.serpapi_api_key:
        raise _MissingKey()
    params: dict[str, Any] = {"engine": "google", "q": query, "api_key": settings.serpapi_api_key, "num": limit}
    response = _session().get("https://serpapi.com/search", params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    rows = data.get("organic_results") if isinstance(data, dict) else []
    return [
        _normalize_result(
            title=item.get("title"),
            url=item.get("link"),
            content=item.get("snippet"),
            published_at=item.get("date"),
            provider="serpapi",
            score=item.get("position"),
        )
        for item in rows or []
    ]


def _search_serper(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.serper_api_key:
        raise _MissingKey()
    response = _session().post(
        "https://google.serper.dev/search",
        json={"q": query, "num": limit},
        headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("organic") if isinstance(data, dict) else []
    return [
        _normalize_result(
            title=item.get("title"),
            url=item.get("link"),
            content=item.get("snippet"),
            published_at=item.get("date"),
            provider="serper",
            score=item.get("position"),
        )
        for item in rows or []
    ]


def _search_you(query: str, recency: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    if not settings.you_api_key:
        raise _MissingKey()
    response = _session().get(
        "https://ydc-index.io/v1/search",
        params={"query": query, "num_web_results": limit},
        headers={"X-API-Key": settings.you_api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("hits") or data.get("results") if isinstance(data, dict) else []
    normalized = []
    for item in rows or []:
        snippets = item.get("snippets") if isinstance(item, dict) else None
        content = " ".join(snippets) if isinstance(snippets, list) else item.get("description")
        normalized.append(
            _normalize_result(
                title=item.get("title"),
                url=item.get("url"),
                content=content,
                published_at=item.get("date"),
                provider="you",
            )
        )
    return normalized


def _normalize_result(
    title: Any,
    url: Any,
    content: Any,
    published_at: Any = None,
    provider: str = "",
    score: Any = None,
) -> dict[str, Any]:
    clean_url = str(url or "").strip()
    if not clean_url:
        return {}
    text = content
    if isinstance(content, list):
        text = " ".join(str(item) for item in content)
    excerpt = _truncate(_clean_text(str(text or "")), MAX_EXCERPT_CHARS)
    domain = _domain(clean_url)
    return {
        "title": _truncate(_clean_text(str(title or domain or clean_url)), 160),
        "url": clean_url,
        "domain": domain,
        "excerpt": excerpt,
        "published_at": _clean_text(str(published_at or "")) or None,
        "provider": provider,
        "score": score,
        "credibility": _credibility(domain),
    }


def _build_sources(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources = []
    for index, item in enumerate([row for row in results if row.get("url")], start=1):
        source = {
            "source_id": f"src_{index}",
            "marker": index,
            "title": item.get("title") or item.get("domain") or item.get("url"),
            "url": item.get("url"),
            "domain": item.get("domain"),
            "published_at": item.get("published_at"),
            "credibility": item.get("credibility"),
            "excerpt": item.get("excerpt") or "",
            "provider": item.get("provider"),
        }
        sources.append(source)
    return sources


def _dedupe_and_rank(results: list[dict[str, Any]], source_policy: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique = []
    for item in results:
        if not item or not item.get("url"):
            continue
        key = _dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    if source_policy in {"official_first", "finance_first"}:
        unique.sort(key=lambda item: _rank_score(item, source_policy), reverse=True)
    return unique


def _rank_score(item: dict[str, Any], source_policy: str) -> int:
    credibility = item.get("credibility")
    if credibility == "official":
        return 4
    if source_policy == "finance_first" and credibility == "finance_media":
        return 3
    if credibility == "major_media":
        return 2
    return 1


def _dedupe_key(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "").split("#", 1)[0].rstrip("/")
    parsed = urlparse(url)
    path = re.sub(r"/+$", "", parsed.path or "")
    return f"{parsed.netloc.lower()}{path.lower()}" or f"{item.get('domain')}:{item.get('title')}"


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _credibility(domain: str) -> str:
    official_markers = (
        "gov",
        "sse.com.cn",
        "szse.cn",
        "hkex.com.hk",
        "sec.gov",
        "cninfo.com.cn",
        "cs.com.cn",
        "pbc.gov.cn",
        "csrc.gov.cn",
    )
    finance_markers = (
        "eastmoney.com",
        "10jqka.com.cn",
        "sina.com.cn",
        "stcn.com",
        "cls.cn",
        "yicai.com",
        "caixin.com",
        "wallstreetcn.com",
        "finance.yahoo.com",
        "reuters.com",
        "bloomberg.com",
    )
    major_media = ("xinhuanet.com", "people.com.cn", "cctv.com", "thepaper.cn")
    if any(marker in domain for marker in official_markers):
        return "official"
    if any(marker in domain for marker in finance_markers):
        return "finance_media"
    if any(marker in domain for marker in major_media):
        return "major_media"
    return "search_result"


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def _recency_days(recency: str) -> int | None:
    return {"day": 1, "week": 7, "month": 31, "year": 366}.get(str(recency or "").lower())


def _published_after(recency: str) -> str | None:
    days = _recency_days(recency)
    if not days:
        return None
    return (datetime.utcnow() - timedelta(days=days)).date().isoformat()


def _brave_freshness(recency: str) -> str | None:
    return {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(str(recency or "").lower())


def _error(status: str, query: str, intent: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "query": query,
        "intent": intent,
        "answerable": False,
        "sources": [],
        "claims": [{"claim": query, "verdict": "insufficient", "source_ids": [], "confidence": "low"}],
        "provider_attempts": [],
        "message": message,
    }
