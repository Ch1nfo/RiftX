# SSRF and Outbound Request Audit References

- Use `entrypoint-discovery` for attacker-controlled webhook, callback, import, proxy, preview, and fetch inputs.
- Model scheme, authority, host, port, path, query, redirect, proxy, resolved address, headers, body, credentials, and response use separately.
- Verify parsing, normalization, destination policy, address classification, redirect revalidation, connection target, and network context in effective order.
- Generic DNS rebinding, parser differential, and internal-network claims are hypotheses until source or deployment evidence supports them.
- Do not send requests, resolve live names, contact targets, or access credentials in this Pack.
