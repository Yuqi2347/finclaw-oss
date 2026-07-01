---
name: report-reading
description: Locate, query, page, open, or delete local research reports safely.
tools:
  - list_report_catalog
  - query_report
  - get_report_detail
  - read_report_section
  - get_report_links
  - delete_report
applies_to:
  - main_agent
  - deep_research
---

# Skill: Report Reading

## Capability
Use the unified local report library to find reports, answer questions from report content, inspect report manifests, read specific sections, and obtain safe view/download links.

## Correct Workflow
1. Find candidate reports with `list_report_catalog`.
2. Select the exact `report_id` returned by the catalog.
3. For report Q&A, summaries, causes, risks, catalysts, company profile, or conclusions, call `query_report(report_id, question)`.
4. Use `get_report_detail` only when you need the manifest, section list, section character counts, or preferred view metadata.
5. Use `read_report_section` only after `get_report_detail` gives a `section_id`, or when `query_report` is insufficient.
6. Use `get_report_links` for opening, downloading, copying, or rendering report links.

## Hard Constraints
- `query_report.report_id`, `get_report_detail.report_id`, and `read_report_section.report_id` must be exact IDs returned by `list_report_catalog`.
- Never use a ticker, stock name, date, or guessed string as `report_id`.
- Never invent localhost URLs, ports, `/reports` paths, report IDs, or report content.
- Never request a full report body. Use `query_report` or paged `read_report_section`.

## Failure Recovery
- If a report call returns `invalid_report_id` or `report not found`, call `list_report_catalog` with the best subject/query and select a returned `report_id`.
- If `query_report` lacks enough material, call `get_report_detail`, choose relevant sections, then page with `read_report_section`.
- If a read result has `has_more=true`, continue the same section with `next_offset`; do not treat unread content as fact.

## Examples
Wrong:
`query_report(report_id="601899.SH", question="矿震影响")`

Correct:
1. `list_report_catalog(subject="紫金矿业", report_type="stock_research")`
2. `query_report(report_id="stock_research:601899.SH:2026-06-14", question="矿震影响")`
