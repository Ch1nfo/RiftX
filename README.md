# RiftX V2

RiftX V2 is a host-native durable agent execution platform. Its WebUI and CLI share a
single control plane, while long-running work is executed through resumable workflows
and node-local runners.

The implementation follows [`RIFTX_V2_DESIGN.md`](RIFTX_V2_DESIGN.md) and is developed
milestone by milestone:

1. Domain and persistence
2. Host runner
3. Tool and skill registries
4. Agent harness
5. Temporal integration
6. WebUI and CLI
7. Approval and PTY
8. Findings, artifacts, and reports
9. Remote runners and Windows support

## Development

Agent-related commands must run in the repository's `agent` Conda environment:

```bash
conda run --no-capture-output -n agent python -m pytest
```

The package uses a `src/` layout and requires Python 3.12.


## Remote Runners

Remote nodes use an outbound authenticated long-poll connection, so the Runner host
does not need an inbound listening port. Configure a high-entropy bootstrap token on
the Control Plane and start the daemon with the same token once:

```bash
export RIFTX_RUNNER_REGISTRATION_TOKEN="replace-with-a-random-secret"
riftx serve

# On the remote host:
export RIFTX_RUNNER_REGISTRATION_TOKEN="replace-with-a-random-secret"
riftx-runner serve \
  --server-url http://control-plane.example:8787 \
  --node-id kali-a \
  --name "Kali Runner A"
```

Registration returns a rotated node-scoped credential. The daemon stores it with
owner-only permissions and uses it for heartbeats, command polling, execution status,
and bounded output uploads. The bootstrap token can be removed from the Runner host
after its node credential has been persisted. Commands are durable, idempotent, and
leased, so a disconnected daemon can reconnect without starting the same execution
key twice.

## Windows shell execution

`ShellKind.POWERSHELL` resolves PowerShell 7 (`pwsh.exe`) first and falls back to
Windows PowerShell (`powershell.exe`). RiftX always launches it with an explicit argv
(`-NoLogo -NoProfile -Command`) rather than `shell=True`. Windows child processes are
created in a new process group; cancellation escalates from normal termination to a
`taskkill /T /F` process-tree cleanup after the grace period.

## Windows interactive terminals

Interactive terminal requests are routed by the Run's node. Local Unix nodes use a
native PTY; remote Windows nodes use the Windows ConPTY API through the conditional
`pywinpty` dependency. The remote Runner preserves the Control Plane's terminal and
execution IDs, forwards transcript bytes with exact offsets, and handles input, resize,
Ctrl+C, ownership, and close commands through the same durable outbound channel.

ConPTY is advertised as a Runner capability only when `pywinpty` is installed. Native
PTY and ConPTY handles cannot be reattached after the Runner process itself restarts, so
any previously open session is reported as `LOST`; its durable transcript remains
available from the Control Plane. Windows ConPTY behavior is covered with a fake native
backend on every platform, while the real PowerShell/ConPTY smoke path requires a
Windows host.
