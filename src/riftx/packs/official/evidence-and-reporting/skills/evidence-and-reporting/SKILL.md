---
name: Evidence and Reporting
description: Produce transparent findings and reports from durable evidence, negative results, and verified closure
version: 1.0.0
source: official
required_capabilities:
  - evidence_ledger
  - reasoning_graph
  - finding_service
  - closure_verifier
preferred_tools:
  - read_artifact
  - query_reasoning_graph
  - create_finding
  - add_artifact
  - complete_task
  - complete_run
approval_level: sensitive
---

## When to use

Use when reviewing a candidate finding, completing evidence-bearing tasks, assembling deliverable artifacts, or finalizing an authorized security Run.

## Preconditions

Task, Reasoning, Evidence, Finding, Artifact, and Success Criterion state is durable and current. Sensitive content has a redaction and audience policy, and target effects have already stopped or remain explicitly reported as unresolved.

## Procedure

1. Enumerate Success Criteria, tasks, hypotheses, attempts, evidence, findings, negative results, blockers, and stop state.
2. Read the referenced artifacts needed to verify each material claim; do not rely on transcript summaries when durable evidence exists.
3. Normalize duplicate candidates by root cause, affected component, prerequisites, impact, and evidence while preserving meaningful target differences.
4. Create or retain a confirmed finding only when its Evidence Contract is satisfied. Keep unsupported material as observations, hypotheses, negatives, or limitations.
5. Complete tasks only when their evidence and stop conditions are met; otherwise block, fail, or leave them pending with an explanation.
6. Produce bounded report artifacts containing scope, methods, evidence references, confirmed findings, negative coverage, limitations, cleanup, and closure outcome.
7. Request Run completion and report the Closure Verifier result exactly, including partial reason codes and unresolved criteria.

## Decision points

- Remove or qualify any claim without an accessible durable source.
- Separate observed behavior from inferred cause and business impact.
- Do not merge findings when different authorization, identity, target, evidence, or remediation paths matter.
- Prefer a transparent partial report over unsupported completeness.

## Stop conditions

Stop when all report claims are traceable, sensitive data is bounded, remaining tasks and criteria are explained, target effects have verified stop state, and Closure has produced complete or partial outcome.

## Expected output

Normalized findings, bounded report artifacts, explicit negative coverage and limitations, and a Closure result whose claims can be replayed from durable state.

## Error handling

Treat inaccessible, mismatched, oversized, corrupted, or improperly scoped artifacts as evidence failures. Preserve the failure and downgrade the affected claim rather than copying sensitive raw content into the report.
