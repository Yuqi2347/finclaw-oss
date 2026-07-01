from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import DATA_DIR
from backend.tools.datahub import datahub_client


LEDGER_DIR = DATA_DIR / "portfolio_ledger"
LEDGER_FILE = LEDGER_DIR / "ledger.json"
MEMORY_DIR = DATA_DIR / "memory"
DECISIONS_DIR = MEMORY_DIR / "decisions"
DECISION_DRAFTS_DIR = DECISIONS_DIR / "drafts"
DECISION_ACTIVE_DIR = DECISIONS_DIR / "active"
DECISION_CLOSED_DIR = DECISIONS_DIR / "closed"
PATTERNS_DIR = MEMORY_DIR / "patterns"
PATTERN_CANDIDATES_FILE = PATTERNS_DIR / "candidates.md"
PATTERN_CONFIRMED_FILE = PATTERNS_DIR / "confirmed.md"
MEMORY_INDEX_FILE = MEMORY_DIR / "index.md"

OPENING_SIDE = "opening_position"
BUY_SIDES = {"buy", "add", "increase", "加仓", "买入"}
SELL_SIDES = {"sell", "reduce", "trim", "clear", "exit", "stop_loss", "清仓", "减仓", "止损", "卖出"}


class PortfolioLedgerService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def get_performance(self, recent_limit: int = 5) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(initializing=True)
            records = self._transactions(state)
            applied = [item for item in records if item.get("status") == "applied"]
            positions = self._build_positions(applied)
            market_positions = self._load_market_positions()
            realized_pnl = round(sum(_number(item.get("realized_pnl")) for item in applied), 4)
            unrealized_pnl = round(self._calculate_unrealized_pnl(positions, market_positions), 4)
            closed_trades = [
                item for item in applied
                if item.get("side") == "sell" and item.get("realized_pnl") is not None
            ]
            wins = sum(1 for item in closed_trades if _number(item.get("realized_pnl")) > 0)
            recent_trades = [
                self._public_transaction(item)
                for item in sorted(
                    [item for item in applied if item.get("side") != OPENING_SIDE],
                    key=lambda row: str(row.get("datetime") or row.get("created_at") or ""),
                    reverse=True,
                )[:max(1, recent_limit)]
            ]
            return {
                "total_pnl": round(realized_pnl + unrealized_pnl, 4),
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "win_rate": round(wins / len(closed_trades), 4) if closed_trades else None,
                "trade_count": len(closed_trades),
                "recent_trades": recent_trades,
                "updated_at": state.get("updated_at"),
                "basis_note": "PnL is calculated from FinClaw ledger opening positions and later applied transactions.",
            }

    def list_transactions(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(initializing=True)
            rows = sorted(
                self._transactions(state),
                key=lambda row: str(row.get("datetime") or row.get("created_at") or ""),
                reverse=True,
            )
            return {
                "transactions": [self._public_transaction(item) for item in rows[:max(1, limit)]],
                "updated_at": state.get("updated_at"),
            }

    def list_decisions(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            self._ensure_dirs()
            normalized = str(status or "").strip().lower()
            rows = self._read_decisions()
            if normalized:
                rows = [row for row in rows if str(row.get("status") or "").lower() == normalized]
            rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
            return {"decisions": rows[:max(1, limit)], "updated_at": _now()}

    def draft_transaction(self, payload: dict[str, Any], session_id: str = "api", run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(initializing=True)
            transaction = self._build_transaction(payload, state, status="pending", session_id=session_id, run_id=run_id)
            decision = self._create_decision(transaction, payload, status="draft")
            transaction["decision_id"] = decision["decision_id"]
            state["transactions"].append(transaction)
            self._save_state(state)
            self._render_index(state)
            return {
                "transaction": self._public_transaction(transaction),
                "decision": decision,
                "missing": self._missing_transaction_fields(transaction),
            }

    def confirm_transaction(self, transaction_id: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(initializing=True)
            transaction = self._find_transaction(state, transaction_id)
            if not transaction:
                raise KeyError(f"unknown transaction: {transaction_id}")
            if transaction.get("status") not in {"pending", "confirmed"}:
                return {
                    "transaction": self._public_transaction(transaction),
                    "status": transaction.get("status"),
                    "message": "transaction already processed",
                }
            self._merge_transaction_updates(transaction, updates or {})
            missing = self._missing_transaction_fields(transaction)
            if missing:
                transaction["status"] = "pending"
                transaction["missing_fields"] = missing
                transaction["updated_at"] = _now()
                self._save_state(state)
                return {
                    "transaction": self._public_transaction(transaction),
                    "status": "pending",
                    "missing": missing,
                    "message": "transaction is still missing required fields",
                }
            transaction["status"] = "confirmed"
            transaction["confirmed_at"] = _now()
            self._save_state(state)
            self._mark_decision_confirmed(transaction)
            applied = self._apply_confirmed_transaction(state, transaction)
            self._save_state(state)
            self._advance_decision_after_apply(transaction, applied)
            self._render_index(state)
            return {
                "transaction": self._public_transaction(transaction),
                "status": "applied",
                "position": applied.get("position"),
                "datahub_result": applied.get("datahub_result"),
            }

    def record_transaction(
        self,
        ticker: str,
        side: str,
        name: str | None = None,
        quantity: float | None = None,
        price: float | None = None,
        datetime: str | None = None,
        fee: float | None = None,
        tax: float | None = None,
        source: str | None = None,
        decision_context: str | None = None,
        rationale: str | None = None,
        position_thread_id: str | None = None,
        session_id: str = "default",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "ticker": ticker,
            "name": name,
            "side": side,
            "quantity": quantity,
            "price": price,
            "datetime": datetime,
            "fee": fee,
            "tax": tax,
            "source": source or "agent",
            "decision_context": decision_context,
            "rationale": rationale,
            "position_thread_id": position_thread_id,
        }
        draft = self.draft_transaction(payload, session_id=session_id, run_id=run_id)
        if draft.get("missing"):
            return {
                "status": "pending",
                "message": "transaction draft created; fill missing fields before it affects PnL or positions",
                **draft,
            }
        result = self.confirm_transaction(str(draft["transaction"]["transaction_id"]))
        return {"status": "applied", **result}

    def review_decision(self, decision_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_dirs()
            decision_path = self._find_decision_path(decision_id)
            if not decision_path:
                raise KeyError(f"unknown decision: {decision_id}")
            decision = _read_json(decision_path)
            if decision.get("status") != "closed":
                raise ValueError("only closed decisions can be reviewed")
            payload = payload or {}
            decision["status"] = "reviewed"
            decision["reviewed_at"] = _now()
            decision["review"] = {
                "outcome": payload.get("outcome"),
                "notes": payload.get("notes"),
                "lessons": payload.get("lessons"),
                "reviewed_by": "user",
            }
            decision["updated_at"] = _now()
            _write_json(decision_path, decision)
            self._render_index(self._load_state(initializing=True))
            return {"decision": decision}

    def _load_state(self, initializing: bool = False) -> dict[str, Any]:
        self._ensure_dirs()
        if LEDGER_FILE.exists():
            state = _read_json(LEDGER_FILE)
            if isinstance(state, dict):
                state.setdefault("transactions", [])
                state.setdefault("initialized_opening_positions", False)
                if initializing and not state.get("initialized_opening_positions"):
                    self._initialize_opening_positions(state)
                return state
        state = {
            "version": 1,
            "initialized_opening_positions": False,
            "created_at": _now(),
            "updated_at": _now(),
            "transactions": [],
        }
        if initializing:
            self._initialize_opening_positions(state)
        return state

    def _initialize_opening_positions(self, state: dict[str, Any]) -> None:
        try:
            positions = datahub_client.get_positions(timeout=5, use_cache=True)
        except Exception as exc:
            state["last_initialization_error"] = str(exc)
            state["updated_at"] = _now()
            return
        now = _now()
        for item in positions if isinstance(positions, list) else []:
            if not isinstance(item, dict):
                continue
            ticker = _normalize_ticker(item.get("ticker"))
            quantity = _optional_number(item.get("quantity"))
            if not ticker or quantity is None or quantity <= 0:
                continue
            price = _optional_number(item.get("cost_price")) or _optional_number(item.get("avg_cost")) or 0.0
            state["transactions"].append({
                "transaction_id": f"open_{ticker}_{uuid4().hex[:8]}",
                "datetime": str(item.get("updated_at") or now),
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "side": OPENING_SIDE,
                "quantity": quantity,
                "price": price,
                "amount": round(quantity * price, 4),
                "fee": 0.0,
                "tax": 0.0,
                "source": "opening_position",
                "decision_id": None,
                "position_thread_id": ticker,
                "status": "applied",
                "realized_pnl": 0.0,
                "created_at": now,
                "updated_at": now,
            })
        state["initialized_opening_positions"] = True
        state["updated_at"] = now
        self._save_state(state)
        self._ensure_pattern_files()
        self._render_index(state)

    def _build_transaction(
        self,
        payload: dict[str, Any],
        state: dict[str, Any],
        status: str,
        session_id: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        ticker = _normalize_ticker(payload.get("ticker"))
        if not ticker:
            raise ValueError("ticker is required")
        side = _canonical_side(payload.get("side"))
        if not side:
            raise ValueError("side is required")
        quantity = _optional_number(payload.get("quantity"))
        price = _optional_number(payload.get("price"))
        if side == "sell" and quantity is None and _is_full_exit_side(payload.get("side")):
            positions = self._build_positions([row for row in self._transactions(state) if row.get("status") == "applied"])
            current = positions.get(ticker)
            if current:
                quantity = _number(current.get("quantity"))
        transaction = {
            "transaction_id": str(payload.get("transaction_id") or f"txn_{uuid4().hex[:12]}"),
            "datetime": str(payload.get("datetime") or _now()),
            "ticker": ticker,
            "name": payload.get("name") or ticker,
            "side": side,
            "original_side": str(payload.get("side") or side),
            "quantity": quantity,
            "price": price,
            "amount": _optional_number(payload.get("amount")),
            "fee": _number(payload.get("fee")),
            "tax": _number(payload.get("tax")),
            "source": str(payload.get("source") or "api"),
            "decision_id": None,
            "position_thread_id": str(payload.get("position_thread_id") or ticker),
            "status": status,
            "session_id": session_id,
            "run_id": run_id,
            "created_at": _now(),
            "updated_at": _now(),
            "decision_context": payload.get("decision_context"),
            "rationale": payload.get("rationale"),
        }
        if transaction["amount"] is None and quantity is not None and price is not None:
            transaction["amount"] = round(quantity * price, 4)
        missing = self._missing_transaction_fields(transaction)
        if missing:
            transaction["missing_fields"] = missing
        return transaction

    def _merge_transaction_updates(self, transaction: dict[str, Any], updates: dict[str, Any]) -> None:
        for key in ("datetime", "ticker", "name", "source", "position_thread_id", "decision_context", "rationale"):
            if updates.get(key) not in (None, ""):
                transaction[key] = _normalize_ticker(updates[key]) if key == "ticker" else updates[key]
        for key in ("quantity", "price", "amount", "fee", "tax"):
            if key in updates and updates.get(key) not in (None, ""):
                transaction[key] = _optional_number(updates.get(key)) if key in {"quantity", "price", "amount"} else _number(updates.get(key))
        if updates.get("side"):
            transaction["side"] = _canonical_side(updates.get("side")) or transaction.get("side")
            transaction["original_side"] = str(updates.get("side"))
        if transaction.get("amount") is None and transaction.get("quantity") is not None and transaction.get("price") is not None:
            transaction["amount"] = round(_number(transaction.get("quantity")) * _number(transaction.get("price")), 4)
        transaction.pop("missing_fields", None)
        transaction["updated_at"] = _now()

    def _apply_confirmed_transaction(self, state: dict[str, Any], transaction: dict[str, Any]) -> dict[str, Any]:
        before = [row for row in self._transactions(state) if row.get("status") == "applied"]
        positions = self._build_positions(before)
        ticker = str(transaction["ticker"])
        side = str(transaction["side"])
        quantity = _number(transaction.get("quantity"))
        price = _number(transaction.get("price"))
        current = positions.get(ticker, {"ticker": ticker, "name": transaction.get("name") or ticker, "quantity": 0.0, "avg_cost": 0.0})

        if side == "buy":
            old_quantity = _number(current.get("quantity"))
            old_cost = _number(current.get("avg_cost"))
            new_quantity = old_quantity + quantity
            new_avg = ((old_quantity * old_cost) + (quantity * price) + _number(transaction.get("fee"))) / new_quantity
            current = {
                "ticker": ticker,
                "name": transaction.get("name") or current.get("name") or ticker,
                "quantity": round(new_quantity, 6),
                "avg_cost": round(new_avg, 6),
            }
            transaction["realized_pnl"] = 0.0
            datahub_result = datahub_client.upsert_position(
                ticker=ticker,
                name=str(current.get("name") or ticker),
                quantity=current["quantity"],
                avg_cost=current["avg_cost"],
            )
        elif side == "sell":
            old_quantity = _number(current.get("quantity"))
            if old_quantity <= 0:
                raise ValueError(f"no active ledger position for {ticker}")
            if quantity > old_quantity + 1e-9:
                raise ValueError(f"sell quantity {quantity} exceeds ledger position {old_quantity}")
            avg_cost = _number(current.get("avg_cost"))
            realized = (price - avg_cost) * quantity - _number(transaction.get("fee")) - _number(transaction.get("tax"))
            remaining = max(0.0, old_quantity - quantity)
            transaction["realized_pnl"] = round(realized, 4)
            if remaining <= 1e-9:
                current = {"ticker": ticker, "name": current.get("name") or ticker, "quantity": 0.0, "avg_cost": avg_cost}
                datahub_result = datahub_client.remove_position(ticker)
                transaction["closed_position"] = True
            else:
                current = {
                    "ticker": ticker,
                    "name": transaction.get("name") or current.get("name") or ticker,
                    "quantity": round(remaining, 6),
                    "avg_cost": avg_cost,
                }
                datahub_result = datahub_client.upsert_position(
                    ticker=ticker,
                    name=str(current.get("name") or ticker),
                    quantity=current["quantity"],
                    avg_cost=current["avg_cost"],
                )
                transaction["closed_position"] = False
        elif side == OPENING_SIDE:
            datahub_result = {"status": "ignored", "reason": "opening position already applied"}
        else:
            raise ValueError(f"unsupported side: {side}")

        transaction["status"] = "applied"
        transaction["applied_at"] = _now()
        transaction["updated_at"] = _now()
        return {"position": current, "datahub_result": datahub_result}

    def _advance_decision_after_apply(self, transaction: dict[str, Any], applied: dict[str, Any]) -> None:
        decision_id = transaction.get("decision_id")
        if not decision_id:
            return
        path = self._find_decision_path(str(decision_id))
        if not path:
            return
        decision = _read_json(path)
        closed = bool(transaction.get("closed_position"))
        decision["transaction_status"] = transaction.get("status")
        decision["updated_at"] = _now()
        decision["applied_at"] = transaction.get("applied_at")
        decision["realized_pnl"] = transaction.get("realized_pnl")
        if closed:
            decision["status"] = "closed"
            decision["closed_at"] = _now()
            decision["review_required"] = True
            target = DECISION_CLOSED_DIR / path.name
        else:
            decision["status"] = "tracking"
            target = DECISION_ACTIVE_DIR / path.name
        _write_json(target, decision)
        if path != target and path.exists():
            path.unlink()
        if closed:
            self._close_tracking_decisions(str(transaction.get("position_thread_id") or transaction.get("ticker")), exclude=decision_id)

    def _mark_decision_confirmed(self, transaction: dict[str, Any]) -> None:
        decision_id = transaction.get("decision_id")
        if not decision_id:
            return
        path = self._find_decision_path(str(decision_id))
        if not path:
            return
        decision = _read_json(path)
        decision["status"] = "confirmed"
        decision["transaction_status"] = transaction.get("status")
        decision["confirmed_at"] = transaction.get("confirmed_at") or _now()
        decision["updated_at"] = _now()
        _write_json(path, decision)

    def _close_tracking_decisions(self, position_thread_id: str, exclude: str | None = None) -> None:
        for path in DECISION_ACTIVE_DIR.glob("*.json"):
            decision = _read_json(path)
            if decision.get("decision_id") == exclude:
                continue
            if decision.get("position_thread_id") != position_thread_id:
                continue
            if decision.get("status") != "tracking":
                continue
            decision["status"] = "closed"
            decision["closed_at"] = _now()
            decision["review_required"] = True
            decision["close_reason"] = "position fully exited"
            decision["updated_at"] = _now()
            target = DECISION_CLOSED_DIR / path.name
            _write_json(target, decision)
            path.unlink()

    def _create_decision(self, transaction: dict[str, Any], payload: dict[str, Any], status: str) -> dict[str, Any]:
        self._ensure_dirs()
        decision = {
            "decision_id": f"dec_{uuid4().hex[:12]}",
            "status": status,
            "transaction_id": transaction.get("transaction_id"),
            "ticker": transaction.get("ticker"),
            "name": transaction.get("name"),
            "side": transaction.get("side"),
            "position_thread_id": transaction.get("position_thread_id"),
            "decision_context": payload.get("decision_context"),
            "rationale": payload.get("rationale"),
            "source": transaction.get("source"),
            "created_at": _now(),
            "updated_at": _now(),
            "review_required": False,
        }
        _write_json(DECISION_DRAFTS_DIR / f"{decision['decision_id']}.json", decision)
        return decision

    def _render_index(self, state: dict[str, Any]) -> None:
        try:
            from backend.services.long_term_memory import long_term_memory_service

            long_term_memory_service.rebuild_index()
        except Exception:
            MEMORY_INDEX_FILE.write_text(f"# FinClaw Memory Index\n\n- Updated at: {_now()}\n- Index rebuild failed.\n", encoding="utf-8")

    def _calculate_unrealized_pnl(self, positions: dict[str, dict[str, Any]], market_positions: dict[str, dict[str, Any]]) -> float:
        total = 0.0
        for ticker, position in positions.items():
            quantity = _number(position.get("quantity"))
            if quantity <= 0:
                continue
            avg_cost = _number(position.get("avg_cost"))
            market = market_positions.get(ticker, {})
            current_price = _optional_number(market.get("current_price"))
            if current_price is None:
                market_value = _optional_number(market.get("market_value"))
                if market_value is not None and quantity:
                    current_price = market_value / quantity
            if current_price is None:
                continue
            total += (current_price - avg_cost) * quantity
        return total

    def _build_positions(self, transactions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        positions: dict[str, dict[str, Any]] = {}
        for item in transactions:
            ticker = str(item.get("ticker") or "")
            if not ticker:
                continue
            side = str(item.get("side") or "")
            quantity = _number(item.get("quantity"))
            price = _number(item.get("price"))
            current = positions.get(ticker, {"ticker": ticker, "name": item.get("name") or ticker, "quantity": 0.0, "avg_cost": 0.0})
            if side == OPENING_SIDE:
                positions[ticker] = {
                    "ticker": ticker,
                    "name": item.get("name") or current.get("name") or ticker,
                    "quantity": quantity,
                    "avg_cost": price,
                }
            elif side == "buy":
                old_quantity = _number(current.get("quantity"))
                new_quantity = old_quantity + quantity
                if new_quantity <= 0:
                    continue
                old_cost = _number(current.get("avg_cost"))
                avg = ((old_quantity * old_cost) + (quantity * price) + _number(item.get("fee"))) / new_quantity
                positions[ticker] = {
                    "ticker": ticker,
                    "name": item.get("name") or current.get("name") or ticker,
                    "quantity": round(new_quantity, 6),
                    "avg_cost": round(avg, 6),
                }
            elif side == "sell":
                old_quantity = _number(current.get("quantity"))
                remaining = max(0.0, old_quantity - quantity)
                if remaining <= 1e-9:
                    positions.pop(ticker, None)
                else:
                    current["quantity"] = round(remaining, 6)
                    positions[ticker] = current
        return positions

    def _load_market_positions(self) -> dict[str, dict[str, Any]]:
        try:
            rows = datahub_client.get_positions(timeout=5, use_cache=True)
        except Exception:
            rows = []
        result: dict[str, dict[str, Any]] = {}
        for item in rows if isinstance(rows, list) else []:
            if isinstance(item, dict):
                ticker = _normalize_ticker(item.get("ticker"))
                if ticker:
                    result[ticker] = item
        return result

    def _missing_transaction_fields(self, transaction: dict[str, Any]) -> list[str]:
        if transaction.get("side") == OPENING_SIDE:
            return []
        missing = []
        if _optional_number(transaction.get("quantity")) is None or _number(transaction.get("quantity")) <= 0:
            missing.append("quantity")
        if _optional_number(transaction.get("price")) is None or _number(transaction.get("price")) <= 0:
            missing.append("price")
        if not str(transaction.get("datetime") or "").strip():
            missing.append("datetime")
        return missing

    def _transactions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        rows = state.setdefault("transactions", [])
        return rows if isinstance(rows, list) else []

    def _find_transaction(self, state: dict[str, Any], transaction_id: str) -> dict[str, Any] | None:
        for item in self._transactions(state):
            if item.get("transaction_id") == transaction_id:
                return item
        return None

    def _read_decisions(self) -> list[dict[str, Any]]:
        self._ensure_dirs()
        rows: list[dict[str, Any]] = []
        for folder in (DECISION_DRAFTS_DIR, DECISION_ACTIVE_DIR, DECISION_CLOSED_DIR):
            for path in folder.glob("*.json"):
                try:
                    item = _read_json(path)
                    item["_path"] = str(path)
                    rows.append(item)
                except Exception:
                    continue
        return rows

    def _find_decision_path(self, decision_id: str) -> Path | None:
        safe = _safe_id(decision_id)
        for folder in (DECISION_DRAFTS_DIR, DECISION_ACTIVE_DIR, DECISION_CLOSED_DIR):
            path = folder / f"{safe}.json"
            if path.exists():
                return path
        return None

    def _public_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "transaction_id", "datetime", "ticker", "name", "side", "quantity", "price", "amount",
            "fee", "tax", "source", "decision_id", "position_thread_id", "status", "realized_pnl",
            "missing_fields", "created_at", "updated_at", "confirmed_at", "applied_at",
        ]
        return {key: transaction.get(key) for key in keys if key in transaction}

    def _save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        _write_json(LEDGER_FILE, state)

    def _ensure_dirs(self) -> None:
        for path in (LEDGER_DIR, DECISION_DRAFTS_DIR, DECISION_ACTIVE_DIR, DECISION_CLOSED_DIR, PATTERNS_DIR):
            path.mkdir(parents=True, exist_ok=True)
        self._ensure_pattern_files()

    def _ensure_pattern_files(self) -> None:
        if not PATTERN_CANDIDATES_FILE.exists():
            PATTERN_CANDIDATES_FILE.write_text("# Pattern Candidates\n\nPatterns inferred from ledger and decisions stay here until user confirmation.\n", encoding="utf-8")
        if not PATTERN_CONFIRMED_FILE.exists():
            PATTERN_CONFIRMED_FILE.write_text("# Confirmed Patterns\n\nOnly user-confirmed investment behavior patterns belong here.\n", encoding="utf-8")


def _canonical_side(value: Any) -> str | None:
    raw = str(value or "").strip()
    lower = raw.lower()
    if lower in BUY_SIDES or raw in BUY_SIDES:
        return "buy"
    if lower in SELL_SIDES or raw in SELL_SIDES:
        return "sell"
    if lower in {OPENING_SIDE, "opening"}:
        return OPENING_SIDE
    return None


def _is_full_exit_side(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"clear", "exit", "stop_loss", "sell_all", "清仓", "止损"}


def _normalize_ticker(value: Any) -> str:
    ticker = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}", ticker):
        if ticker.startswith("6"):
            return f"{ticker}.SH"
        if ticker.startswith(("0", "3")):
            return f"{ticker}.SZ"
        if ticker.startswith(("4", "8")):
            return f"{ticker}.BJ"
    return ticker


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number


def _number(value: Any) -> float:
    number = _optional_number(value)
    return number if number is not None else 0.0


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


portfolio_ledger_service = PortfolioLedgerService()
