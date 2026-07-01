from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any


def sanitize_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json_value(item) for item in value]
    if hasattr(value, "item"):
        try:
            return sanitize_json_value(value.item())
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return None


def sanitize_json_dict(data: dict | None) -> dict | None:
    if data is None:
        return None
    return sanitize_json_value(data)
