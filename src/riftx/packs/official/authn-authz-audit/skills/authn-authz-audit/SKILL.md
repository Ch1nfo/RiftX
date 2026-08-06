---
name: Authentication and Authorization Audit
description: Trace authentication, session, role, tenant, object, and privilege decisions from protected entrypoints to authoritative enforcement
version: 1.0.0
source: official
required_capabilities:
  - code_workspace
  - audit_snapshot
  - semantic_navigation
  - evidence_ledger
  - reasoning_graph
preferred_tools:
  - glob
  - grep
  - read_many_files
  - symbol_search
  - find_references
  - call_hierarchy
  - record_observation
  - propose_hypothesis
  - propose_finding
  - record_negative_result
approval_level: never
---

## When to use

Use after entrypoint discovery to review login, session restoration, token validation, logout, password or factor reset, role checks, tenant isolation, ownership checks, administrative actions, service identities, impersonation, and privilege transitions.

## Preconditions

Use only the owner-bound Workspace or Audit Snapshot. Start with an entrypoint catalog and known identity or privilege boundaries. This Skill reads source and records durable reasoning; it does not run the application, authenticate to a target, or assume framework defaults.

## Procedure

1. Partition entrypoints into public, authenticated, tenant-bound, privileged, administrative, service-to-service, background, and intentionally internal surfaces.
2. Trace how identity is established and restored: credential validation, token parsing, session lookup, revocation, expiry, audience or issuer checks, factor state, and account status.
3. For each protected operation, record subject, action, resource, tenant or ownership selector, privilege context, and the authoritative enforcement point.
4. Verify enforcement on the effective registration and caller path. Inspect ordering, alternate routes, wrappers, default policy, error handling, cache behavior, and fail-open branches.
5. Follow attacker-controlled identifiers and role or tenant claims to queries, policy inputs, and mutations. Distinguish object lookup from authorization of that object.
6. Propose a finding only when a reachable path permits an unauthorized security-relevant action and the relevant defense is absent, bypassed, or ineffective. Otherwise record the defense or missing evidence.
7. Preserve duplicate, unreachable, test-only, correctly denied, correctly tenant-bound, and intentionally public paths as negative results.

## Decision points

- A route annotation, middleware name, or policy helper is a candidate enforcement point, not proof that it runs or denies the tested subject-action-resource tuple.
- Attacker control of an identifier is not IDOR when the effective query or policy binds ownership or tenant correctly.
- Authentication weakness and authorization weakness are separate claims unless evidence establishes their chain.
- If identity comes from a trusted upstream, verify the local trust boundary and validation contract before treating headers or claims as authoritative.

## Stop conditions

Stop when every in-scope protected entrypoint has an identity source and authorization outcome, or an explicit coverage gap; every proposed bypass has source, reachability, subject-action-resource, impact, and defense evidence; and negative paths are durable.

## Expected output

An evidence-backed identity flow map, enforcement catalog, candidate findings, verified defenses, negative results, and unresolved coverage gaps without raw credentials or session material.

## Error handling

Preserve ambiguous middleware order, generated policy code, missing upstream contracts, unsupported languages, and static-analysis fallback. Do not infer allow or deny behavior from conventions, names, documentation, or model confidence.
