---
name: Variant Analysis
description: Derive root-cause invariants from verified findings, search structural and semantic neighbors, and verify every candidate independently
version: 1.0.0
source: official
required_capabilities:
  - code_workspace
  - audit_snapshot
  - semantic_navigation
  - evidence_ledger
  - reasoning_graph
  - finding_promotion_gate
preferred_tools:
  - glob
  - grep
  - read_many_files
  - symbol_search
  - find_references
  - call_hierarchy
  - query_reasoning_graph
  - record_observation
  - propose_hypothesis
  - propose_finding
  - record_negative_result
approval_level: never
---

## When to use

Use after a finding has sufficient verified evidence to extract a stable root cause, especially for copied handlers, sibling services, alternate frameworks, repeated wrappers, shared sinks, parallel code generation, incomplete fixes, or patch-bypass review.

## Preconditions

Use the owner-bound immutable source snapshot and authoritative seed Finding or evidence-complete candidate. Do not start from a vulnerability label, scanner rule, remembered pattern, or unverified claim alone.

## Procedure

1. Reconstruct the seed and extract its invariant: attacker capability, source, transformations, security boundary, sink or decision, missing or bypassed defense, preconditions, impact, and distinguishing context.
2. Separate invariant anchors from incidental syntax such as variable names, formatting, helper names, message text, or one library call.
3. Define a bounded search plan across sibling entrypoints, callers, references, wrappers, implementations, configuration, generated variants, alternate language or framework surfaces, and changed or unchanged branches.
4. Search broadly with lexical anchors, then narrow with symbols, references, callers, data or control-flow edges, types, configuration, and defense structure.
5. For every candidate, independently verify attacker control, reachability, effective defenses, impact, snapshot identity, source locations, and duplicate relationship. Never reuse the seed's evidence as candidate evidence.
6. Propose only independently evidence-complete candidates. Link duplicates and record patched, defended, unreachable, test-only, generated-only, lexical-only, and unsupported candidates as negative results.
7. Report searched surfaces, exclusions, truncation, unsupported languages, unresolved candidates, and remaining coverage gaps so absence of variants is not overstated.

## Decision points

- Same API, sink, helper, or text does not imply the same root cause.
- Same root cause does not imply the same impact, reachability, or defense state.
- A fix in one path is evidence to inspect siblings, not evidence that they are fixed or vulnerable.
- Each variant remains a candidate until it passes the same verification discipline as the seed.

## Stop conditions

Stop when the bounded search plan is exhausted or explicitly partial, every match has an independent disposition, duplicates are linked, and uncovered or unsupported surfaces are reported.

## Expected output

Root-cause models, searched-surface accounting, independently verified variant candidates, duplicate links, negative matches, unresolved candidates, and coverage gaps suitable for Finding Verification and Closure.

## Error handling

Preserve truncated search, unsupported languages, generated variants, ambiguous symbols, dynamic dispatch, stale snapshots, and incomplete seed evidence. Do not generalize beyond the proven invariant or clone claims and evidence across locations.
