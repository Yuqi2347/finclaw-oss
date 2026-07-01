---
name: stock-data
description: Resolve A-share symbols and read DataHub stock snapshots or bounded data packages.
tools:
  - search_stock_symbol
  - get_stock_snapshot
  - get_stock_data_package
applies_to:
  - main_agent
  - deep_research
---

# Skill: Stock Data

## Capability
Use DataHub to resolve fuzzy stock names/codes and read structured A-share data with freshness and availability awareness.

## Correct Workflow
1. If the user provides a Chinese name, abbreviation, fuzzy code, or uncertain ticker, call `search_stock_symbol`.
2. If only current price/light quote is needed and ticker is known, call `get_stock_snapshot`.
3. For a broader stock view, call `get_stock_data_package(mode="overview")`.
4. For details, call `get_stock_data_package(mode="section", section=...)` and page with `offset/limit/max_chars` when needed.

## Hard Constraints
- Use standard A-share tickers such as `601899.SH`, `000001.SZ`, `833xxx.BJ`.
- Treat `data_freshness`, `time_context`, and `data_availability` as part of the answer.
- `empty` or `failed` fields are data gaps, not facts.
- Do not claim DataHub refreshed data unless the tool result explicitly says so.

## Failure Recovery
- If ticker is missing, call `search_stock_symbol`.
- If a section is unavailable, explain the gap and use other authorized sources rather than fabricating.
- If a section returns `has_more` or `next_offset`, continue the same section only.
