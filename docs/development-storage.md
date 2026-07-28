# Local workspace storage

RiftX vendors a large Rust workspace. The source tree is comparatively small, but local validation creates two independent Cargo target directories:

- `codex-rs/target/` for `riftx`, `riftxd`, the embedded App Server, V8, AWS, TLS, SQLite, and test binaries;
- `apps/desktop/src-tauri/target/` for the separate Tauri dependency graph and Desktop tests/bundles.

A clean source checkout is roughly hundreds of MiB. A debug build of both graphs commonly adds **10–16 GiB** because Rust stores unstripped object files, debug information, proc-macro artifacts, and duplicate dependency builds. This is reproducible build output, not product data, and is already ignored by Git.

Use the supported cleanup command after a validation slice:

```bash
conda run -n agent just clean-riftx-generated
```

Preview first with:

```bash
conda run -n agent python3 scripts/riftx/clean-generated.py --dry-run
```

The cleanup removes only the two Cargo `target/` directories and generated `riftxd-*` Desktop sidecars. It deliberately keeps `node_modules/`, source files, lockfiles, Git history, local configuration, state databases, workspaces, reports, and artifacts.

For routine Rust testing keep `CARGO_INCREMENTAL=0`, use scoped `just test -p ...`, and avoid `--all-features` unless a specific compatibility question requires it. These practices reduce target growth but do not eliminate the cost of compiling the embedded runtime.
