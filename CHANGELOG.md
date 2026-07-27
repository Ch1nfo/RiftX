# Changelog

All notable changes to RiftX are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `1.0 计划.md` as the engineering execution contract toward RiftX `1.0.0`.
- `riftx.local.toml.example` for machine-local config overrides (gitignored `riftx.local.toml`).
- `VERSION` as the shared product version source (`0.8.0` during 1.0 development).
- `docs/1.0-baseline.md` toolchain and test baseline record.

### Changed

- Documented Linux CLI as a formal 1.0 platform entry (Desktop remains macOS / Windows).
- Restored repository `riftx.toml` to a safe Responses-compatible example without personal providers.
- Aligned Desktop package / Tauri / Cargo crate versions with `VERSION`.

### Fixed

- Desktop CI no longer invokes raw `cargo test`; uses repository nextest entry via `just`.

## [1.0.0] - TBD

Release notes will be filled when M8 exit criteria are met. Until then track work under Unreleased and the stage milestones in `1.0 计划.md` (`0.9.0-dev` … `1.0.0-rc.1`).
