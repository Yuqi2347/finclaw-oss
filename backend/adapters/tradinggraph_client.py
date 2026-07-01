from __future__ import annotations

import json as jsonlib
import os
from typing import Any

import requests


class TradingGraphError(RuntimeError):
    """Raised when the industry-chain capability backend cannot satisfy a request."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class TradingGraphClient:
    def __init__(self, base_url: str | None = None, timeout: int = 30) -> None:
        self.base_url = (base_url or os.getenv("TRADINGGRAPH_API_BASE") or "http://127.0.0.1:8900").rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(method, url, params=params, json=json, timeout=timeout or self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            payload = None
            try:
                payload = exc.response.json() if exc.response is not None else None
            except ValueError:
                payload = exc.response.text if exc.response is not None else None
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            message = f"产业链透视请求失败：{detail or exc}"
            raise TradingGraphError(message, exc.response.status_code if exc.response is not None else None, payload) from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TradingGraphError(f"产业链透视服务不可用：{exc}") from exc

        if not resp.content:
            return None
        try:
            return jsonlib.loads(resp.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TradingGraphError(f"产业链透视返回了非 JSON 响应：{path}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health", timeout=10)

    def create_run(self, mode: str, query: str, markets: list[str], budget: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/runs",
            json={"mode": mode, "query": query, "markets": markets, "budget": budget},
            timeout=20,
        )

    def get_resumable_run(self, mode: str | None = None, query: str | None = None) -> dict[str, Any] | None:
        params = {k: v for k, v in {"mode": mode, "query": query}.items() if v}
        return self._request("GET", "/api/runs/resumable", params=params, timeout=10)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/runs/{run_id}", timeout=10)

    def pause_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/pause", timeout=10)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/resume", timeout=10)

    def start_node(self, run_id: str, node_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/start-node/{node_id}", timeout=15)

    def enrich_nodes(self, run_id: str, node_ids: list[str], markets: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/runs/{run_id}/enrich-nodes",
            json={"node_ids": node_ids, "markets": markets},
            timeout=60,
        )

    def get_graph(self, mainline: str | None = None, include_osint: bool = True) -> dict[str, Any]:
        params = {"include_osint": include_osint}
        if mainline:
            params["mainline"] = mainline
        return self._request("GET", "/api/graph", params=params, timeout=20)

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/nodes/{node_id}", timeout=10)

    def list_mainlines(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/graph/mainlines", timeout=10)
        return data if isinstance(data, list) else []


tradinggraph_client = TradingGraphClient()
