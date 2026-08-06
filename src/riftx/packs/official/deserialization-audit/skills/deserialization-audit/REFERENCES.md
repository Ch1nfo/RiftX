# Deserialization Audit References

- Use `entrypoint-discovery` to locate untrusted structured-data inputs and identity or privilege context.
- Model parser mode, type metadata, resolver, constructed class, lifecycle hook, gadget edge, side effect, integrity, schema, and allowlist separately.
- A format or parser name is a search seed; unsafe object construction and reachable side effects require source evidence.
- Verify gadget presence and compatibility in the owner-bound snapshot rather than relying on remembered gadget lists.
- Do not parse payloads, instantiate objects, or execute the target project in this Pack.
