# Changelog

All notable changes to RiftX are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `1.0 计划.md` as the engineering execution contract toward RiftX `1.0.0`.
- `riftx.local.toml.example` for machine-local config overrides (gitignored `riftx.local.toml`).
- `VERSION` as the enforced shared `1.0.0` release version for Desktop, Tauri, `riftx`, and `riftxd`.
- `docs/1.0-baseline.md` toolchain and test baseline record.
- Explicit LLM Profile `protocol` (`responses` | `chat_completions`) with repeatable `config_version` migration.
- `codex-riftx-llm-bridge`: loopback Responses → Chat Completions adapter so Runtime stays on `/v1/responses` for Chat Completions profiles.
- LLM Profile connection test (`riftx llm profiles list|test`, Desktop Test connection) with a three-layer capability matrix.
- Transactional lazy LLM Profile runtime reload, shared credential ownership checks, and explicit Profile lifecycle states.
- Runtime-level Responses and Chat Completions tool-loop acceptance coverage.
- Auto multi-turn budgets, recovery, stop reasons, report/evidence closure, and cross-platform CLI JSON contracts.
- RiftX 1.0 threat model, RustSec exception governance, dependency license checks, release payload scanning, and CycloneDX SBOM generation.
- Reproducible Linux `x86_64-unknown-linux-gnu` tarball packaging with SHA-256, license, attribution, install guide, and build metadata.
- Protected, tag-bound macOS/Windows/Linux release workflow with signing/notarization gates and aggregate checksums.

### Changed

- Documented Linux CLI as a formal 1.0 platform entry (Desktop remains macOS / Windows).
- Restored repository `riftx.toml` to a safe Responses-compatible example without personal providers.
- Aligned Desktop package / Tauri / Cargo crate versions with `VERSION`.
- Pinned Rust 1.95.0, Node 22.20.0, pnpm 10.33.0, and the Tauri 2 dependency set for release reproducibility.

### Fixed

- Desktop CI no longer invokes raw `cargo test`; uses repository nextest entry via `just`.
- LLM Profile create no longer restarts the daemon before an API key is saved; missing keys no longer block other profiles or daemon startup.

## [1.0.0] - TBD

Release notes will be filled when M8 exit criteria are met. Until then track work under Unreleased and the stage milestones in `1.0 计划.md` (`0.9.0-dev` … `1.0.0-rc.1`).
