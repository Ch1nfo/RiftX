# Deserialization Audit Changelog

## 1.0.0

- Add untrusted structured input, parser, type resolution, object hook, gadget, and integrity tracing.
- Require reachable construction and side effects before proposing unsafe deserialization.
- Preserve schema-only parsing and unreachable hooks as negative cases.
