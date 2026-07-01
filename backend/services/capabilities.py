from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from backend.core.config import (
    BETTAFISH_ROOT,
    DATA_DIR,
    DEFAULT_PYTHON,
    FINAGENT_ROOT,
    FINCLAW_API_BASE_URL,
    PROJECT_ROOT,
    TRADINGAGENTS_ROOT,
)


CAPABILITIES_ROOT = PROJECT_ROOT / "capabilities"
SETTINGS_PATH = DATA_DIR / "capabilities" / "settings.json"


@dataclass(frozen=True)
class CapabilityModule:
    id: str
    name: str
    display_name: str
    english_name: str
    aliases: tuple[str, ...]
    visibility: str
    category: str
    description: str
    best_for: tuple[str, ...]
    skill: str
    tools: tuple[str, ...]
    permissions: tuple[str, ...]
    default_enabled: bool
    default_timeout_seconds: int
    health: dict[str, Any]
    implementation: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CapabilityModule":
        return cls(
            id=str(payload.get("id") or "").strip(),
            name=str(payload.get("name") or "").strip(),
            display_name=str(payload.get("display_name") or payload.get("name") or "").strip(),
            english_name=str(payload.get("english_name") or "").strip(),
            aliases=tuple(str(item).strip() for item in payload.get("aliases", []) if str(item).strip()),
            visibility=str(payload.get("visibility") or "external").strip(),
            category=str(payload.get("category") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            best_for=tuple(str(item).strip() for item in payload.get("best_for", []) if str(item).strip()),
            skill=str(payload.get("skill") or "").strip(),
            tools=tuple(str(item).strip() for item in payload.get("tools", []) if str(item).strip()),
            permissions=tuple(str(item).strip() for item in payload.get("permissions", []) if str(item).strip()),
            default_enabled=bool(payload.get("default_enabled", True)),
            default_timeout_seconds=_clamp_timeout(payload.get("default_timeout_seconds", 3600)),
            health=payload.get("health") if isinstance(payload.get("health"), dict) else {},
            implementation=payload.get("implementation") if isinstance(payload.get("implementation"), dict) else {},
        )


class CapabilityService:
    def __init__(self, root: Path = CAPABILITIES_ROOT, settings_path: Path = SETTINGS_PATH) -> None:
        self.root = root
        self.settings_path = settings_path
        self._lock = threading.RLock()

    def list_modules(self, *, visibility: str | None = None) -> list[dict[str, Any]]:
        settings = self._load_settings()
        modules = []
        for module in self._load_modules():
            if visibility and module.visibility != visibility:
                continue
            modules.append(self._module_payload(module, settings.get(module.id, {})))
        return modules

    def get_module(self, module_id: str) -> dict[str, Any]:
        normalized = _normalize_id(module_id)
        settings = self._load_settings()
        for module in self._load_modules():
            if module.id == normalized:
                return self._module_payload(module, settings.get(module.id, {}), include_internal=True)
        raise KeyError(f"unknown capability module: {module_id}")

    def update_module(self, module_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_id(module_id)
        module = self._find_module(normalized)
        with self._lock:
            settings = self._load_settings()
            current = dict(settings.get(normalized, {}))
            if "enabled" in patch:
                current["enabled"] = bool(patch["enabled"])
            if "timeout_seconds" in patch:
                current["timeout_seconds"] = _clamp_timeout(patch["timeout_seconds"])
            if "permissions" in patch and isinstance(patch["permissions"], list):
                allowed = set(module.permissions)
                current["permissions"] = [str(item) for item in patch["permissions"] if str(item) in allowed]
            settings[normalized] = current
            self._save_settings(settings)
        return self._module_payload(module, current, include_internal=True)

    def enabled_external_tools(self) -> set[str]:
        tools: set[str] = set()
        for module in self.list_modules(visibility="external"):
            if module.get("enabled"):
                tools.update(str(item) for item in module.get("tools", []) if str(item))
        return tools

    def external_tools(self) -> set[str]:
        tools: set[str] = set()
        for module in self._load_modules():
            if module.visibility == "external":
                tools.update(module.tools)
        return tools

    def disabled_external_tools(self) -> set[str]:
        return self.external_tools() - self.enabled_external_tools()

    def enabled_external_skills(self) -> set[str]:
        skills: set[str] = set()
        for module in self.list_modules(visibility="external"):
            if module.get("enabled") and module.get("skill"):
                skills.add(str(module["skill"]))
        return skills

    def tools_for_modules(self, module_ids: list[str] | set[str] | None) -> set[str]:
        ids = {_normalize_id(item) for item in (module_ids or []) if str(item).strip()}
        if not ids:
            return set()
        tools: set[str] = set()
        for module in self._load_modules():
            if module.id in ids:
                tools.update(module.tools)
        return tools

    def timeout_for_module(self, module_id: str, default: int = 3600) -> int:
        normalized = _normalize_id(module_id)
        try:
            module = self._find_module(normalized)
        except KeyError:
            return _clamp_timeout(default)
        settings = self._load_settings().get(module.id, {})
        return _clamp_timeout(settings.get("timeout_seconds", module.default_timeout_seconds))

    def health(self, module_id: str) -> dict[str, Any]:
        module = self._find_module(_normalize_id(module_id))
        settings = self._load_settings().get(module.id, {})
        enabled = bool(settings.get("enabled", module.default_enabled))
        if not enabled:
            return self._health_result("disabled", "模块已禁用")
        config = module.health if isinstance(module.health, dict) else {}
        try:
            if config.get("type") == "checks":
                return self._run_checks_health(config)
            if config.get("type") == "command":
                return self._run_command_health(config)
            if config.get("type") == "http":
                return self._run_http_health(config)
        except subprocess.TimeoutExpired:
            return self._health_result("error", "健康检查超时")
        except Exception as exc:
            return self._health_result("error", f"健康检查失败：{exc}")
        return self._health_result("unknown", "未配置可执行的健康检查接口")

    def _find_module(self, module_id: str) -> CapabilityModule:
        for module in self._load_modules():
            if module.id == module_id:
                return module
        raise KeyError(f"unknown capability module: {module_id}")

    def _load_modules(self) -> list[CapabilityModule]:
        modules: list[CapabilityModule] = []
        if not self.root.exists():
            return modules
        for path in sorted(self.root.glob("*/capability.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                module = CapabilityModule.from_dict(payload)
            except Exception:
                continue
            if module.id and module.name and module.tools:
                modules.append(module)
        return modules

    def _module_payload(self, module: CapabilityModule, settings: dict[str, Any], *, include_internal: bool = False) -> dict[str, Any]:
        permissions = settings.get("permissions")
        if not isinstance(permissions, list):
            permissions = list(module.permissions)
        payload = {
            "id": module.id,
            "name": module.name,
            "display_name": module.display_name,
            "english_name": module.english_name,
            "aliases": list(module.aliases),
            "visibility": module.visibility,
            "category": module.category,
            "description": module.description,
            "best_for": list(module.best_for),
            "skill": module.skill,
            "tools": list(module.tools),
            "permissions": [item for item in permissions if item in module.permissions],
            "available_permissions": list(module.permissions),
            "enabled": bool(settings.get("enabled", module.default_enabled)),
            "timeout_seconds": _clamp_timeout(settings.get("timeout_seconds", module.default_timeout_seconds)),
            "default_timeout_seconds": module.default_timeout_seconds,
            "health": self._health_snapshot(module, settings),
        }
        if include_internal:
            payload["health_config"] = module.health
            payload["implementation"] = module.implementation
        return payload

    def _health_snapshot(self, module: CapabilityModule, settings: dict[str, Any]) -> dict[str, Any]:
        if not bool(settings.get("enabled", module.default_enabled)):
            return {"status": "disabled", "message": "模块已禁用"}
        return {"status": "unknown", "message": "尚未执行健康检查"}

    def _run_checks_health(self, config: dict[str, Any]) -> dict[str, Any]:
        checks_config = config.get("checks")
        if not isinstance(checks_config, list) or not checks_config:
            return self._health_result("unknown", "未配置检查项")
        checks = [self._run_one_check(item) for item in checks_config if isinstance(item, dict)]
        if not checks:
            return self._health_result("unknown", "未配置有效检查项")
        if any(item["status"] == "error" for item in checks):
            status = "error"
            message = "插件不可调用，存在失败检查项"
        elif any(item["status"] == "warning" for item in checks):
            status = "warning"
            message = "插件基本可调用，但存在风险项"
        else:
            status = "ok"
            message = "插件轻量健康检查通过"
        return self._health_result(status, message, checks=checks)

    def _run_one_check(self, config: dict[str, Any]) -> dict[str, Any]:
        check_type = str(config.get("type") or "").strip()
        check_id = str(config.get("id") or check_type or "check").strip()
        if check_type in {"path_exists", "file_exists", "directory_exists"}:
            return self._run_path_check(check_id, check_type, config)
        if check_type == "python_module":
            return self._run_python_module_check(check_id, config)
        return {"id": check_id, "status": "warning", "message": f"未知检查类型：{check_type or 'empty'}"}

    def _run_path_check(self, check_id: str, check_type: str, config: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(config.get("path") or "").strip()
        path = Path(self._expand_value(raw_path))
        if not path.exists():
            return {"id": check_id, "status": "error", "message": f"路径不存在：{path}"}
        if check_type == "file_exists" and not path.is_file():
            return {"id": check_id, "status": "error", "message": f"不是文件：{path}"}
        if check_type == "directory_exists" and not path.is_dir():
            return {"id": check_id, "status": "error", "message": f"不是目录：{path}"}
        return {"id": check_id, "status": "ok", "message": f"存在：{path}"}

    def _run_python_module_check(self, check_id: str, config: dict[str, Any]) -> dict[str, Any]:
        module_name = str(config.get("module") or "").strip()
        if not module_name:
            return {"id": check_id, "status": "error", "message": "未配置 Python 模块名"}
        cwd_value = self._expand_value(str(config.get("cwd") or PROJECT_ROOT))
        cwd = Path(cwd_value)
        if not cwd.exists():
            return {"id": check_id, "status": "error", "message": f"工作目录不存在：{cwd}"}
        timeout = _clamp_health_timeout(config.get("timeout_seconds", 5))
        env = dict(os.environ)
        pythonpath = [str(cwd), str(PROJECT_ROOT)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        proc = subprocess.run(
            [
                DEFAULT_PYTHON,
                "-c",
                "import importlib, sys; importlib.import_module(sys.argv[1])",
                module_name,
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            return {"id": check_id, "status": "ok", "message": f"模块可导入：{module_name}"}
        detail = (proc.stderr or proc.stdout or "").strip()
        return {"id": check_id, "status": "error", "message": f"模块导入失败：{module_name}；{detail[-400:]}"}

    def _run_command_health(self, config: dict[str, Any]) -> dict[str, Any]:
        command = config.get("command")
        if not isinstance(command, list) or not command:
            return self._health_result("error", "未配置健康检查命令")
        cmd = [self._expand_value(str(item)) for item in command]
        cwd = Path(self._expand_value(str(config.get("cwd") or PROJECT_ROOT)))
        timeout = _clamp_health_timeout(config.get("timeout_seconds", 8))
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            try:
                payload = json.loads(stdout)
                return self._normalize_health_payload(payload)
            except ValueError:
                pass
        if proc.returncode == 0:
            return self._health_result("ok", stdout or "健康检查命令执行成功")
        return self._health_result("error", stderr or stdout or f"健康检查命令失败：returncode={proc.returncode}")

    def _run_http_health(self, config: dict[str, Any]) -> dict[str, Any]:
        path = str(config.get("path") or "").strip()
        url = str(config.get("url") or "").strip()
        timeout = _clamp_health_timeout(config.get("timeout_seconds", 8))
        if path == "/api/tradinggraph/health":
            from backend.services.tradinggraph_service import tradinggraph_service

            return self._normalize_health_payload(tradinggraph_service.health())
        if not url and path:
            url = f"{FINCLAW_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        if not url:
            return self._health_result("error", "未配置健康检查 URL")
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return self._normalize_health_payload(response.json())

    def _normalize_health_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._health_result("warning", "健康检查返回了非 JSON 对象")
        status = str(payload.get("status") or "ok").lower()
        if status not in {"ok", "warning", "error", "disabled", "unknown"}:
            status = "ok" if status in {"healthy", "ready", "online"} else "unknown"
        message = str(payload.get("message") or payload.get("detail") or "健康检查完成")
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else None
        extra = {key: value for key, value in payload.items() if key not in {"status", "message", "detail", "checks", "checked_at"}}
        return self._health_result(status, message, checks=checks, **extra)

    def _health_result(self, status: str, message: str, *, checks: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "message": message,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        if checks is not None:
            payload["checks"] = checks
        payload.update(extra)
        return payload

    def _expand_value(self, value: str) -> str:
        replacements = {
            "project_root": PROJECT_ROOT,
            "finagent_root": FINAGENT_ROOT,
            "bettafish_root": BETTAFISH_ROOT,
            "tradingagents_root": TRADINGAGENTS_ROOT,
        }
        expanded = value
        for key, path in replacements.items():
            expanded = expanded.replace("${" + key + "}", str(path))
        return os.path.expandvars(expanded)

    def _load_settings(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    def _save_settings(self, settings: dict[str, dict[str, Any]]) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_id(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _clamp_timeout(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 3600
    return max(60, min(7200, parsed))


def _clamp_health_timeout(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 8
    return max(1, min(15, parsed))


capability_service = CapabilityService()
