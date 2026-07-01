from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str), flush=True)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        import os

        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_config(default_config: dict) -> dict:
    config = default_config.copy()
    config["llm_provider"] = "minimax"
    config["deep_think_llm"] = "MiniMax-M2.7"
    config["quick_think_llm"] = "MiniMax-M2.7-highspeed"
    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    config["strict_data_vendor"] = True
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["output_language"] = "Chinese"
    return config


def main() -> int:
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--trade-date", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    load_env(root / ".env")

    from tradingagents.default_config import DEFAULT_CONFIG
    from web.progress import PIPELINE_STAGES, ProgressTracker
    from web.runner import run_analysis_in_thread

    tracker = ProgressTracker(ticker=args.ticker, trade_date=args.trade_date)
    thread = run_analysis_in_thread(
        ticker=args.ticker,
        trade_date=args.trade_date,
        config=build_config(DEFAULT_CONFIG),
        tracker=tracker,
    )
    stage_names = {item["id"]: item["name"] for item in PIPELINE_STAGES}
    completed_seen: set[str] = set()
    emit("started", ticker=args.ticker, trade_date=args.trade_date, current_stage="market")

    while thread.is_alive():
        current = tracker.current_stage or "running"
        for stage_id in list(tracker.completed_stages):
            if stage_id and stage_id not in completed_seen:
                completed_seen.add(stage_id)
                emit("stage_done", stage_id=stage_id, stage_name=stage_names.get(stage_id, stage_id))
        emit(
            "progress",
            current_stage=current,
            completed_stages=list(completed_seen),
            llm_calls=tracker.llm_calls,
            tool_calls=tracker.tool_calls,
            tokens_in=tracker.tokens_in,
            tokens_out=tracker.tokens_out,
        )
        time.sleep(1.5)

    thread.join(timeout=1)
    if tracker.error:
        details = getattr(tracker, "error_details", {}) or {}
        emit(
            "failed",
            error=tracker.error,
            error_type=details.get("type"),
            error_repr=details.get("repr"),
            traceback=details.get("traceback"),
            failed_stage=details.get("current_stage") or tracker.current_stage,
            completed_stages=details.get("completed_stages") or list(tracker.completed_stages),
        )
        return 1

    for stage_id in list(tracker.completed_stages):
        if stage_id and stage_id not in completed_seen:
            completed_seen.add(stage_id)
            emit("stage_done", stage_id=stage_id, stage_name=stage_names.get(stage_id, stage_id))

    emit(
        "completed",
        signal=tracker.signal,
        current_stage="completed",
        completed_stages=list(completed_seen),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
