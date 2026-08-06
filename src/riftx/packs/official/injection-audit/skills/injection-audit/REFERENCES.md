# Injection Audit References

- Use `entrypoint-discovery` for attacker-controlled inputs and effective callers.
- Use `repository-mapping` for persistence, template, process, message, and integration subsystems.
- Model each candidate as source, transformations, sink, grammar/context, privilege, defense, impact, and analysis quality.
- Prefer structural separation such as parameterization, argument arrays, typed builders, and strict allowlists over generic escaping assumptions.
- Scanner matches and dangerous API names are search seeds, never confirmed findings by themselves.
