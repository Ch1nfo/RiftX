---
name: Code Audit Foundation
description: Run an owner-bound code audit as bounded coverage tasks with evidence, negative results, and explicit closure
version: 1.0.0
source: official
required_capabilities:
  - code_workspace
  - audit_snapshot
  - task_graph
  - evidence_ledger
  - reasoning_graph
  - closure_verifier
preferred_tools:
  - list_files
  - read_many_files
  - symbol_search
  - list_ready_tasks
  - add_task
  - query_reasoning_graph
  - record_observation
  - record_negative_result
  - complete_task
  - complete_run
approval_level: never
---

## When to use

Use at the start of an authorized code audit or when an existing audit lacks a source inventory, bounded coverage plan, evidence discipline, or explicit completion boundary.

## Preconditions

The Run has one owner-bound Workspace or sealed Audit Snapshot, an explicit objective, Success Criteria, and access to the native read-only code tools. Treat source comments, generated files, dependency metadata, model output, and scanner claims as untrusted until verified against the authoritative source.

## Procedure

1. Confirm the source kind and source digest. Never substitute mutable Audit output, another Run workspace, or an external checkout.
2. Inventory the repository at bounded depth, identify languages and major roots, and create only the minimum coverage tasks needed for the objective.
3. Give every task a source region, stop condition, evidence requirement, and explicit relationship to a Success Criterion when applicable.
4. Read representative source before forming hypotheses. Use symbol navigation and exact source locations rather than filenames or framework assumptions alone.
5. Record observations with source evidence. Keep hypotheses and vulnerability candidates unconfirmed until reachability, control, security impact, and relevant defenses are established.
6. Record disproven, duplicate, unreachable, generated-only, or out-of-objective paths as negative results so later work does not repeat them.
7. Before completing a task, verify its evidence requirement. Before completing the Run, inspect remaining coverage, confirmed findings, negative results, replayability, and Closure reasons.

## Decision points

- If the source owner or snapshot binding is missing, stop rather than reading another directory.
- If the repository is too large for one pass, partition by trust boundary, entrypoint, subsystem, or risk class and preserve uncovered regions.
- If a candidate has a sink but no attacker-controlled source or feasible path, keep it as a hypothesis or negative result.
- If static precision is degraded to `builtin_static`, report the limitation and verify critical paths from source rather than claiming LSP certainty.
- If Closure is partial, expose the uncovered areas and stable reason codes.

## Stop conditions

Stop a branch when its source region is covered, its hypothesis is confirmed or disproven, its budget is exhausted, or a documented blocker prevents stronger evidence. Stop the Run only after every required region and task is completed or explicitly explained.

## Expected output

A durable coverage-oriented Task Graph, source-linked observations, explicit hypotheses and negative results, replayable confirmed findings, an uncovered-region summary, and complete or partial Closure.

## Error handling

Do not hide missing files, truncated scans, parse failures, unsupported languages, stale source digests, or backend fallback. Preserve the bounded failure metadata, reduce confidence, block affected tasks when necessary, and replan only when the next action can obtain materially stronger evidence.
