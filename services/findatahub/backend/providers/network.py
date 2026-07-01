from __future__ import annotations

import os
from contextlib import contextmanager


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def clear_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


@contextmanager
def direct_network():
    """禁用代理的上下文管理器"""
    old_values = {key: os.environ.get(key) for key in (*PROXY_ENV_KEYS, "NO_PROXY", "no_proxy")}
    clear_proxy_env()
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
