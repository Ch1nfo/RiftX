---
name: Web Request Analysis
description: Compare authorized HTTP requests one controlled variable at a time with durable exchange evidence
version: 1.0.0
source: official
required_capabilities:
  - target_http
  - http_traffic
  - reasoning_graph
  - evidence_ledger
preferred_tools:
  - target_http_request
  - query_http_traffic
  - read_http_exchange
  - record_attempt
  - record_observation
  - record_negative_result
approval_level: sensitive
---

## When to use

Use when a web hypothesis depends on how a target responds to a bounded change in method, path, parameter, header, body, content type, or authorized identity state.

## Preconditions

The target URL and resolved origin are in Scope, target HTTP interaction is approved, a current baseline exchange exists or can be created, and any identity or credential reference is authorized for this Run.

## Procedure

1. Read the full baseline exchange and record its target identity, request shape, application state, identity state, and artifact references.
2. State one falsifiable hypothesis and choose one material variable to change.
3. Record the planned attempt and check existing attempts for an equivalent action signature.
4. Send the bounded request through the governed target HTTP service; never bypass its Scope, redirect, credential, rate, or artifact controls.
5. Read the resulting exchange and compare status, redirects, headers, body structure, semantic content, timing, and resulting state while accounting for nondeterminism.
6. Record a supported observation, a remaining hypothesis, or a negative result. Add another attempt only when it changes the tested variable or resolves a known ambiguity.

## Decision points

- Refresh the baseline after session, identity, deployment, or material application-state changes.
- Do not combine multiple mutations when a single-variable comparison can answer the question.
- Treat timing and length differences as leads until repeated and explained.
- Stop retries with the same action signature unless new evidence or a concrete retry rationale exists.

## Stop conditions

Stop when the hypothesis is supported or disproven with replayable exchanges, the baseline is no longer valid, approval or Scope blocks the mutation, or remaining attempts exceed the task budget.

## Expected output

A sequence of durable attempts linked to baseline and changed HTTP exchanges, a bounded differential analysis, and an evidence-backed conclusion or negative result.

## Error handling

Preserve redirect rejection, timeout, transport, parser, artifact, stale-state, and approval errors. Never synthesize missing responses or silently compare incompatible identity states.
