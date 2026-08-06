---
name: Secret and Configuration Audit
description: Review embedded secrets, exposure paths, insecure defaults, and effective security configuration without exposing raw values
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

Use to review source, manifests, deployment definitions, CI/CD configuration, containers, examples, tests, fixtures, migrations, infrastructure, and runtime configuration for embedded credentials, private key material, secret exposure, insecure defaults, dangerous toggles, weak transport or cryptographic settings, debug behavior, and unsafe precedence.

## Preconditions

Use only the owner-bound source and repository map. Evidence storage must support source locations and digests without requiring raw secret values. This Skill does not validate credentials, contact external services, decrypt material, inspect unowned environment state, or execute configuration.

## Procedure

1. Inventory configuration surfaces and classify first-party production, deployment, example, test, generated, vendored, encrypted, and documentation contexts.
2. Search for credential and key formats, secret-bearing variables, unsafe security toggles, weak defaults, debug exposure, permissive trust settings, disabled verification, and fallback behavior.
3. Immediately redact candidate values. Record only source location, value class, bounded prefix class when non-sensitive, cryptographic fingerprint, length or shape, and consumer metadata.
4. Distinguish real secrets from placeholders, public identifiers, hashes, checksums, encrypted blobs, test fixtures, and generated examples using source context and effective consumers.
5. Trace each real candidate or setting through references, loaders, precedence, validation, override, and deployment paths to determine whether it is active, exposed, privileged, reusable, or safely replaced.
6. Propose a finding only when evidence establishes sensitive material or unsafe behavior, a reachable consumer or exposure path, and security impact. Never include the raw value in the claim or evidence.
7. Record rotated, placeholder, test-only, generated, unreachable, securely overridden, encrypted-at-rest, and false-positive patterns as negative results without retaining sensitive content.

## Decision points

- Entropy, prefixes, variable names, and scanner severity are candidates only; source context and effective use determine sensitivity.
- A default is not the effective value until configuration precedence and failure behavior are traced.
- Public keys, client identifiers, hashes, fingerprints, and encrypted blobs are not automatically secrets.
- If safe redaction cannot be guaranteed, stop processing the value and preserve only the location and reason code.

## Stop conditions

Stop when in-scope configuration surfaces are classified, candidates are safely redacted and resolved or explicitly unknown, effective security settings have consumer and precedence evidence, and all retained findings avoid raw secret material.

## Expected output

A redaction-safe candidate inventory, effective configuration map, proposed findings, verified defenses, negative results, and coverage gaps containing locations and fingerprints but no raw credentials, tokens, cookies, private keys, or recovery material.

## Error handling

If a read or tool result may expose sensitive material, minimize it immediately and do not copy it into reasoning, memory, findings, logs, or reports. Preserve unresolved deployment precedence, templating, encrypted values, external secret-manager references, unsupported formats, and missing consumers as explicit uncertainty.
