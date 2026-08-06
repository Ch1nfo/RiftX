---
name: Dependency and Supply Chain Audit
description: Reconcile dependency manifests, locks, sources, integrity, install behavior, build inputs, and reachable production consumers offline
version: 1.0.0
source: official
required_capabilities:
  - code_workspace
  - audit_snapshot
  - semantic_navigation
  - evidence_ledger
  - reasoning_graph
preferred_tools:
  - list_files
  - glob
  - grep
  - read_many_files
  - symbol_search
  - find_references
  - record_observation
  - propose_hypothesis
  - propose_finding
  - record_negative_result
approval_level: never
---

## When to use

Use to review dependency manifests and locks, workspaces, alternate registries, Git and path sources, vendored code, plugins, build tools, generated artifacts, install hooks, update configuration, CI inputs, provenance, and dependency use in production paths.

## Preconditions

Use only the owner-bound source and repository map. This Skill performs offline source analysis: it does not install packages, execute build scripts, contact registries, resolve current advisory data, or trust model memory as vulnerability intelligence.

## Procedure

1. Inventory ecosystems, manifests, locks, workspace roots, vendored trees, package-manager configuration, build definitions, update bots, and deployment packaging.
2. Reconcile declared and resolved dependency identity, version or revision, source, checksum or integrity, override, feature or scope, and lock coverage.
3. Identify floating, mutable, alternate, Git, URL, local path, workspace-patched, vendored, generated, plugin, and install-script inputs; record who or what controls each source.
4. Trace suspicious dependencies and build tools to imports, registration, generated output, packaging, startup, plugin loading, CI, and production deployment consumers.
5. Separate source-integrity, dependency-confusion, lock drift, build-execution, artifact-provenance, and known-vulnerability questions. Each needs its own evidence.
6. Propose a finding only when the repository proves an unsafe source, integrity, execution, or reachable dependency condition with impact. External advisory claims require an authorized evidence source outside this Pack.
7. Record correctly locked, verified, unused, development-only, test-only, unreachable, intentionally vendored, and unsupported advisory candidates as negative results or coverage gaps.

## Decision points

- Manifest presence does not prove production reachability.
- A lockfile does not protect inputs excluded from it, mutable Git refs, path sources, install scripts, plugins, or generated artifacts by itself.
- Package names and remembered versions are not authoritative advisory evidence.
- Vendoring changes the trust and update problem; it does not automatically make the code safe or vulnerable.

## Stop conditions

Stop when manifests, locks, source overrides, build and install inputs, and production consumers are mapped; accepted findings have repository evidence; and external freshness or advisory questions are explicit gaps.

## Expected output

An evidence-backed dependency inventory, lock and source analysis, build and install risks, reachable consumer map, proposed findings, negative results, and external verification gaps.

## Error handling

Preserve unsupported lock formats, generated manifests, missing submodules, ambiguous workspace resolution, unavailable advisory sources, and dynamic plugin discovery. Do not execute package tooling or fabricate resolved versions, checksums, ownership, registry state, CVEs, or exploitability.
