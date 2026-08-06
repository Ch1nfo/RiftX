---
name: File Upload and Path Audit
description: Trace untrusted file content, metadata, names, archive entries, and paths into filesystem, storage, extraction, serving, and execution behavior
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

Use to review upload and download handlers, multipart processing, archive extraction, import and export, temporary files, attachment storage, media processing, static serving, local caching, path-based object lookup, file deletion, and any source-to-filesystem flow.

## Preconditions

Use the owner-bound source and entrypoint catalog. Identify storage roots, serving roots, temporary locations, archive libraries, object stores, filesystem privileges, and supported semantic-analysis quality. This Skill does not upload files, extract archives, or write to disk.

## Procedure

1. Enumerate file and path entrypoints and record attacker control over content, filename, extension, MIME, archive entry, storage key, identifier, path segment, destination, or operation.
2. Trace decoding, Unicode handling, separator normalization, basename processing, canonicalization, extension or type detection, generated naming, collision handling, and destination selection in actual order.
3. Resolve filesystem, archive, object-storage, parser, serving, deletion, overwrite, execution, and downstream processing sinks and their effective roots and privileges.
4. Verify containment against the final normalized path, including absolute paths, alternate separators, encoded traversal, archive entries, symlinks, hard links, race windows, and parent replacement where relevant.
5. Separate content validation, storage placement, later serving, later parsing, and execution claims. A safe name does not make unsafe content safe, and a suspicious extension does not prove execution.
6. Propose a finding only when a reachable input crosses an intended file or path boundary with security impact and the relevant defense is absent or bypassable.
7. Record generated-name, canonically contained, rejected, non-public, non-executable, test-only, unreachable, duplicate, and safely processed paths as negative results.

## Decision points

- Path control and string joins are candidates, not traversal proof.
- MIME and extension checks answer content-policy questions, not final-path containment by themselves.
- Canonicalization must occur before the side effect and be checked against the effective immutable root.
- Static source may not prove race or symlink safety; preserve uncertainty instead of inventing filesystem behavior.

## Stop conditions

Stop when in-scope file flows and sinks have input, transformation, effective root, defense, and impact evidence; unresolved race, link, parser, or serving behavior is explicit; and defended paths are durable.

## Expected output

An evidence-backed file-flow inventory, path-boundary map, candidate findings, verified defenses, negative results, and coverage gaps spanning ingestion through storage and later consumption.

## Error handling

Preserve unsupported archive formats, generated storage adapters, platform-specific path semantics, unavailable object-store policy, dynamic serving configuration, race uncertainty, and static fallback. Never claim a write, read, overwrite, delete, serve, or execute effect without a reachable sink.
