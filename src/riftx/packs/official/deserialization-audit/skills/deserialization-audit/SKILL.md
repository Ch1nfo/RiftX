---
name: Deserialization Audit
description: Trace untrusted structured input through parsing, type resolution, object construction, lifecycle hooks, gadgets, and integrity controls
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

Use to review cookies and sessions, tokens, messages, queues, caches, RPC payloads, files, imports, framework binders, polymorphic JSON or XML, YAML or binary loaders, language-native object formats, and custom object reconstruction.

## Preconditions

Use the owner-bound source and entrypoint catalog. Identify parser libraries, modes, type registries, binders, schemas, integrity checks, trusted origins, callbacks, and relevant classpaths. This Skill does not deserialize payloads or execute gadget chains.

## Procedure

1. Enumerate structured-input entrypoints and identify who controls bytes, fields, type metadata, class names, tags, aliases, references, or signed state.
2. Resolve the effective parser mode and options, including safe versus full loaders, polymorphism, type metadata, custom resolvers, binders, schemas, and object factories.
3. Trace type selection and construction to concrete classes, constructors, setters, converters, lifecycle callbacks, post-load hooks, finalizers, proxies, and deferred evaluation.
4. Follow reachable hooks or gadget candidates to security-relevant side effects such as process execution, file access, network calls, reflection, code loading, expression evaluation, or privilege changes.
5. Verify trusted origin, signature or MAC, freshness, schema, type allowlist, classpath restriction, immutable data model, and post-parse validation on the effective path.
6. Propose a finding only when attacker-controlled input can select or influence reachable object behavior with a relevant side effect and the integrity or type defenses are absent or bypassable.
7. Record schema-only, inert primitive, signed-and-verified, allowlisted, unreachable, missing-class, test-only, duplicate, and no-side-effect candidates as negative results.

## Decision points

- Parsing structured data is not unsafe deserialization without dangerous object construction or downstream interpretation.
- A remembered gadget is irrelevant until the required class is present, selectable, constructed, invoked, and reaches a side effect.
- Integrity protects only when verified before dangerous reconstruction and bound to the exact payload and context.
- Static fallback may not resolve reflective construction; preserve the missing edge instead of assuming reachability.

## Stop conditions

Stop when in-scope parser paths have input trust, mode, type resolution, construction, hook or gadget, side effect, and defense evidence or explicit gaps; disproven chains are durable.

## Expected output

An evidence-backed parser inventory, type-resolution map, hook and gadget candidates, verified controls, proposed findings, negative results, and coverage gaps without executing untrusted data.

## Error handling

Preserve generated binders, reflective factories, missing dependency classes, native runtime behavior, unsupported formats, encrypted or signed payload uncertainty, and static fallback. Never fabricate parser options, classpath contents, callback invocation, gadget compatibility, or side effects.
