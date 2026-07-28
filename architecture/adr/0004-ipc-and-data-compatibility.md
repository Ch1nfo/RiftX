# ADR 0004: RiftX 1.0 IPC and Data Compatibility

- Status: Accepted
- Date: 2026-07-28
- Decision owners: RiftX maintainers
- Relates to: [ADR 0001](0001-v0.7-local-native-execution.md)

## Context

RiftX Desktop, CLI, and `riftxd` ship from one repository but may be upgraded or restarted
independently. Silent wire drift can execute an operation with the wrong authorization or lifecycle
semantics. At the same time, users upgrading from the v0.8 configuration must not lose existing LLM
Profiles merely because 1.0 adds explicit protocol and enablement fields.

## Decision

1. Local IPC has one integer `IPC_PROTOCOL_VERSION`. CLI and Desktop perform a daemon handshake and
   fail fast with a repair message when the exact version differs.
2. RiftX does not negotiate mixed-version local IPC or silently reinterpret unknown fields. Public
   DTOs use strict serde contracts where defined, and protocol changes must update fixtures and tests.
3. macOS, Windows, and Linux use the same business DTOs and endpoint semantics. Only transport and
   process-control implementations differ: UDS on Unix, Named Pipe on Windows, process groups on
   Unix, and Job Objects on Windows.
4. Persistent configuration compatibility is versioned separately from IPC. `llm.config_version`
   drives repeatable one-shot migrations; missing pre-1.0 `protocol` and `enabled` values receive the
   safe compatibility defaults `responses` and `true`.
5. A migrated configuration is validated, written to a temporary file, fsynced, and atomically
   renamed. Re-running migration is idempotent.
6. SQLite schema evolution remains owned by the state-store migration layer. Runtime homes and
   ephemeral process state are rebuildable and are not compatibility authorities.
7. CLI-owned JSON output has a versioned schema fixture. Domain output reuses the IPC DTO contract
   instead of creating parallel CLI shapes.
8. Release artifacts for all platforms must come from the same tag and commit. Platform-specific
   failures must remain explicit and must not be reported as supported behavior.

## Consequences

- Upgrading only Desktop/CLI or only `riftxd` can temporarily block operation, but cannot silently
  cross an incompatible protocol boundary.
- Wire changes require a deliberate protocol bump and synchronized client updates.
- Supported configuration migration is narrow, testable, and independent of transport versioning.
- Cross-platform CI must run the same gateway/CLI contract suite on macOS, Windows, Ubuntu 22.04,
  and Ubuntu 24.04 before M6 can exit.
