# Authentication and Authorization Audit References

- Use `entrypoint-discovery` for reachable input and privilege boundaries.
- Use `repository-mapping` for service, tenant, persistence, and trust-boundary context.
- Treat authentication, session management, authorization, ownership, tenant isolation, and privilege transition as separate evidence questions.
- Record subject, action, resource, context, decision, enforcement location, and effective caller path for each reviewed control.
- Never store raw passwords, tokens, cookies, secret answers, recovery codes, or private keys in model-visible evidence.
