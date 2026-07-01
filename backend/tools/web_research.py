from __future__ import annotations

from typing import Any

from backend.services.web_research import web_research as _web_research


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
    return _web_research(
        query=query,
        intent=intent,
        recency=recency,
        max_sources=max_sources,
        queries=queries,
        max_sources_per_query=max_sources_per_query,
        total_source_budget=total_source_budget,
        source_policy=source_policy,
    )
