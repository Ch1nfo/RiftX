# RiftX Linux Security Acceptance

Linux Docker on the GitHub Actions Ubuntu runner is the security acceptance baseline. macOS Docker Desktop is a development environment only and does not establish production network-isolation guarantees.

## Required gates

- Sandbox image builds from the locked Codex tree and versioned security-tool sources.
- Runtime uses a non-root user, read-only root filesystem, dropped capabilities, `no-new-privileges`, PID/memory/CPU limits, and tmpfs workspaces.
- Containers start with `--network none`; managerd installs the default-deny nftables policy before connecting the management network.
- Only scope-approved target CIDRs and ports are reachable from the sandbox.
- Metadata, loopback, management networks, Docker API, other sandboxes, external DNS, raw sockets, and non-allowlisted egress remain unreachable.
- Bootstrap credentials are one-time, absent from URLs/logs/audit/SQLite, and revoked by interrupt, kill, delete, expiry, or restart reconciliation.
- Juice Shop and DVWA images are pinned by OCI digest and remain isolated on an internal target network.
- Markdown and JSON reports include structured state and the artifact SHA-256 manifest.

The `linux-security.yml` workflow is authoritative for these gates. A local macOS run may verify functionality but must not mark this acceptance complete.
