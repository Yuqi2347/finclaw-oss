---
name: web-research
description: Verify current or disputed facts with web sources and cite returned sources.
tools:
  - web_research
applies_to:
  - main_agent
  - deep_research
---

# Skill: Web Research

## Capability
Use web search to verify latest facts, disputed claims, news, social-media claims, report errors, or external source material.

## Correct Workflow
1. Convert the user need into focused searchable questions.
2. For multiple facts or comparisons, use one `web_research` call with `queries` for parallel search.
3. Set a reasonable `total_source_budget`; do not gather sources just to look comprehensive.
4. Answer only from returned sources and cite web-derived facts with returned markers such as `[1]`.

## Hard Constraints
- Never invent citations or cite sources not returned by the tool.
- Do not use web search for local-only facts such as holdings, pending actions, local report catalog, or cached task status.
- Do not make repeated serial searches for one problem when one batched query can cover it.

## Failure Recovery
- If sources are insufficient, say what remains unverified.
- If provider attempts fail, report the limitation and use local tools only for local facts.
