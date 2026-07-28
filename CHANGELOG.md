# Changelog

All notable changes to RiftX are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

No post-1.0 changes are currently recorded.

## [1.0.0]

### Added

- A macOS and Windows Desktop workbench for first-run setup, LLM Profile management, authorized engagement creation, conversation history, approvals, Auto controls, reports, and Artifact export.
- A formal Linux x64 CLI/daemon product path with stable JSON output, exit codes, engagement lifecycle commands, approvals, Auto controls, reports, Artifacts, Tools, Skills, credentials, and diagnostics.
- Responses API and Chat Completions LLM Profiles with transactional lazy Runtime activation, explicit lifecycle states, protected credential references, and three-layer connection capability tests.
- A Chat Completions bridge that preserves structured function calls and tool outputs while exposing the Runtime-compatible Responses contract.
- Runtime-level Responses and Chat Completions tool loops, including structured argument validation, interruption, provider errors, keep-alives, usage events, and strict streaming conversion.
- RedTeam, Pentest, and Lab-only Auto execution modes backed by one `ExecutionIntent` policy path, tool-risk discovery, approval binding, deadline enforcement, and process-tree termination.
- Auto multi-turn budgets, no-progress replanning, pause/resume/kill, daemon recovery, cross-asset objectives, evidence-backed success evaluation, and explicit stop reasons.
- Versioned local state, conversation pagination, target/evidence entities, encrypted audit records, reproducible Markdown/JSON reports, and integrity-checked Artifact capture/export.
- Tools Directory and Skills discovery with hashing, metadata validation, diagnostics, and approval invalidation when executable content changes.
- A RiftX 1.0 threat model, security policy, release payload scanner, dependency/license/RustSec controls, CycloneDX SBOM generation, and protected provider smoke workflows.
- Reproducible Linux `x86_64-unknown-linux-gnu` tarball packaging with SHA-256, license, attribution, install guide, example configuration, and build metadata.
- A protected, tag-bound release workflow that tests each platform, signs and notarizes macOS/Windows artifacts, aggregates checksums/SBOM, and can create draft releases only.
- Forward-only v0.8 configuration and SQLite migrations with validated adjacent pre-1.0 backups and fail-closed handling for backup, migration, or newer-schema errors.

### Changed

- Linux CLI is now a formally supported 1.0 entry point; Desktop remains supported on macOS 12+ Apple Silicon and Windows 10/11 x64.
- Desktop, Tauri, CLI, daemon, and release metadata now share the enforced `1.0.0` version.
- Rust 1.95.0, Node 22.20.0, pnpm 10.33.0, and the Tauri 2 dependency set are pinned for release builds.
- Repository configuration examples contain no personal Provider, API Key, internal endpoint, or machine-specific credential name.
- Scope language now describes operator-declared authorization and application-level prechecks rather than an OS-enforced network sandbox.

### Fixed

- LLM Profile creation no longer restarts the daemon before a Key exists, and a missing or invalid Profile no longer prevents unrelated Profiles from serving work.
- Profile configuration and credential updates now use candidate Runtime activation with rollback instead of stopping the working Runtime first.
- Shared credentials cannot be deleted while another Profile or engagement still owns a reference.
- Chat SSE parsing now preserves UTF-8 across network chunks, accepts CRLF framing, rejects incomplete EOF, distinguishes terminal reasons, and does not silently discard unsupported request fields.
- Function-tool capability probes require a structured call ID, function name, and valid JSON arguments rather than matching ordinary response text.
- Provider errors are bounded and redacted without slicing through UTF-8 boundaries.
- High-risk Tools Directory executables cannot bypass RedTeam approval through ordinary shell chains, relative paths, absolute paths, or `PATH` lookup.
- Desktop settings coordination no longer silently interrupts active work and reconciles daemon/runtime state after restart or connection loss.

### Security

- API Keys remain in the platform credential store or an explicitly selected headless environment/stdin source; they are not written to TOML, reports, audit plaintext, or child-tool environments.
- Approval decisions are bound to engagement, thread, turn, tool call, command digest, policy revision, executable inventory, authorization deadline, and working directory.
- Authorization expiry, audit failure, explicit Kill, and Auto budget exhaustion stop or pause further execution according to the documented fail-closed boundary.
- RiftX does not include product telemetry, automatic target-data upload, or an automatic updater in 1.0.

### Known limitations

- Local shell and tool execution occurs on the operator machine. RiftX application prechecks are not an unbypassable OS network sandbox; use an isolated, explicitly authorized Lab.
- Auto is restricted to Lab environments and requires explicit risk confirmation, authorization expiry, and bounded turn/tool/time budgets.
- Linux is distributed as a glibc 2.35+ tarball; RiftX 1.0 does not provide deb/rpm repositories or a Linux Desktop application.
- In-place downgrade from migrated 1.0 data to 0.8 is unsupported. Stop all RiftX processes and restore both validated pre-1.0 backups instead.
- Provider tool-calling behavior and rate limits vary. A Profile must pass the protected capability test and Runtime tool loop before release acceptance.
- Five exact, time-bounded RustSec exceptions remain documented in `security/rustsec-exceptions.toml` and expire on 2026-09-30.
