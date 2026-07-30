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


## Quick start

Install the Python package in the repository's `agent` environment and install the
workspace's Node dependencies:

```bash
conda run --no-capture-output -n agent python -m pip install -e ".[dev]"
pnpm install
```

Build the WebUI, then start the shared FastAPI Control Plane. The production-style
server serves the built WebUI and API from the same address:

```bash
pnpm web:build
conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml serve
```

Open <http://127.0.0.1:8787/> or print/open it with:

```bash
conda run --no-capture-output -n agent riftx web
```

For frontend development, keep the Control Plane running and start Vite in a second
terminal. Vite proxies `/api` and `/healthz` to port `8787`:

```bash
pnpm web:dev
# Open http://127.0.0.1:5173/
```

The API and read-only UI can start while Temporal is unavailable. Creating and running
durable Agent workflows additionally requires a Temporal server at `127.0.0.1:7233`
and a RiftX worker:

```bash
conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml worker
```

### English and Chinese

The WebUI selects Chinese automatically when the browser locale starts with `zh`.
Use the `中文 / EN` button in the top bar to switch languages; the selection is stored
in browser local storage.

CLI output defaults to English for backward compatibility. Select Chinese or English
with the global option or environment variable:

```bash
conda run --no-capture-output -n agent riftx --language zh run list
RIFTX_LANGUAGE=zh conda run --no-capture-output -n agent riftx run list
conda run --no-capture-output -n agent riftx --language en run list
```

The language setting applies to Rich tables, empty states, statuses, errors, operation
feedback, and interactive-mode guidance. Command names and machine identifiers remain
unchanged.

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

## Managed browser runtime

Browser sessions are owned by the selected Run node and execute through its Runner.
RiftX supports ephemeral Chromium contexts, Runner-local persistent profiles, and
Chromium CDP attachment. Install the Chromium runtime once on every Runner that should
advertise browser capability:

```bash
conda run --no-capture-output -n agent playwright install chromium
```

The Control Plane exposes `/api/v1/browser/sessions` for open, observe, action,
takeover, release, close, and WebSocket observation streaming. Agent-facing results
contain bounded visible text, stable interactive-element references, form metadata,
network summaries, and Artifact IDs instead of the complete DOM. Runner-local profile
paths and CDP endpoints are not included in API or agent tool results. During user
takeover, Agent write actions are rejected while sanitized observations continue;
release produces a durable takeover summary.

## Browser and Burp connectors

Both external connectors use the same `/api/v1/connectors` protocol to import complete
HTTP request/response Artifacts, append to an existing Run or create a scoped Run,
follow Run events over SSE, cancel the Run, and open its WebUI. Connectors are capture
and control clients only; they do not contain an Agent runtime.

```bash
# Chrome/Chromium DevTools extension
pnpm --filter @riftx/browser-extension test
pnpm --filter @riftx/browser-extension build

# Dependency-free Burp connector core test
apps/burp-extension/scripts/test-core.sh
```

Load `apps/browser-extension/dist` as an unpacked extension after building. Build the
Burp Montoya JAR from `apps/burp-extension` with JDK 21+ and Gradle, then load it from
Burp's Extensions tab.

## Runtime metrics and release qualification

Every persisted Run exposes the eleven Post-V2 runtime metrics through the shared
Control Plane. Metrics are computed on demand from durable SQL state with a fixed
query budget; a metric with no observations is returned as explicitly unavailable
instead of being reported as a misleading zero.

```bash
riftx run metrics RUN_ID
curl http://127.0.0.1:8787/api/v1/runs/RUN_ID/metrics
```

The final release qualification maps all fifteen mandatory architecture and recovery
gates to executable pytest evidence. Run it from the repository root in the Agent
Conda environment; it exits non-zero if any gate fails and prints a machine-readable
JSON report.

```bash
conda run --no-capture-output -n agent python scripts/qa/release-gate.py
```
