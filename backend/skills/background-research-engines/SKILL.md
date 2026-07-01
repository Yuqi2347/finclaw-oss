---
name: background-research-engines
description: Primary structured research capability source. Activate for stock deep dives, sector/mainline stage analysis, narrative heat, valuation/risk debate, or when web search alone would only produce shallow snippets.
tools:
  - run_stock_research
  - run_market_discovery
  - get_analysis_jobs
  - get_stock_research_status
  - recommend_stock_research_action
applies_to:
  - main_agent
  - deep_research
---

# Skill: Background Research Capabilities

## Role

This skill provides structured research material for deep investment reasoning.

Use it when the research needs more than fact lookup: company understanding, business quality, valuation, risk debate, market-stage judgment, mainline heat, or capital narrative.

## Tool Choice

- 个股深研 / EquityScope: use `run_stock_research` for single-stock professional research.
- 主线雷达 / ThemeRadar: use `run_market_discovery` for sector, theme, mainline, market-stage, and narrative-heat research.
- Existing or launched job status: `get_analysis_jobs`

If high-cost budget is limited, choose the engine that best matches the research object: stock -> `run_stock_research`; sector/mainline -> `run_market_discovery`.

## Research Use

Treat engine output as primary structured material, not as the final answer.

After the report is available, use report-reading tools to extract the relevant sections, then update the draft by explaining what changed in thesis, confidence, risk boundary, valuation, timing, or unresolved uncertainty.

Use web search for verification, freshness, and missing details; do not use it as the only material source when this skill is available and relevant.

## Hard Constraints
- High-cost runs require confirmation.
- Do not launch the same running task repeatedly.
- Do not claim a report exists until the job result or report catalog confirms it.

## Failure Recovery
- If a job is still running, poll status rather than moving on as if it completed.
- If a job fails or times out, record the failure as a gap and use other authorized tools.
