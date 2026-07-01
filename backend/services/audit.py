from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.config import DATA_DIR


AUDIT_LOG = DATA_DIR / "audit.log"


def log_event(event_type: str, payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
        "payload": payload,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
