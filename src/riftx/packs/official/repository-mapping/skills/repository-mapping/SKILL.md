---
name: Repository Mapping
description: Map repository roots, languages, frameworks, trust boundaries, generated code, and audit exclusions from owner-bound source evidence
version: 1.0.0
source: official
required_capabilities:
  - code_workspace
  - audit_snapshot
  - evidence_ledger
  - reasoning_graph
preferred_tools:
  - list_files
  - glob
  - read_many_files
  - grep
  - symbol_search
  - find_references
  - record_observation
  - record_negative_result
approval_level: never
---

## When to use

Use before vulnerability-focused review, after a source snapshot changes, or whenever repository size, multiple languages, generated code, vendored dependencies, or unclear service boundaries make audit coverage ambiguous.

## Preconditions

The authoritative Workspace or Audit Snapshot is available through native code tools. No project command, package manager, compiler, build script, plugin, or language-specific discovery task is required or permitted by this Skill.

## Procedure

1. List the top-level tree and bounded second-level structure. Record truncation rather than assuming unseen entries are absent.
2. Classify roots as first-party source, generated output, vendored dependency, tests, fixtures, documentation, configuration, migrations, infrastructure, or build artifacts.
3. Identify languages from file extensions and representative source. Confirm frameworks and runtimes from manifests, imports, configuration, routing symbols, or framework-specific entrypoints.
4. Map major subsystems and ownership boundaries: input adapters, authentication and authorization, business logic, persistence, background jobs, file processing, outbound integrations, administrative surfaces, and secret-bearing configuration.
5. Use symbols and references to verify that apparent modules participate in first-party flows. Do not infer runtime reachability from directory presence alone.
6. Record excluded, generated, unsupported, oversized, binary, or truncated regions with a reason and downstream risk.
7. Produce a concise map that later Skills can use to partition tasks without rescanning the whole repository.

## Decision points

- If a root is generated but locally modified, record both facts and keep it separate from ordinary first-party source.
- If framework evidence conflicts, preserve the competing observations and mark the framework unresolved.
- If a monorepo contains independent products, split the map by deployable or trust boundary rather than directory depth alone.
- If an unsupported language contains security-critical adapters, create a coverage gap instead of treating lexical navigation as full semantic review.

## Stop conditions

Stop when every visible top-level root is classified, major first-party subsystems and trust boundaries have evidence, and unknown or truncated regions are explicitly recorded. Do not continue into vulnerability claims during this mapping task.

## Expected output

An evidence-backed inventory of roots, languages, frameworks, deployable units, trust boundaries, security-relevant subsystems, generated/vendor regions, exclusions, unknowns, and recommended audit partitions.

## Error handling

If tree listing, globbing, decoding, parsing, or semantic navigation is truncated or unavailable, preserve the affected paths and quality metadata. Fall back to bounded source reads where useful, but never relabel partial lexical evidence as complete architecture knowledge.
