# Changelog

All notable user-facing changes to RiftX are recorded here. The project follows
[Semantic Versioning](https://semver.org/) while it remains in alpha.

## Unreleased

### Added

- Pentest-first authorized testing workflow with Scope, Approval, budget, Credential
  Reference, evidence, Finding, closure, and Markdown/HTML/JSON reporting.
- `riftx onboard`, `riftx doctor`, and one-command local startup through `riftx start`.
- Bundled production WebUI in the Python wheel.
- Durable Temporal workflows, local Runner effects, PTY takeover, Browser, MCP,
  connectors, research, Memory, Subagents, Hooks, and Operator Skills.
- Executable release gates, including the Docker-free core-path invariant.

### Changed

- Installation uses a standard Python 3.12 environment; Conda is not required for users.
- Browser, MCP, connectors, and Playwright are optional and do not block the core path.
- The supported trust profile is explicitly loopback-only `local_single_operator`.

### Removed

- The Code Audit product surface and Candidate/Promotion/Evaluation write paths. Only
  historical migration, read compatibility, Snapshot compatibility, and Safety Stop
  cleanup remain.

### Security

- Dangerous effects remain governed by authorization, scope, approval, attribution,
  credential separation, durable ownership, and affirmative stop evidence.
