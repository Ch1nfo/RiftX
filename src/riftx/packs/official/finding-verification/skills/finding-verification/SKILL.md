---
name: Finding Verification
description: Independently reconstruct vulnerability candidates, challenge control and reachability, inspect defenses and counterevidence, and preserve verified or non-finding outcomes
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

Use after a scanner, model, operator, specialized audit Pack, or variant search produces vulnerability candidates, and before any conclusion is presented as confirmed.

## Preconditions

Use the immutable owner-bound source snapshot and candidate identities from the authoritative Reasoning Graph. This Skill can add evidence-backed observations, hypotheses, candidates, and negative results; it cannot directly set Confirmed status or bypass the internal promotion gate.

## Procedure

1. Load each candidate claim, identity, evidence references, source digest, path, assumed attacker capability, security boundary, impact, and unresolved questions.
2. Reconstruct the relevant source independently. Verify exact locations, symbols, callers, data or control flow, configuration, and analysis quality rather than trusting the original narrative.
3. Establish attacker control and preconditions, then prove every material reachability edge to the security-relevant sink, decision, or side effect.
4. Search for effective defenses and counterevidence: authorization, validation, canonicalization, encoding, parameterization, allowlists, type restrictions, signatures, fixed destinations, isolation, error handling, and unreachable branches.
5. Verify impact and reproduction contract at the strongest safe level available. Static evidence may verify some findings; unsupported runtime claims remain explicit gaps.
6. Check duplicate identity, stale snapshot, generated or vendored location, alternate paths, mitigating context, and whether the candidate is actually one issue or several.
7. Propose an evidence-complete candidate for the existing promotion pipeline, or record it as disproven, duplicate, stale, unsupported, or unresolved with durable reasons.

## Decision points

- Severity, confidence, consensus, scanner brand, exploit folklore, or a plausible narrative is not verification.
- A source and sink without a proven connecting edge remains unresolved.
- A verified defense must be on the effective path and match the relevant context; a defense elsewhere does not disprove the candidate.
- This Skill never writes Confirmed status. Confirmation remains the responsibility of the authoritative internal gate and evidence contract.

## Stop conditions

Stop when every candidate has a stable identity and one disposition: evidence-complete candidate for promotion, disproven, duplicate, stale, unsupported, or unresolved with explicit missing evidence.

## Expected output

Verified candidate packages, durable negative results, duplicate links, unresolved candidates, counterevidence, and evidence gaps suitable for the existing Finding promotion and Closure systems.

## Error handling

Preserve stale digests, missing files, ambiguous symbols, truncated paths, unsupported runtime behavior, inaccessible evidence, and conflicting observations. Never repair gaps with invented edges or status changes.
