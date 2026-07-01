from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from backend.adapters.tradinggraph_client import TradingGraphClient, TradingGraphError, tradinggraph_client
from backend.core.config import FINCLAW_API_BASE_URL
from backend.core.env import settings


VALID_MARKETS = {"CN", "US", "HK"}
DEFAULT_MARKETS = ["CN", "US", "HK"]
DEFAULT_BUDGET = {"max_nodes": 50, "max_depth": 5}
DEFAULT_TRADINGGRAPH_WEB_BASE = "http://127.0.0.1:5173"
SUMMARY_NODE_CHAR_BUDGET = 7_000
SUMMARY_DEFAULT_NODE_LIMIT = 300
SUMMARY_MAX_NODE_LIMIT = 500
NEIGHBOR_DEFAULT_LIMIT = 20
NEIGHBOR_MAX_LIMIT = 20
NEIGHBOR_MAX_DEPTH = 2
NODE_FIELD_CHAR_BUDGET = 11_000
NODE_FIELD_DEFAULT_LIMIT = 20
NODE_FIELD_MAX_LIMIT = 50
NODE_OVERVIEW_TEXT_LIMIT = 800
NODE_READABLE_FIELDS = {
    "tickers",
    "price_changes",
    "bottleneck_profile",
    "key_findings",
    "description",
    "tech_summary",
    "architecture_path",
    "bottleneck_signal",
    "supply_concentration",
    "geo_concentration",
    "pending_branches",
    "coverage_checklist",
    "audit",
    "evidence",
}


class TradingGraphService:
    def __init__(self, client: TradingGraphClient | None = None) -> None:
        self.client = client or tradinggraph_client
        self.web_base = (os.getenv("TRADINGGRAPH_WEB_BASE") or DEFAULT_TRADINGGRAPH_WEB_BASE).rstrip("/")
        self.root = self._resolve_root(os.getenv("TRADINGGRAPH_ROOT"), settings.finagent_root / "capabilities" / "tradinggraph")
        self.auto_start = os.getenv("TRADINGGRAPH_AUTO_START", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.start_timeout = self._env_int("TRADINGGRAPH_START_TIMEOUT", 20)
        self.web_auto_start = os.getenv("TRADINGGRAPH_WEB_AUTO_START", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.web_start_timeout = self._env_int("TRADINGGRAPH_WEB_START_TIMEOUT", 15)
        self.npm_executable = os.getenv("TRADINGGRAPH_NPM", "npm.cmd" if os.name == "nt" else "npm")
        self.python_executable = os.getenv("TRADINGGRAPH_PYTHON", settings.python_executable or sys.executable)
        self._start_lock = threading.Lock()
        self._frontend_start_lock = threading.Lock()

    @staticmethod
    def _resolve_root(value: str | None, default: Path) -> Path:
        raw = (value or "").strip()
        path = Path(raw).expanduser() if raw else default
        if not path.is_absolute():
            path = settings.project_root / path
        return path.resolve()

    def health(self) -> dict[str, Any]:
        return self.client.health()

    def ensure_service(self) -> dict[str, Any]:
        try:
            data = self.client.health()
            return self._service_state(data, backend_auto_started=False)
        except TradingGraphError as exc:
            if exc.status_code is not None:
                raise
            if not self.auto_start:
                raise TradingGraphError(
                    "产业链透视服务未运行，且 TRADINGGRAPH_AUTO_START 已禁用。"
                    f"启动命令：{self._start_command_text()}"
                ) from exc

        with self._start_lock:
            try:
                data = self.client.health()
                return self._service_state(data, backend_auto_started=False)
            except TradingGraphError as exc:
                if exc.status_code is not None:
                    raise

            self._start_backend_process()
            deadline = time.time() + self.start_timeout
            last_error = ""
            while time.time() < deadline:
                try:
                    data = self.client.health()
                    return self._service_state(data, backend_auto_started=True)
                except TradingGraphError as exc:
                    last_error = str(exc)
                    time.sleep(0.5)

        raise TradingGraphError(
            "产业链透视服务启动超时。"
            f"启动命令：{self._start_command_text()}。最后错误：{last_error}"
        )

    def control_industry_graph(
        self,
        action: str,
        mode: str = "mainline",
        query: str = "",
        run_id: str = "",
        node_ids: list[str] | None = None,
        markets: list[str] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = (action or "").strip()
        mode = (mode or "mainline").strip()
        query = (query or "").strip()
        run_id = (run_id or "").strip()
        node_ids = self._clean_list(node_ids)
        markets = self._clean_markets(markets)
        budget = self._clean_budget(budget, markets)

        if mode not in {"mainline", "ticker"}:
            raise ValueError("mode must be mainline or ticker")
        service_state = self.ensure_service()

        if action == "start_or_resume":
            if not query:
                raise ValueError("query is required for start_or_resume")
            run = self.client.get_resumable_run(mode=mode, query=query)
            used_existing = bool(run)
            if not run:
                run = self.client.create_run(mode, query, markets, budget)
            return self._run_result(action, run, used_existing=used_existing, service=service_state)

        if action == "pause":
            self._require_run(run_id, action)
            return self._run_result(action, self.client.pause_run(run_id), service=service_state)

        if action == "resume":
            self._require_run(run_id, action)
            return self._run_result(action, self.client.resume_run(run_id), service=service_state)

        if action == "continue_from_node":
            self._require_run(run_id, action)
            if len(node_ids) != 1:
                raise ValueError("continue_from_node requires exactly one node_id")
            return self._run_result(action, self.client.start_node(run_id, node_ids[0]), node_id=node_ids[0], service=service_state)

        if action == "enrich_nodes":
            self._require_run(run_id, action)
            if not node_ids:
                raise ValueError("enrich_nodes requires node_ids")
            data = self.client.enrich_nodes(run_id, node_ids, markets)
            return {
                "ok": True,
                "action": action,
                "run_id": run_id,
                "node_ids": node_ids,
                "result": data,
                "view_url": self._view_url(run_id=run_id),
                "service": service_state,
            }

        raise ValueError("action must be start_or_resume, pause, resume, continue_from_node, or enrich_nodes")

    def read_industry_graph(
        self,
        action: str = "list_mainlines",
        mainline: str = "",
        run_id: str = "",
        node_id: str = "",
        include_osint: bool = True,
        offset: int = 0,
        limit: int = 0,
        depth: int = 1,
    ) -> dict[str, Any]:
        action = (action or "list_mainlines").strip()
        mainline = (mainline or "").strip()
        run_id = (run_id or "").strip()
        node_id = (node_id or "").strip()
        service_state = self.ensure_service()

        if action == "list_mainlines":
            mainlines = self.client.list_mainlines()
            return {
                "ok": True,
                "action": action,
                "mainlines": self._compact_mainlines(mainlines),
                "count": len(mainlines),
                "view_url": self._view_url(),
                "api_view_url": self._api_view_url(),
                "service": service_state,
            }

        if action == "get_graph_summary":
            graph = self.client.get_graph(mainline=mainline or None, include_osint=include_osint)
            summary = self._summarize_graph(graph, offset=offset, limit=limit)
            return {
                "ok": True,
                "action": action,
                "mainline": mainline,
                "summary": summary,
                "graph_status": summary["graph_status"],
                "view_url": self._view_url(mainline=mainline or None),
                "api_view_url": self._api_view_url(mainline=mainline or None),
                "service": service_state,
            }

        if action == "get_node_neighbors":
            if not node_id:
                raise ValueError("node_id is required for get_node_neighbors")
            graph = self.client.get_graph(mainline=mainline or None, include_osint=include_osint)
            resolved_node_id = self._resolve_node_ref(graph, node_id) or node_id
            return {
                "ok": True,
                "action": action,
                "mainline": mainline,
                "node_id": resolved_node_id,
                "requested_node_id": node_id,
                "neighbors": self._node_neighbors(graph, resolved_node_id, depth=depth, offset=offset, limit=limit),
                "view_url": self._view_url(mainline=mainline or None, node_id=resolved_node_id),
                "api_view_url": self._api_view_url(mainline=mainline or None, node_id=resolved_node_id),
                "service": service_state,
            }

        if action == "get_run_status":
            self._require_run(run_id, action)
            run = self.client.get_run(run_id)
            return self._run_result(action, run, service=service_state)

        if action == "get_resumable_run":
            run = self.client.get_resumable_run()
            return {
                "ok": True,
                "action": action,
                "run": run,
                "view_url": self._view_url(run_id=run.get("id") if isinstance(run, dict) else None),
                "api_view_url": self._api_view_url(run_id=run.get("id") if isinstance(run, dict) else None),
                "service": service_state,
            }

        raise ValueError("action must be list_mainlines, get_graph_summary, get_node_neighbors, get_run_status, or get_resumable_run")

    def read_industry_graph_node(
        self,
        node_id: str,
        include_neighbors: bool = False,
        mainline: str = "",
        include_osint: bool = True,
        mode: str = "overview",
        field: str = "",
        offset: int = 0,
        limit: int = 0,
        max_chars: int = NODE_FIELD_CHAR_BUDGET,
    ) -> dict[str, Any]:
        node_id = (node_id or "").strip()
        if not node_id:
            raise ValueError("node_id is required")
        mode = (mode or "overview").strip().lower()
        if mode not in {"overview", "field"}:
            raise ValueError("mode must be overview or field")
        service_state = self.ensure_service()
        resolved_node_id = node_id
        if self._is_node_ref(node_id):
            graph = self.client.get_graph(mainline=(mainline or "").strip() or None, include_osint=include_osint)
            resolved_node_id = self._resolve_node_ref(graph, node_id) or node_id
        node = self.client.get_node(resolved_node_id)
        node_payload = self._node_overview_payload(node) if mode == "overview" else self._node_field_payload(
            node,
            field=field,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "requested_node_id": node_id,
            "node_id": resolved_node_id,
            **node_payload,
            "view_url": self._view_url(node_id=resolved_node_id),
            "api_view_url": self._api_view_url(node_id=resolved_node_id),
            "service": service_state,
        }
        if include_neighbors:
            graph = self.client.get_graph(mainline=(mainline or "").strip() or None, include_osint=include_osint)
            payload["neighbors"] = self._node_neighbors(graph, resolved_node_id, depth=1, offset=0, limit=NEIGHBOR_DEFAULT_LIMIT)
            payload["view_url"] = self._view_url(mainline=(mainline or "").strip() or None, node_id=resolved_node_id)
            payload["api_view_url"] = self._api_view_url(mainline=(mainline or "").strip() or None, node_id=resolved_node_id)
        return payload

    def _node_overview_payload(self, node: dict[str, Any]) -> dict[str, Any]:
        readable_fields = self._node_field_manifest(node)
        return {
            "node": {
                "id": node.get("id"),
                "name": node.get("canonical_name") or self._first_name(node),
                "type": node.get("type"),
                "mainlines": node.get("mainlines") or [],
                "research_status": node.get("research_status"),
                "research_depth": node.get("research_depth"),
                "confidence": node.get("confidence"),
                "description": self._trim_text(node.get("description"), NODE_OVERVIEW_TEXT_LIMIT),
                "tech_summary": self._trim_text(node.get("tech_summary"), NODE_OVERVIEW_TEXT_LIMIT),
                "key_findings_preview": self._trim_text(node.get("key_findings"), NODE_OVERVIEW_TEXT_LIMIT),
                "tickers_preview": self._compact_tickers(node.get("tickers") or [], limit=8),
                "price_changes": node.get("price_changes") or {},
                "bottleneck_score": self._bottleneck_score(node.get("bottleneck_profile") or {}),
            },
            "readable_fields": readable_fields,
            "read_protocol": {
                "next_step": "Use mode='field' with one readable field only when the user question needs that detail.",
                "pagination": "If a field result has has_more=true, continue the same field with next_offset.",
                "multi_node": "For multiple nodes, call this tool once per node and stop when enough evidence is collected.",
            },
        }

    def _node_field_payload(
        self,
        node: dict[str, Any],
        field: str,
        offset: int = 0,
        limit: int = 0,
        max_chars: int = NODE_FIELD_CHAR_BUDGET,
    ) -> dict[str, Any]:
        field = (field or "").strip()
        if not field:
            raise ValueError("field is required when mode=field")
        root_field = field.split(".", 1)[0]
        if root_field not in NODE_READABLE_FIELDS:
            raise ValueError(f"field is not readable: {field}")

        value = self._get_field_path(node, field)
        if value is None and root_field == "evidence":
            value = self._extract_top_level_evidence(node)
        max_chars = max(1_000, min(int(max_chars or NODE_FIELD_CHAR_BUDGET), NODE_FIELD_CHAR_BUDGET))
        result = self._page_value(value, offset=offset, limit=limit, max_chars=max_chars)
        return {
            "node": {
                "id": node.get("id"),
                "name": node.get("canonical_name") or self._first_name(node),
                "type": node.get("type"),
            },
            "field": field,
            "field_chars": self._char_count(value),
            "field_result": result,
            "readable_fields": self._node_field_manifest(node),
        }

    def _node_field_manifest(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        manifest = []
        for field in sorted(NODE_READABLE_FIELDS):
            value = self._extract_top_level_evidence(node) if field == "evidence" else node.get(field)
            if self._is_empty_value(value):
                continue
            chars = self._char_count(value)
            manifest.append(
                {
                    "field": field,
                    "chars": chars,
                    "items": self._item_count(value),
                    "paged": chars > NODE_FIELD_CHAR_BUDGET or self._item_count(value) > NODE_FIELD_DEFAULT_LIMIT,
                    "kind": type(value).__name__,
                }
            )
        return manifest

    def _page_value(self, value: Any, offset: int = 0, limit: int = 0, max_chars: int = NODE_FIELD_CHAR_BUDGET) -> dict[str, Any]:
        offset = max(0, int(offset or 0))
        requested_limit = int(limit or NODE_FIELD_DEFAULT_LIMIT)
        applied_limit = max(1, min(requested_limit, NODE_FIELD_MAX_LIMIT))
        total_chars = self._char_count(value)

        if isinstance(value, list):
            return self._page_list(value, offset=offset, limit=applied_limit, requested_limit=requested_limit, max_chars=max_chars, total_chars=total_chars)
        if isinstance(value, dict):
            return self._page_mapping(value, offset=offset, limit=applied_limit, requested_limit=requested_limit, max_chars=max_chars, total_chars=total_chars)

        text = "" if value is None else str(value)
        end = min(offset + max_chars, len(text))
        return {
            "kind": "text",
            "content": text[offset:end],
            "offset": offset,
            "max_chars": max_chars,
            "total_chars": len(text),
            "has_more": end < len(text),
            "next_offset": end if end < len(text) else None,
        }

    def _page_list(
        self,
        value: list[Any],
        offset: int,
        limit: int,
        requested_limit: int,
        max_chars: int,
        total_chars: int,
    ) -> dict[str, Any]:
        page = []
        used_chars = 0
        next_index = offset
        for index, item in enumerate(value[offset : offset + limit], start=offset):
            item_chars = self._char_count(item)
            if item_chars > max_chars:
                item = self._summarize_oversized_value(item)
                item_chars = self._char_count(item)
            if page and used_chars + item_chars > max_chars:
                break
            page.append(item)
            used_chars += item_chars
            next_index = index + 1
        has_more = next_index < len(value)
        return {
            "kind": "list",
            "items": page,
            "offset": offset,
            "limit": limit,
            "requested_limit": requested_limit,
            "returned": len(page),
            "total_items": len(value),
            "total_chars": total_chars,
            "returned_chars": self._char_count(page),
            "has_more": has_more,
            "next_offset": next_index if has_more else None,
        }

    def _page_mapping(
        self,
        value: dict[str, Any],
        offset: int,
        limit: int,
        requested_limit: int,
        max_chars: int,
        total_chars: int,
    ) -> dict[str, Any]:
        keys = list(value)
        selected: dict[str, Any] = {}
        page_keys = keys[offset : offset + limit]
        key_manifest = [{"key": key, "chars": self._char_count(value.get(key)), "kind": type(value.get(key)).__name__} for key in page_keys]
        used_chars = 0
        next_index = offset
        for index, key in enumerate(page_keys, start=offset):
            item = value.get(key)
            item_chars = self._char_count({key: item})
            if selected and used_chars + item_chars > max_chars:
                break
            if item_chars > max_chars:
                selected[key] = self._summarize_oversized_value(item)
            else:
                selected[key] = item
            used_chars += self._char_count({key: selected[key]})
            next_index = index + 1
        has_more = next_index < len(keys)
        return {
            "kind": "dict",
            "items": selected,
            "child_field_manifest": key_manifest,
            "offset": offset,
            "limit": limit,
            "requested_limit": requested_limit,
            "returned": len(selected),
            "total_items": len(keys),
            "total_chars": total_chars,
            "returned_chars": self._char_count(selected),
            "has_more": has_more,
            "next_offset": next_index if has_more else None,
            "note": "For a large child item, request it with field='<field>.<key>' instead of expanding the parent.",
        }

    def _summarize_oversized_value(self, value: Any) -> dict[str, Any]:
        return {
            "omitted": True,
            "chars": self._char_count(value),
            "kind": type(value).__name__,
            "reason": "child value exceeds the single-read budget",
        }

    def _extract_top_level_evidence(self, node: dict[str, Any]) -> Any:
        evidence = node.get("evidence")
        if evidence:
            return evidence
        sources = node.get("sources")
        if sources:
            return sources
        return None

    def _get_field_path(self, node: dict[str, Any], field: str) -> Any:
        current: Any = node
        for part in field.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else None
            else:
                return None
        return current

    def _first_name(self, node: dict[str, Any]) -> str:
        names = node.get("names") or []
        return names[0] if isinstance(names, list) and names else ""

    @staticmethod
    def _trim_text(value: Any, max_chars: int) -> str:
        text = "" if value is None else str(value).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20].rstrip() + "...[trimmed]"

    @staticmethod
    def _char_count(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _item_count(value: Any) -> int:
        if isinstance(value, (list, dict)):
            return len(value)
        return 1 if value not in (None, "") else 0

    @staticmethod
    def _is_empty_value(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    @staticmethod
    def _bottleneck_score(profile: dict[str, Any]) -> Any:
        if not isinstance(profile, dict):
            return None
        return profile.get("aggregate_score") or profile.get("score") or profile.get("bottleneck_score")

    def _run_result(self, action: str, run: dict[str, Any], **extra: Any) -> dict[str, Any]:
        run_id = str(run.get("id") or "")
        return {
            "ok": True,
            "action": action,
            "run": run,
            "view_url": self._view_url(run_id=run_id or None),
            "api_view_url": self._api_view_url(run_id=run_id or None),
            **extra,
        }

    def _service_state(self, health: dict[str, Any], backend_auto_started: bool) -> dict[str, Any]:
        return {
            "status": "running",
            "auto_started": backend_auto_started,
            "health": health,
            "frontend": self.ensure_frontend(),
        }

    def ensure_frontend(self) -> dict[str, Any]:
        if not self.web_base:
            return {"status": "disabled", "reason": "TRADINGGRAPH_WEB_BASE is empty"}
        if self._frontend_running():
            return {"status": "running", "auto_started": False, "url": self.web_base}
        if not self.web_auto_start:
            return {
                "status": "unavailable",
                "auto_started": False,
                "url": self.web_base,
                "reason": "TRADINGGRAPH_WEB_AUTO_START is disabled",
                "start_command": self._start_frontend_command_text(),
            }

        with self._frontend_start_lock:
            if self._frontend_running():
                return {"status": "running", "auto_started": False, "url": self.web_base}
            try:
                self._start_frontend_process()
            except Exception as exc:
                return {
                    "status": "unavailable",
                    "auto_started": False,
                    "url": self.web_base,
                    "error": str(exc),
                    "start_command": self._start_frontend_command_text(),
                }

            deadline = time.time() + self.web_start_timeout
            last_error = ""
            while time.time() < deadline:
                if self._frontend_running():
                    return {"status": "running", "auto_started": True, "url": self.web_base}
                last_error = f"{self.web_base} not ready"
                time.sleep(0.5)

        return {
            "status": "starting",
            "auto_started": True,
            "url": self.web_base,
            "note": "frontend process was started but did not answer before timeout",
            "last_error": last_error,
            "start_command": self._start_frontend_command_text(),
        }

    def _summarize_graph(self, graph: dict[str, Any], offset: int = 0, limit: int = 0) -> dict[str, Any]:
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        mainlines = graph.get("mainlines") or []
        node_types = Counter(str(node.get("type") or "unknown") for node in nodes)
        relation_types = Counter(str(edge.get("type") or edge.get("relation") or edge.get("relation_type") or edge.get("label") or "unknown") for edge in edges)
        ranked_nodes = sorted(nodes, key=self._node_priority_score, reverse=True)
        mainline_items = self._compact_mainlines(mainlines)
        node_page = self._node_directory_page(ranked_nodes, offset=offset, limit=limit)
        graph_status = {
            "has_graph_data": bool(nodes or edges),
            "is_empty": not bool(nodes or edges),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "mainline_count": len(mainlines),
            "metadata_note": "mainline status/run_id may be null even when graph data exists; use graph_status node_count/edge_count as truth.",
        }
        return {
            "graph_status": graph_status,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "mainline_count": len(mainlines),
            "node_types": dict(node_types),
            "relation_types": dict(relation_types),
            "mainlines": mainline_items,
            "nodes": node_page,
            "next_reads": [
                "read_industry_graph(action='get_graph_summary', mainline='<mainline>', offset=<next_offset>, limit=<limit>)",
                "read_industry_graph(action='get_node_neighbors', node_id='<node_id>', mainline='<mainline>')",
                "read_industry_graph_node(node_id='<node_id>', mode='overview')",
                "read_industry_graph_node(node_id='<node_id>', mode='field', field='<readable_field>', offset=<next_offset>)",
            ],
        }

    def _node_priority_score(self, node: dict[str, Any]) -> float:
        score = self._safe_float(node.get("confidence"))
        text = " ".join(
            str(node.get(key) or "")
            for key in ["research_depth", "research_status", "key_findings", "supply_concentration", "geo_concentration"]
        ).lower()
        signal = node.get("bottleneck_signal") or {}
        profile = node.get("bottleneck_profile") or {}
        if isinstance(signal, dict):
            score += self._safe_float(signal.get("score") or signal.get("bottleneck_score"))
            if str(signal.get("level") or signal.get("severity") or "").lower() in {"high", "critical"}:
                score += 2
        if isinstance(profile, dict) and profile:
            score += 0.5
        if any(keyword in text for keyword in ["bottleneck", "卡脖子", "瓶颈", "high", "critical"]):
            score += 1
        return score

    def _node_directory_page(self, nodes: list[dict[str, Any]], offset: int = 0, limit: int = 0) -> dict[str, Any]:
        total = len(nodes)
        offset = max(0, min(int(offset or 0), total))
        requested_limit = int(limit or SUMMARY_DEFAULT_NODE_LIMIT)
        page_limit = max(1, min(requested_limit, SUMMARY_MAX_NODE_LIMIT))
        page: list[dict[str, Any]] = []
        used_chars = 2
        next_index = offset
        for index, node in enumerate(nodes[offset : offset + page_limit], start=offset):
            item = self._compact_node_directory_item(node, index=index)
            item_chars = len(str(item))
            if page and used_chars + item_chars > SUMMARY_NODE_CHAR_BUDGET:
                break
            page.append(item)
            used_chars += item_chars
            next_index = index + 1
        truncated = next_index < total
        return {
            "items": page,
            "total": total,
            "offset": offset,
            "limit": page_limit,
            "returned": len(page),
            "truncated": truncated,
            "next_offset": next_index if truncated else None,
            "sort": "bottleneck_score_desc",
            "note": "This is a lightweight node directory. Use node_ref plus mainline with read_industry_graph_node or get_node_neighbors for details.",
        }

    def _compact_node_directory_item(self, node: dict[str, Any], index: int | None = None) -> dict[str, Any]:
        names = node.get("names") or []
        fallback_name = names[0] if isinstance(names, list) and names else ""
        item = {
            "name": node.get("canonical_name") or fallback_name,
            "type": node.get("type"),
            "score": round(self._node_priority_score(node), 4),
        }
        if index is None:
            item["id"] = node.get("id")
        else:
            item["node_ref"] = self._node_ref(index)
        return item

    def _compact_node(self, node: dict[str, Any]) -> dict[str, Any]:
        names = node.get("names") or []
        fallback_name = names[0] if isinstance(names, list) and names else ""
        return {
            "id": node.get("id"),
            "name": node.get("canonical_name") or fallback_name,
            "type": node.get("type"),
            "mainlines": node.get("mainlines") or [],
            "research_status": node.get("research_status"),
            "research_depth": node.get("research_depth"),
            "confidence": node.get("confidence"),
            "ticker_symbols": self._compact_ticker_symbols(node.get("tickers") or [], limit=6),
            "has_key_findings": bool(str(node.get("key_findings") or "").strip()),
        }

    def _compact_mainlines(self, mainlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted = []
        for item in mainlines:
            value = {
                "id": item.get("id"),
                "name": item.get("name"),
                "status": item.get("status") or item.get("research_status"),
                "run_id": item.get("run_id") or item.get("active_run_id"),
                "node_count": item.get("node_count") or item.get("nodes_count"),
                "edge_count": item.get("edge_count") or item.get("edges_count"),
                "updated_at": item.get("updated_at") or item.get("modified_at") or item.get("created_at"),
                "color": item.get("color"),
            }
            compacted.append({key: val for key, val in value.items() if val is not None})
        return compacted

    def _key_edges(self, edges: list[dict[str, Any]], priority_node_ids: set[str], limit: int = 18) -> list[dict[str, Any]]:
        scored = sorted(edges, key=lambda edge: self._edge_priority_score(edge, priority_node_ids), reverse=True)
        return [self._compact_edge(edge) for edge in scored[:limit]]

    def _edge_priority_score(self, edge: dict[str, Any], priority_node_ids: set[str]) -> float:
        source = self._edge_source_id(edge)
        target = self._edge_target_id(edge)
        score = self._safe_float(edge.get("confidence") or edge.get("weight") or edge.get("score"))
        if source in priority_node_ids:
            score += 2
        if target in priority_node_ids:
            score += 2
        text = " ".join(str(edge.get(key) or "") for key in ["type", "relation", "label", "evidence", "summary"]).lower()
        if any(keyword in text for keyword in ["bottleneck", "卡脖子", "瓶颈", "supplier", "supply", "上游", "下游"]):
            score += 1
        return score

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _compact_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": edge.get("id"),
            "source": self._edge_source_id(edge),
            "target": self._edge_target_id(edge),
            "type": edge.get("type") or edge.get("relation") or edge.get("relation_type") or edge.get("label"),
            "confidence": edge.get("confidence") or edge.get("weight") or edge.get("score"),
            "has_summary": bool(str(edge.get("summary") or edge.get("evidence") or edge.get("description") or "").strip()),
        }

    def _bottleneck_chains(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        node_names = {str(node.get("id") or ""): self._compact_node(node).get("name") for node in nodes}
        chains: list[dict[str, Any]] = []
        for edge in edges[:10]:
            source = self._edge_source_id(edge)
            target = self._edge_target_id(edge)
            if source in node_names or target in node_names:
                chains.append(
                    {
                        "source": {"id": source, "name": node_names.get(source) or source},
                        "target": {"id": target, "name": node_names.get(target) or target},
                        "relation": edge.get("type"),
                    }
                )
        return chains[:8]

    def _node_neighbors(self, graph: dict[str, Any], node_id: str, depth: int = 1, offset: int = 0, limit: int = 0) -> dict[str, Any]:
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        node_map = {str(node.get("id") or ""): node for node in nodes}
        requested_depth = int(depth or 1)
        applied_depth = max(1, min(requested_depth, NEIGHBOR_MAX_DEPTH))
        offset = max(0, int(offset or 0))
        requested_limit = int(limit or NEIGHBOR_DEFAULT_LIMIT)
        applied_limit = max(1, min(requested_limit, NEIGHBOR_MAX_LIMIT))
        adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for edge in edges:
            source = self._edge_source_id(edge)
            target = self._edge_target_id(edge)
            if not source or not target:
                continue
            adjacency.setdefault(source, []).append((target, edge))
            adjacency.setdefault(target, []).append((source, edge))

        visited = {node_id}
        frontier = {node_id}
        related_ids: dict[str, int] = {}
        related_edges_by_id: dict[str, dict[str, Any]] = {}
        for level in range(1, applied_depth + 1):
            next_frontier: set[str] = set()
            for current in frontier:
                for neighbor_id, edge in adjacency.get(current, []):
                    edge_id = str(edge.get("id") or f"{self._edge_source_id(edge)}>{self._edge_target_id(edge)}")
                    related_edges_by_id.setdefault(edge_id, edge)
                    if neighbor_id not in visited:
                        related_ids.setdefault(neighbor_id, level)
                        next_frontier.add(neighbor_id)
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break

        sorted_node_ids = sorted(related_ids, key=lambda item: (related_ids[item], item))
        sorted_edges = sorted(related_edges_by_id.values(), key=lambda edge: str(edge.get("id") or ""))
        total_items = max(len(sorted_node_ids), len(sorted_edges))
        page_node_ids = sorted_node_ids[offset : offset + applied_limit]
        page_edges = sorted_edges[offset : offset + applied_limit]
        next_offset = offset + applied_limit if offset + applied_limit < total_items else None
        return {
            "center": self._compact_node_directory_item(node_map[node_id]) if node_id in node_map else {"id": node_id},
            "nodes": [
                {**self._compact_node_directory_item(node_map[item]), "distance": related_ids[item]}
                for item in page_node_ids
                if item in node_map
            ],
            "edges": [self._compact_edge(edge) for edge in page_edges],
            "depth": applied_depth,
            "requested_depth": requested_depth,
            "max_depth": NEIGHBOR_MAX_DEPTH,
            "limit": applied_limit,
            "requested_limit": requested_limit,
            "max_limit": NEIGHBOR_MAX_LIMIT,
            "offset": offset,
            "node_count": len(sorted_node_ids),
            "edge_count": len(sorted_edges),
            "returned_nodes": len(page_node_ids),
            "returned_edges": len(page_edges),
            "truncated": next_offset is not None or requested_depth > applied_depth or requested_limit > applied_limit,
            "next_offset": next_offset,
            "note": "Neighbors are capped and paged. Increase offset to continue; depth is capped to avoid oversized context.",
        }

    @staticmethod
    def _edge_source_id(edge: dict[str, Any]) -> str:
        return str(edge.get("source_id") or edge.get("from_id") or edge.get("from") or edge.get("source_node_id") or "")

    @staticmethod
    def _edge_target_id(edge: dict[str, Any]) -> str:
        return str(edge.get("target_id") or edge.get("to_id") or edge.get("to") or edge.get("target_node_id") or "")

    def _resolve_node_ref(self, graph: dict[str, Any], node_ref: str) -> str | None:
        if not self._is_node_ref(node_ref):
            return node_ref
        try:
            index = int(node_ref[1:]) - 1
        except ValueError:
            return None
        nodes = sorted(graph.get("nodes") or [], key=self._node_priority_score, reverse=True)
        if 0 <= index < len(nodes):
            return str(nodes[index].get("id") or "") or None
        return None

    @staticmethod
    def _is_node_ref(value: str) -> bool:
        text = (value or "").strip().lower()
        return len(text) == 4 and text.startswith("n") and text[1:].isdigit()

    @staticmethod
    def _node_ref(index: int) -> str:
        return f"n{index + 1:03d}"

    def _api_view_url(self, mainline: str | None = None, node_id: str | None = None, run_id: str | None = None) -> str:
        params = {k: v for k, v in {"mainline": mainline, "node_id": node_id, "run_id": run_id}.items() if v}
        suffix = f"?{urlencode(params)}" if params else ""
        return f"{FINCLAW_API_BASE_URL}/api/tradinggraph/view{suffix}"

    def _view_url(self, mainline: str | None = None, node_id: str | None = None, run_id: str | None = None) -> str:
        return self.external_view_url(mainline=mainline, node_id=node_id, run_id=run_id) or self._api_view_url(
            mainline=mainline,
            node_id=node_id,
            run_id=run_id,
        )

    def external_view_url(self, mainline: str | None = None, node_id: str | None = None, run_id: str | None = None) -> str:
        if not self.web_base:
            return ""
        params = {k: v for k, v in {"mainline": mainline, "node_id": node_id, "run_id": run_id}.items() if v}
        suffix = f"?{urlencode(params)}" if params else ""
        return f"{self.web_base}{suffix}"

    @staticmethod
    def _truncate_text(value: Any, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars].rstrip()}..."

    @staticmethod
    def _compact_tickers(items: list[Any], limit: int) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or item.get("ts_code") or "").strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            compacted.append(
                {
                    "symbol": symbol,
                    "name": item.get("name") or item.get("short_name") or item.get("company_name"),
                    "market": item.get("market") or item.get("exchange"),
                }
            )
            if len(compacted) >= limit:
                break
        return compacted

    @staticmethod
    def _compact_ticker_symbols(items: list[Any], limit: int) -> list[str]:
        symbols: list[str] = []
        seen: set[str] = set()
        for item in items:
            symbol = ""
            if isinstance(item, dict):
                symbol = str(item.get("symbol") or item.get("ts_code") or "").strip()
            elif isinstance(item, str):
                symbol = item.strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) >= limit:
                break
        return symbols

    def _frontend_running(self) -> bool:
        try:
            resp = requests.get(self.web_base, timeout=2)
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def _start_backend_process(self) -> None:
        if not self.root.exists():
            raise TradingGraphError(f"产业链透视根目录不存在：{self.root}")
        parsed = urlparse(self.client.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8900
        log_dir = Path(os.getenv("TRADINGGRAPH_LOG_DIR", str(self.root / "logs"))).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "tradinggraph-backend.stdout.log"
        stderr_path = log_dir / "tradinggraph-backend.stderr.log"
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        cmd = [
            self.python_executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
            subprocess.Popen(
                cmd,
                cwd=str(self.root),
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )

    def _start_frontend_process(self) -> None:
        frontend_root = self.root / "frontend"
        package_json = frontend_root / "package.json"
        if not package_json.exists():
            raise TradingGraphError(f"产业链透视前端 package.json 不存在：{package_json}")
        log_dir = Path(os.getenv("TRADINGGRAPH_LOG_DIR", str(self.root / "logs"))).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "tradinggraph-frontend.stdout.log"
        stderr_path = log_dir / "tradinggraph-frontend.stderr.log"
        env = os.environ.copy()
        env.setdefault("BROWSER", "none")
        env.setdefault("VITE_TRADINGGRAPH_API_BASE", self.client.base_url)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        with stdout_path.open("ab") as stdout_file, stderr_path.open("ab") as stderr_file:
            subprocess.Popen(
                [self.npm_executable, "run", "dev"],
                cwd=str(frontend_root),
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )

    def _start_command_text(self) -> str:
        parsed = urlparse(self.client.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8900
        return (
            f'cd /d "{self.root}" && "{self.python_executable}" -m uvicorn '
            f"backend.main:app --host {host} --port {port}"
        )

    def _start_frontend_command_text(self) -> str:
        frontend_root = self.root / "frontend"
        return (
            f'cd /d "{frontend_root}" && set VITE_TRADINGGRAPH_API_BASE={self.client.base_url} '
            f'&& "{self.npm_executable}" run dev'
        )

    @staticmethod
    def _clean_list(values: list[str] | None) -> list[str]:
        return [str(item).strip() for item in (values or []) if str(item).strip()]

    @staticmethod
    def _clean_markets(markets: list[str] | None) -> list[str]:
        cleaned = [str(item).strip().upper() for item in (markets or DEFAULT_MARKETS)]
        cleaned = [item for item in cleaned if item in VALID_MARKETS]
        return cleaned or DEFAULT_MARKETS

    @staticmethod
    def _clean_budget(budget: dict[str, Any] | None, markets: list[str]) -> dict[str, Any]:
        cleaned = {**DEFAULT_BUDGET, **(budget or {})}
        cleaned["markets"] = markets
        return cleaned

    @staticmethod
    def _require_run(run_id: str, action: str) -> None:
        if not run_id:
            raise ValueError(f"run_id is required for {action}")

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default


tradinggraph_service = TradingGraphService()
