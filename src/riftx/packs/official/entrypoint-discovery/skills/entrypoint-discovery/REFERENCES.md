# Entrypoint Discovery References

- A symbol name is not proof of production reachability.
- Prefer registration sites, framework configuration, references, and callers that bind an input channel to a concrete implementation.
- HTTP routes are only one entrypoint class; include RPC, CLI, queue, event, scheduler, file, plugin, migration, serverless, and administrative paths.
- Test-only and generated references must not silently become production reachability evidence.
- Static caller quality must remain visible, especially when `builtin_static` is lexical or indeterminate.
