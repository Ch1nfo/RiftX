---
name: Injection Audit
description: Trace attacker influence through transformations into query, command, template, expression, header, log, and interpreter sinks
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

Use after entrypoint discovery to review SQL and document queries, operating-system commands, templates, expressions, scripts, interpreters, response headers, logs, directory or naming services, and other grammars where attacker-controlled data may become instructions.

## Preconditions

Use the owner-bound source and an entrypoint catalog. Identify relevant languages, frameworks, persistence adapters, templating systems, process APIs, and supported semantic-analysis quality. This Skill does not execute payloads or the target project.

## Procedure

1. Enumerate sink families and concrete APIs from imports, wrappers, builders, adapters, and framework integrations rather than generic keywords alone.
2. Resolve sink callers and trace candidate sources from entrypoints, messages, files, environment, configuration, persistence, or upstream services.
3. Record each transformation, alias, parse, decode, validation, normalization, allowlist, encoding, parameterization, argument separation, or type conversion on the reachable path.
4. Determine sink semantics and context: code versus data position, grammar, interpreter, privilege, environment, side effects, and whether a framework or driver preserves structural separation.
5. Verify defenses against that exact context. Generic escaping, validation names, or upstream assumptions are insufficient without reachable implementation evidence.
6. Propose a finding only when attacker influence reaches an interpreter-relevant position and the effective defense is missing or bypassable with security impact. Keep incomplete paths as hypotheses.
7. Record constant-only, unreachable, test-only, structurally separated, correctly parameterized, correctly encoded, and duplicate paths as negative results.

## Decision points

- Dynamic string construction is not injection without attacker influence and interpreter-relevant placement.
- Attacker data reaching an API is not injection when the API preserves code-data separation for that argument.
- Sanitization must match the final grammar and context; validation before later decoding or concatenation may not remain effective.
- Static fallback or truncated call hierarchy lowers path confidence and requires direct source verification of critical edges.

## Stop conditions

Stop when in-scope sink families are inventoried, accepted candidates have complete source-to-sink and defense evidence, unresolved paths expose their missing edge, and defended or disproven paths are durable.

## Expected output

An evidence-backed source and sink inventory, candidate data paths, transformation and defense records, proposed findings, negative results, and coverage gaps with explicit analysis quality.

## Error handling

Preserve unresolved dynamic dispatch, generated queries, framework internals, unsupported languages, missing dependencies, decoding ambiguity, and truncated navigation. Never invent taint flow, runtime values, parser behavior, or exploitability.
