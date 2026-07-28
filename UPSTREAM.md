# Codex Upstream Policy

RiftX vendors the source tree of `openai/codex` at the repository root so the Rust workspace remains directly buildable and RiftX crates can use the existing workspace dependencies. The exact source revision is recorded in `codex-upstream.lock`.

## Import Boundary

- Source repository: `https://github.com/openai/codex.git`
- Import mode: root-level vendored fork
- Excluded: upstream Git metadata and `.codex` repository automation/skill configuration
- Retained: upstream source, build files, dependency locks, `LICENSE`, `NOTICE`, `SECURITY.md`, and `AGENTS.md`

The `.codex` exclusion does not remove Codex runtime code. It avoids importing repository-local automation that is not required to build or run the product.

## RiftX Changes

RiftX-specific code should be isolated where practical:

- `codex-rs/riftx-core`
- `codex-rs/riftx-gateway`
- `codex-rs/riftx-ipc`
- `codex-rs/riftx-tools`
- `codex-rs/riftx-app-server-adapter`
- `codex-rs/riftx-cli`

Changes to upstream crates should be limited to integration points that cannot be implemented through composition. Each such component must be listed in `patched_components` and covered by focused tests.

## Updating Upstream

1. Fetch the desired official Codex revision in a separate checkout.
2. Review Agent Runtime protocol, permissions, local execution, and model-provider changes.
3. Run the typed adapter fixtures and RiftX cross-platform contract tests against the candidate revision.
4. Import the candidate source while preserving RiftX-owned files and directories.
5. Reapply the small, recorded RiftX patches.
6. Update `codex-upstream.lock` only after build and compatibility checks pass.

Do not build RiftX releases from a floating upstream branch. A failed compatibility check must stop the update; RiftX does not carry a container or remote-execution fallback.
