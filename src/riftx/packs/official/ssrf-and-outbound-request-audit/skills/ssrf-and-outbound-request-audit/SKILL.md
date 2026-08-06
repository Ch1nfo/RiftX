---
name: SSRF and Outbound Request Audit
description: Trace attacker influence over outbound protocols, destinations, redirects, proxies, credentials, network reachability, and response handling
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

Use to review URL previews, webhooks, callbacks, imports, image or document fetches, proxy endpoints, redirectors, service clients, metadata retrieval, remote validation, update checks, and other attacker-influenced outbound network operations.

## Preconditions

Use the owner-bound source and entrypoint catalog. Identify outbound client wrappers, URL parsers, redirect policy, proxy configuration, credential attachment, deployment network context, and available configuration evidence. This Skill does not send network requests.

## Procedure

1. Enumerate outbound client and protocol sinks, including wrappers, SDKs, redirect handling, proxying, DNS or resolver helpers, sockets, and non-HTTP schemes where visible.
2. Trace attacker influence separately for scheme, user info, authority, host, port, path, query, fragment, redirect target, proxy, resolved address, and request body or headers.
3. Record parsing and normalization order, alternate representations, decoding, default ports, hostname comparison, DNS or address classification, allowlists, denylists, and connection target selection.
4. Verify whether redirects are followed and revalidated, whether resolution can change after validation, whether proxies alter the destination, and whether credentials or sensitive headers cross trust boundaries.
5. Determine effective network reachability and impact from source evidence: internal services, loopback, link-local, metadata, control planes, authenticated upstreams, response disclosure, blind side effects, or resource exhaustion.
6. Propose a finding only when attacker-controlled destination influence reaches a network sink and the effective policy permits a security-relevant destination or effect. Keep incomplete deployment or resolver questions as hypotheses.
7. Record fixed-host, path-only, body-only, unreachable, rejected, fully revalidated, duplicate, and intentionally allowed destinations as negative results.

## Decision points

- Attacker influence over request content is not SSRF without destination or routing influence.
- String prefix, substring, or pre-parse hostname checks may not represent the final connected destination; verify parser and connection semantics.
- DNS rebinding, proxy behavior, redirect changes, and network reachability require evidence, not generic possibility.
- Separate SSRF, open redirect, credential leakage, response disclosure, and denial-of-service claims unless evidence establishes a chain.

## Stop conditions

Stop when in-scope outbound sinks have source, destination-component control, parser, resolution, redirect, proxy, credential, policy, network-context, and response evidence or explicit gaps; defended paths are durable.

## Expected output

An evidence-backed outbound sink inventory, destination-flow map, policy and credential boundaries, candidate findings, negative results, and deployment or resolver coverage gaps.

## Error handling

Preserve dynamic client configuration, unavailable deployment network policy, generated SDKs, custom resolvers, proxy inheritance, unsupported protocols, and static fallback. Never send a request or invent DNS answers, redirect targets, proxy routing, internal reachability, attached credentials, or response content.
