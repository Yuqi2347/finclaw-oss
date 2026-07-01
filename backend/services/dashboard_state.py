"""
Dashboard State Manager - 单一数据源管理器

提供线程安全的看板状态管理、事件订阅、后台定时刷新。
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


class DashboardStateManager:
    """看板状态管理器

    职责：
    1. 维护看板数据的单一真相源
    2. 提供线程安全的读写操作
    3. 支持事件订阅机制
    4. 后台定时刷新（可选）
    """

    def __init__(self):
        self._state: dict[str, Any] = {
            "updated_at": None,
            "market_overview": {},
            "portfolio_summary": {},
            "watchlist": [],
            "positions": [],
            "news": [],
            "news_meta": {},
            "errors": [],
        }
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[str, Any], None]] = []
        self._refresh_in_progress = False
        self._last_refresh_time: float | None = None

    def get_state(self) -> dict[str, Any]:
        """获取当前状态的深拷贝"""
        with self._lock:
            return copy.deepcopy(self._state)

    def get_field(self, key: str, default: Any = None) -> Any:
        """获取单个字段"""
        with self._lock:
            return copy.deepcopy(self._state.get(key, default))

    def update_state(self, updates: dict[str, Any], notify: bool = True):
        """批量更新状态

        Args:
            updates: 要更新的字段字典
            notify: 是否通知订阅者
        """
        with self._lock:
            self._state.update(updates)
            if "updated_at" not in updates:
                self._state["updated_at"] = datetime.utcnow().isoformat()

        if notify:
            for key, value in updates.items():
                self._notify_subscribers(key, value)

    def update_field(self, key: str, value: Any, notify: bool = True):
        """更新单个字段

        Args:
            key: 字段名
            value: 字段值
            notify: 是否通知订阅者
        """
        with self._lock:
            self._state[key] = value
            if key != "updated_at":
                self._state["updated_at"] = datetime.utcnow().isoformat()

        if notify:
            self._notify_subscribers(key, value)

    def append_error(self, error: str):
        """添加错误信息"""
        with self._lock:
            if "errors" not in self._state:
                self._state["errors"] = []
            self._state["errors"].append(error)

    def clear_errors(self):
        """清空错误信息"""
        with self._lock:
            self._state["errors"] = []

    def subscribe(self, callback: Callable[[str, Any], None]):
        """订阅状态变化

        Args:
            callback: 回调函数，接收 (key, value) 参数
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Any], None]):
        """取消订阅"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _notify_subscribers(self, key: str, value: Any):
        """通知所有订阅者"""
        for subscriber in self._subscribers:
            try:
                subscriber(key, value)
            except Exception as exc:
                logger.error(f"Subscriber notification failed: {exc}")

    def is_refresh_in_progress(self) -> bool:
        """检查是否正在刷新"""
        with self._lock:
            return self._refresh_in_progress

    def set_refresh_in_progress(self, in_progress: bool):
        """设置刷新状态"""
        with self._lock:
            self._refresh_in_progress = in_progress
            if in_progress:
                self._last_refresh_time = time.time()

    def get_last_refresh_time(self) -> float | None:
        """获取上次刷新时间（Unix 时间戳）"""
        with self._lock:
            return self._last_refresh_time

    def should_refresh(self, min_interval_seconds: int = 30) -> bool:
        """判断是否应该刷新

        Args:
            min_interval_seconds: 最小刷新间隔（秒）

        Returns:
            True 如果距离上次刷新超过最小间隔
        """
        with self._lock:
            if self._refresh_in_progress:
                return False
            if self._last_refresh_time is None:
                return True
            return (time.time() - self._last_refresh_time) >= min_interval_seconds


# 全局单例
dashboard_state = DashboardStateManager()
