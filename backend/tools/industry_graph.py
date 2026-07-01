from __future__ import annotations

from typing import Any

from backend.services.tradinggraph_service import tradinggraph_service


def control_industry_graph(
    action: str,
    mode: str = "mainline",
    query: str = "",
    run_id: str = "",
    node_ids: list[str] | None = None,
    markets: list[str] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return tradinggraph_service.control_industry_graph(
        action=action,
        mode=mode,
        query=query,
        run_id=run_id,
        node_ids=node_ids,
        markets=markets,
        budget=budget,
    )


def read_industry_graph(
    action: str = "list_mainlines",
    mainline: str = "",
    run_id: str = "",
    node_id: str = "",
    include_osint: bool = True,
    offset: int = 0,
    limit: int = 0,
    depth: int = 1,
) -> dict[str, Any]:
    return tradinggraph_service.read_industry_graph(
        action=action,
        mainline=mainline,
        run_id=run_id,
        node_id=node_id,
        include_osint=include_osint,
        offset=offset,
        limit=limit,
        depth=depth,
    )


def read_industry_graph_node(
    node_id: str,
    include_neighbors: bool = False,
    mainline: str = "",
    include_osint: bool = True,
    mode: str = "overview",
    field: str = "",
    offset: int = 0,
    limit: int = 0,
    max_chars: int = 11000,
) -> dict[str, Any]:
    return tradinggraph_service.read_industry_graph_node(
        node_id,
        include_neighbors=include_neighbors,
        mainline=mainline,
        include_osint=include_osint,
        mode=mode,
        field=field,
        offset=offset,
        limit=limit,
        max_chars=max_chars,
    )
