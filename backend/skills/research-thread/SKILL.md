---
name: research-thread
description: Start, inspect, or control autonomous Deep Research threads with user-confirmed tool permissions.
tools:
  - start_research_thread
  - get_research_thread
  - control_research_thread
applies_to:
  - main_agent
---

# Skill: Research Thread

## Capability
Create or inspect long-running Deep Research Agent threads for systematic stock, mainline, market, comparison, or multi-source verification tasks.

## Correct Workflow
1. Use this only for systematic, broad, multi-tool research needs.
2. Before starting a new thread, check existing research records or active threads when relevant.
3. Call `start_research_thread` with a concise `research_goal`, subject, constraints, allowed tools, and blocked tools.
4. `research_goal` must preserve the user's explicit intent only: research object, questions, and constraints. Do not expand it into a research plan, checklist, inferred concerns, playbook-derived framework, or preset conclusion.
5. Let the confirmation card expose the goal and tool permissions for user editing.
6. Use `get_research_thread` for progress; use `control_research_thread` to pause, resume, or cancel.

## Hard Constraints
- Do not use research threads for simple quote, position, report-link, or single-fact questions.
- Do not silently start high-cost engines outside the confirmation flow.
- If the user blocks a tool, include it in `blocked_tools`.

## Failure Recovery
- If an existing active or recent thread is relevant, reuse or inspect it instead of starting another.
- If thread output is compact, use `read_research_record` after completion for report-level content.
