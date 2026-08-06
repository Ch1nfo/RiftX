---
name: Entrypoint Discovery
description: Discover externally influenced and privileged code entrypoints with registration, reachability, input-boundary, and source evidence
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
  - record_negative_result
approval_level: never
---

## When to use

Use after repository mapping and before vulnerability-family review to enumerate externally influenced, event-driven, scheduled, administrative, and privilege-bearing code entrypoints.

## Preconditions

An owner-bound source and repository map are available. Known frameworks, deployable units, generated/vendor roots, and unsupported regions should already be identified where possible. This Skill performs source analysis only and does not start the application.

## Procedure

1. Enumerate likely registration surfaces from routing, RPC, CLI, job, queue, event, scheduler, file-upload, plugin, migration, webhook, serverless, and administrative configuration.
2. Search for framework annotations, registration calls, handler interfaces, command tables, message consumers, scheduled callbacks, and bootstrap wiring.
3. Resolve each candidate to a source definition and collect its registration site, references, callers, or framework configuration. A matching name alone is insufficient.
4. Record the input channel and attacker influence: path, query, body, header, cookie, message, file, environment, configuration, database row, local IPC, or privileged internal call.
5. Record identity and privilege context when visible, including anonymous, authenticated, tenant-bound, administrative, worker, system, or background execution.
6. Trace the first meaningful downstream subsystem or trust boundary using references and call hierarchy. Mark lexical or truncated analysis quality explicitly.
7. Link aliases, wrappers, generated adapters, and duplicate registrations to one logical entrypoint. Record unreachable, test-only, dead, or unsupported candidates as negative or unresolved results.

## Decision points

- If registration evidence exists but the handler definition is generated or unavailable, keep the entrypoint with a coverage gap rather than inventing implementation details.
- If a handler has no registration or production caller, classify it as unresolved or unreachable.
- If a caller chain uses `builtin_static`, verify security-critical edges from source before treating the path as unique.
- If the same implementation is exposed through multiple identities or protocols, preserve the distinct input and authorization boundaries while linking the shared code.

## Stop conditions

Stop when every mapped deployable unit and expected input channel has been searched, accepted entrypoints have source and reachability evidence, and unresolved, duplicate, generated, test-only, and unsupported candidates are recorded.

## Expected output

An evidence-backed entrypoint catalog containing logical identity, source location, registration evidence, input channel, identity and privilege context, downstream trust boundary, aliases, analysis quality, and unresolved candidates.

## Error handling

Preserve missing definitions, ambiguous symbols, parse errors, truncated scans, unsupported languages, and static fallback quality. Do not replace missing registration or caller evidence with framework conventions or model assumptions.
