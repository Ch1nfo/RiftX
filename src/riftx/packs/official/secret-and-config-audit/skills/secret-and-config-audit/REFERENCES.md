# Secret and Configuration Audit References

- Use `repository-mapping` to separate production, deployment, example, test, generated, vendored, and documentation contexts.
- Use `credential-handling` whenever a tool or workflow might access real secret material; this Pack itself does not request credential access.
- Evidence may contain source location, digest, value class, redacted shape, fingerprint, effective consumer, precedence, and exposure path, but never the raw value.
- Treat secret scanners as candidate generators and configuration names as search hints, not proof.
- Verify the effective setting through loader, validation, override, fallback, and consumer code.
