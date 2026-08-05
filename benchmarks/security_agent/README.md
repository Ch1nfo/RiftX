# RiftX Security Capability Evaluation fixtures

This directory contains deterministic, local-only development scenarios for the
`riftx.evaluation.security_agent` harness.

Rules:

- Fixtures are untrusted test data and must never be imported or executed.
- `scenario.yaml` binds an immutable file digest, reset recipe, authorization scope,
  budget, memory policy and deterministic ground truth.
- Code Audit fixtures are read-only source inputs.
- Penetration Testing fixtures are static HTTP transcripts; the SEC-001 suite performs
  no network requests.
- Public scenarios support development and contract tests. Sealed regression scenarios
  may use the same schema but must not be committed to a public distribution.
- Evaluation comparisons are diagnostic and do not establish a product ranking.
