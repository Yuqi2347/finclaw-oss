---
name: portfolio-ledger
description: Read portfolio facts and record user-confirmed transactions without inventing missing trade data.
tools:
  - get_positions
  - get_portfolio_summary
  - record_portfolio_transaction
  - upsert_position
  - remove_position
applies_to:
  - main_agent
---

# Skill: Portfolio Ledger

## Capability
Read local portfolio facts and record explicit user transactions or position edits through confirmation flows.

## Correct Workflow
1. Use read tools for current portfolio and position facts.
2. When the user says they bought, sold, added, reduced, cleared, or stopped out, call `record_portfolio_transaction`.
3. If required fields such as price or quantity are missing, still create a confirmable draft when the tool supports it.
4. Use direct position editing only when the user explicitly asks to correct local records.

## Hard Constraints
- Do not infer broker-account facts not stored locally.
- Do not record trades from analysis or suggestions.
- Do not fill missing price, quantity, or cost with zero.

## Failure Recovery
- If user omits sell price/quantity, ask or create a draft with missing fields, depending on tool result.
- If a transaction and current position conflict, surface the conflict instead of silently overwriting.
