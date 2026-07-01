---
name: memory
description: Read or update long-term memory using profile/playbook/convictions policies.
tools:
  - memory_read
  - memory_write
  - memory_update
  - memory_archive
applies_to:
  - main_agent
---

# Skill: Memory

## Capability
Read and update long-term memory files while respecting confidence, confirmation, lifecycle, and file-specific boundaries.

## Correct Workflow
1. Read memory only when the current task needs user profile, playbook, or convictions beyond injected context.
2. Profile updates represent user traits and can be maintained by the Agent.
3. Playbook updates revise the current research architecture: dimensions, questions, emphasis, ordering, or removals.
4. Convictions updates store only current active investment theses: market, industry, theme, or stock judgments that should influence future analysis.
5. Archive stale or contradicted content rather than deleting context silently.

## Hard Constraints
- Do not store low-confidence guesses as stable memory.
- Do not write Agent analysis into user convictions unless the user confirmed it as their view.
- Do not use archive content as active belief.
- Do not write concrete buy/sell, add/reduce position, clear position, stop-loss, or target-price instructions into convictions.
- Do not put raw report summaries or reusable methodology into convictions.
- A conviction candidate must include judgement, scope, evidence, invalidation condition, review trigger, and source.
- Do not append dated logs, single-stock cases, report summaries, or tool-failure lessons into playbook.
- A playbook candidate must be an architecture revision, not a memory note. It may add, delete, merge, rename, reorder, or rewrite research dimensions.

## Convictions Lifecycle
1. ADD: create a candidate only when there is no related active thesis.
2. UPDATE: revise an existing active/watching thesis when new evidence changes strength, scope, or wording.
3. ARCHIVE: move invalidated or obsolete active theses out of the active file.
4. If new evidence conflicts with an active thesis, surface a candidate/conflict for user review; do not silently merge.

## Playbook Boundary
Playbook is one living research architecture, not a repository of examples.

Use `memory_update(file="playbook")` when the user confirms that the research architecture should change. The replacement should be the revised architecture or a directly replaceable architecture fragment. If you only learned a lesson from a single research report, do not write it unless the user explicitly turns it into a general research dimension or question.

## Failure Recovery
- If conflict is detected, surface it as a pending memory issue.
- If unsure whether to remember something, do not write.
