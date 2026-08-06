---
name: Negative Results
description: Preserve disproven, exhausted, blocked, duplicate, and inconclusive paths as reusable bounded evidence
version: 1.0.0
source: official
required_capabilities:
  - reasoning_graph
  - attempt_history
  - task_graph
preferred_tools:
  - query_reasoning_graph
  - record_attempt
  - record_negative_result
  - update_task
  - complete_task
approval_level: never
---

## When to use

Use whenever an authorized security branch fails to support its hypothesis, becomes blocked, exhausts its budget, duplicates prior work, or cannot reach a reliable conclusion.

## Preconditions

The related task or hypothesis and its attempts are identifiable, observed results or failure metadata are available, and the conditions that bound reuse can be stated without inventing evidence.

## Procedure

1. Read the relevant reasoning nodes, task state, prior attempts, artifacts, and existing negative results.
2. Classify the outcome as disproven, inconclusive, blocked, exhausted, duplicate, unavailable, or out of scope.
3. Record the exact target identity, application or environment state, authorized identity, action signature, parameters, timing, tool version, and observed outcome needed to bound reuse.
4. Attach evidence or failure references and state what the result does and does not rule out.
5. Update the task with the remaining path, blocker, or satisfied negative requirement. Complete it only when its declared stop condition permits.
6. Before a retry, compare action signatures and require changed evidence, capability, parameters, state, or assumptions that can materially change the result.

## Decision points

- A timeout, tool error, parser error, or missing approval is not proof that the target behavior is absent.
- A negative under one identity, tenant, route, version, or state does not automatically transfer to another.
- Prefer reusing a bounded negative result over repeating an equivalent target effect.
- Reopen a branch when material target state or new evidence invalidates the original reuse boundary.

## Stop conditions

Stop when the negative outcome and reuse boundary are durable, the task has an explicit next state, and no non-duplicate retry with new evidentiary value remains within Scope and budget.

## Expected output

A durable negative result linked to its task or hypothesis, attempts and artifacts, with an action signature, confidence, limitations, and explicit reuse scope.

## Error handling

If evidence or attempt state is missing, record the result as inconclusive or blocked. Do not reconstruct precise parameters from memory or overwrite a prior negative with a broader claim.
