---
name: Web Attack Surface
description: Map authorized web routes, inputs, identities, and trust boundaries from browser and HTTP evidence
version: 1.0.0
source: official
required_capabilities:
  - browser_observation
  - web_fetch
  - http_traffic
  - reasoning_graph
preferred_tools:
  - open_browser
  - observe_browser
  - web_fetch
  - query_http_traffic
  - read_http_exchange
  - record_observation
  - propose_hypothesis
approval_level: sensitive
---

## When to use

Use when an authorized web application must be mapped before vulnerability testing, especially when routes or inputs depend on navigation, authentication, client-side behavior, or redirects.

## Preconditions

Entrypoints and allowed origins are explicit in Scope. Browser and fetch operations use the existing approval path, credential use is separately authorized, and captured traffic and screenshots can be stored as bounded artifacts.

## Procedure

1. Establish the exact origin, environment, identity state, and excluded paths before navigation.
2. Observe the initial page and captured traffic before taking actions. Record redirects, cookies by metadata only, forms, links, scripts, API calls, and visible state transitions.
3. Traverse the minimum representative flows needed for the objective. Re-observe after every state-changing navigation or identity transition.
4. Correlate each route, method, parameter, content type, and authentication requirement with an HTTP exchange or browser observation.
5. Record confirmed surface facts as observations. Record candidate hidden routes, role differences, and trust-boundary concerns as hypotheses until directly observed.
6. Deduplicate equivalent endpoints while preserving material differences in method, identity, input location, content type, and state.

## Decision points

- Stop before crossing to an origin that is not explicitly in Scope.
- Never treat generated route guesses or documentation alone as observed target surface.
- Separate public, authenticated, privileged, tenant-specific, and state-dependent variants.
- Use active browser actions only when observation and fetch evidence cannot answer the mapping question.

## Stop conditions

Stop when representative flows and input classes are mapped, remaining branches require unavailable identities or approval, repeated navigation adds no new surface, or Scope and budget boundaries are reached.

## Expected output

An evidence-linked inventory of routes, methods, inputs, identity states, trust boundaries, redirects, and bounded hypotheses for later verification.

## Error handling

Preserve browser observation versions, failed navigation metadata, fetch errors, and unavailable traffic references. Do not replay stale element references, leak session material, or infer missing exchanges.
