---
name: tradinggraph
description: Read or control 产业链透视 / ChainLens industry-chain bottleneck graphs without full graph dumps.
tools:
  - read_industry_graph
  - read_industry_graph_node
  - control_industry_graph
applies_to:
  - main_agent
  - deep_research
---

# Skill: 产业链透视 / ChainLens

## Capability
Inspect existing industry-chain bottleneck graphs, read node-level evidence, inspect neighbors, and control graph generation/resume/continuation.

## Correct Workflow
1. Use `read_industry_graph(action="list_mainlines")` to discover exact mainline names.
2. Use `get_graph_summary` for lightweight node directories.
3. Use `read_industry_graph_node(mode="overview")` for one node.
4. Use `mode="field"` only for a readable field shown by overview.
5. Use `control_industry_graph` for start/resume/continue/enrich actions requiring external work.

## Hard Constraints
- Full graph reads are disabled.
- Do not infer empty graph status from missing `run_id`; use `graph_status.node_count/edge_count`.
- For multiple nodes, read one node at a time and stop when enough evidence exists.
- Use `view_url` as the capability's original external frontend link; do not invent links.

## Failure Recovery
- If `node_ref` is used, pass the same `mainline`.
- If content is paged or truncated, continue with the same field and `next_offset`.
