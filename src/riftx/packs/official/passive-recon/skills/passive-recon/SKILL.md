---
name: Passive Recon
description: Collect scope-bound public-source attack-surface leads with provenance and explicit uncertainty
version: 1.0.0
source: official
required_capabilities:
  - web_search
  - web_fetch
  - web_research
  - evidence_ledger
preferred_tools:
  - web_search
  - web_fetch
  - web_research
  - record_observation
  - record_negative_result
approval_level: sensitive
---

## When to use

Use before active enumeration when an authorized target has a public footprint that may reveal domains, services, technologies, repositories, documents, or historical endpoints.

## Preconditions

The Run has an explicit objective and Scope. Public sources are untrusted leads, not authorization and not direct proof of a current vulnerability. External search and fetch actions must pass the existing approval policy.

## Procedure

1. Normalize the authorized organizations, domains, addresses, products, and excluded assets without broadening them.
2. Search narrowly for identifiers, public endpoints, documentation, certificate or repository references, and historical metadata relevant to the objective.
3. Fetch only sources needed to validate a lead. Preserve URL, retrieval time, redirect metadata, bounded content artifacts, and source limitations.
4. Correlate leads by exact identifiers and time. Keep association confidence separate from target verification.
5. Record useful leads as observations with source artifacts. Record stale, inaccessible, contradictory, duplicate, or out-of-scope leads as negative results.
6. Hand active-validation candidates to a separately scoped task; do not probe them from this Skill.

## Decision points

- Reject related assets that are outside Scope even when ownership appears plausible.
- Prefer primary sources and current target-controlled material over snippets and third-party summaries.
- Require another source or later target evidence when identity, ownership, freshness, or environment is ambiguous.
- Stop searching a query family when it yields only duplicates or no new identifiers.

## Stop conditions

Stop when the defined passive questions are answered, search families are exhausted, the external-service budget is reached, or all remaining leads require active interaction or operator clarification.

## Expected output

A bounded source inventory, evidence-linked observations, rejected or stale leads, and explicit candidates for later authorized validation.

## Error handling

Preserve failed fetch metadata, redirects, parser limitations, and provider errors without copying secrets or excessive source content. Never replace missing provenance with model recollection.
