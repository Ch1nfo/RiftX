# Dependency and Supply Chain Audit References

- Use `repository-mapping` to distinguish production, build, test, generated, vendored, and deployment roots.
- Record declared identity, resolved identity, source, version or revision, integrity, lock state, scope, consumer, build role, and provenance separately.
- Treat registries, Git repositories, URL archives, path dependencies, plugins, install scripts, and generated artifacts as distinct trust boundaries.
- External advisory and freshness data require an authorized evidence source; model memory is not one.
- Never run installers, package managers, build hooks, or project commands in this Pack.
