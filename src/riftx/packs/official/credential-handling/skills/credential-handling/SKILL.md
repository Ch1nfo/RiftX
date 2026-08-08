---
name: Credential Handling
description: Use authorized credential references through governed tools without exposing raw secret material
version: 1.0.0
source: official
required_capabilities:
  - credential_reference
  - registered_tool_execution
  - target_http
  - redaction
preferred_tools:
  - search_tools
  - get_tool
  - run_registered_tool
  - target_http_request
  - record_attempt
  - record_observation
  - record_negative_result
approval_level: always
---

## When to use

Use only when an authorized security task requires an authenticated identity, token, key, certificate, session, or other secret-backed capability.

## Preconditions

An opaque credential reference exists and is bound to the intended target, identity, tenant, purpose, Scope, approval, and validity window. A production tool can resolve it inside a protected execution boundary and redact resulting output.

## Procedure

1. State the minimum identity and authenticated action needed for the task without requesting secret content.
2. Verify current Scope, target identity, purpose, approval, credential-reference metadata, expiry, and allowed tool.
3. Select a governed tool whose contract accepts the credential reference. Never interpolate raw secret material into model-visible arguments or shell text.
4. Record the attempt using only the opaque reference ID, bounded identity metadata, target, purpose, and stop condition.
5. Execute the minimum authenticated action. Preserve redacted request, response, identity transition, artifact, and cleanup metadata.
6. Record the observed authenticated state or a negative result, then release, close, revoke, or expire temporary credential-backed resources as required.

## Decision points

- Refuse any request to print, decode, copy, summarize, persist, or return raw secret material.
- Require new approval when target, identity, tenant, purpose, arguments, effect, or validity changes.
- Prefer least-privilege and shortest-lived references.
- Keep separate identities and tenants distinct in attempts, evidence, browser state, and findings.

## Stop conditions

Stop on missing or stale binding, denied approval, target mismatch, unexpected privilege, redaction failure, secret exposure risk, invalid or revoked reference, completed purpose, or inability to confirm cleanup.

## Expected output

A redacted attempt and evidence trail containing opaque reference metadata, authenticated identity state, observed result, and cleanup or revocation status without recoverable credentials.

## Error handling

Fail closed if credential resolution, redaction, target binding, or cleanup services are unavailable. Quarantine any artifact suspected of containing secret material and record only bounded incident metadata.
