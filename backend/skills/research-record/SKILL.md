---
name: research-record
description: Read persisted research records via summary, pending judgments, and paged sections.
tools:
  - read_research_record
applies_to:
  - main_agent
  - deep_research
---

# Skill: Research Record

## Capability
Read long-term research records created by Deep Research. Records are user-facing research assets, not raw run logs.

## Correct Workflow
1. If `record_id` is unknown, call `read_research_record(query=...)` to list matching records.
2. If a user-provided id or old report id fails, treat it as a query and use the returned candidate `record_id`; do not invent internal ids.
3. For a known record, call `read_research_record(record_id=...)` first. The default view returns the title, `研究摘要`, `待验证判断`, and section list.
4. Read正文 only when the default view indicates the old record is relevant.
5. Read only the needed section with `section`, `offset`, and `max_chars`.
6. Continue with `next_offset` only when the same section has `has_more=true`.

## Hard Constraints
- Do not request a whole record.
- Do not treat old research as verified fact by default; it is a clue until rechecked against current evidence.
- Do not repeat the same `record_id + section + offset` after `has_more=false`.
- Do not assume unread sections support a claim.

## Failure Recovery
- If a section is empty or fully read, choose another listed section or state the gap.
- If no record is found, continue with other authorized tools or ask whether to start a new research thread.
