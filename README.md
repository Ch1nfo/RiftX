# RiftX V2

RiftX V2 is a host-native durable agent execution platform. Its WebUI and CLI share a
single control plane, while long-running work is executed through resumable workflows
and node-local runners.

The tracked implementation coverage and release-qualification commands are documented in
[`docs/v2-completion-audit.md`](docs/v2-completion-audit.md). The implementation is organized
around these milestones:

1. Domain and persistence
2. Host runner
3. Tool and skill registries
4. Agent harness
5. Temporal integration
6. WebUI and CLI
7. Approval and PTY
8. Findings, artifacts, and reports
9. Remote runners and Windows support

The product and architecture decisions adopted from the CyberStrikeAI comparison are
mapped to RiftX code and tests in
[`docs/cyberstrikeai-adoption.md`](docs/cyberstrikeai-adoption.md).

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
cp -n configs/tools.example.yaml configs/tools.yaml
cp -n configs/models.example.yaml configs/models.yaml
pnpm web:build
export RIFTX_ADMIN_TOKEN="$(openssl rand -hex 32)"
conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml serve
```

`configs/tools.yaml` and `configs/models.yaml` are the local mutable registries used by
WebUI/CLI edits and are ignored by Git; their `*.example.yaml` counterparts remain the
sanitized, versioned templates. Configure the copied model profile before sending a
Run's first instruction.

The checked-in example explicitly selects the only implemented trust profile,
`local_single_operator`. `RIFTX_ADMIN_TOKEN` authenticates that stable server-owned
LocalPrincipal as well as administration requests. The WebUI asks for the token after
opening and keeps it only in page memory; CLI/API clients read it from the environment.
The token is never the Principal ID and is never written to the Principal state file.

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

The API, read-only UI, and conversation-only Run creation can all work while Temporal
is unavailable. Sending the first instruction and running a durable Agent workflow
additionally require a Temporal server at `127.0.0.1:7233` and a RiftX worker. Start
them in separate terminals before sending that instruction:

Install the Temporal CLI first. On macOS use `brew install temporal`; on other
platforms install the official CLI package documented at
<https://docs.temporal.io/cli> and verify `temporal --version`. If this executable or
the server is absent, the Control Plane can still create a conversation-only Run, but
the first instruction must fail explicitly with `temporal_unavailable`.

```bash
# Terminal 1: local durable workflow service (UI: http://127.0.0.1:8233)
mkdir -p .riftx
temporal server start-dev \
  --ip 127.0.0.1 \
  --port 7233 \
  --ui-port 8233 \
  --db-filename .riftx/temporal.db

# Terminal 2: RiftX workflow/activity worker
conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml worker
```

For a managed Temporal service, set its TLS endpoint and namespace in the shared
configuration used by both the Control Plane and Worker, enable `temporal.tls_enabled`,
and export `RIFTX_TEMPORAL_API_KEY` when the service uses API-key authentication. TLS
uses operating-system trust roots by default. Private PKI and mTLS deployments may set
`tls_server_root_ca_path`, `tls_server_name`, `tls_client_cert_path`, and
`tls_client_private_key_path`; the client certificate and private-key paths are required
as a pair. Configuration contains file paths only—never API keys, certificate PEM, or
private-key PEM. See [`docs/deployment.md`](docs/deployment.md) for a complete example.

The Control Plane health endpoint only proves that the API is online. Before an
acceptance run, also verify that Temporal is reachable and that a RiftX worker is
polling the configured `riftx-v2` task queue.

For supervised services, TLS reverse proxying, SSE/WebSocket settings, persistent
state, backup, upgrade, and stop-path acceptance checks, see
[`docs/deployment.md`](docs/deployment.md).

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

## Model profiles

RiftX supports OpenAI and OpenAI-compatible endpoints through two request modes:
`chat_completions` (the default) and `responses`. A profile contains the provider,
Base URL, model name, request mode, timeout, retry policy, and credential source.

The checked-in [`configs/models.example.yaml`](configs/models.example.yaml) is a
metadata-only template. Runtime metadata is written to `configs/models.yaml`; local
write-only API keys are stored separately in `.riftx/secrets/models.json` with owner-only
permissions. Both runtime files are ignored by Git. API responses, WebUI state, CLI
output, events, and logs never return the stored key.

You can configure profiles from **Models** in the WebUI or from the CLI:

```bash
# Hidden interactive input avoids shell history and process-list disclosure.
conda run --no-capture-output -n agent riftx model configure primary \
  --provider openai_compatible \
  --request-mode chat_completions \
  --base-url https://api.example.com/v1 \
  --model example-model \
  --api-key-prompt

conda run --no-capture-output -n agent riftx model list
conda run --no-capture-output -n agent riftx model default primary
```

Environment credentials remain supported and take precedence over the local secret
store:

```bash
export RIFTX_MODEL_BASE_URL="https://api.example.com/v1"
export RIFTX_MODEL_API_KEY="replace-with-provider-key"
```

`openai_compatible` profiles must always declare a non-empty Base URL. Model request
timeouts must be finite, greater than zero, and no more than 600 seconds; the same
contract is enforced by YAML, the management API, and CLI validation. When
`requires_api_key: false`, RiftX does not consult the profile's credential environment
variable or local Secret Store and supplies only a fixed non-secret SDK placeholder.

Metadata and local keys are read and changed under a shared OS file lock. Each stored
key is also bound to the digest of its exact Profile metadata, so a crash or an
uncooperative stale snapshot fails with a missing credential instead of pairing a new
key with an old endpoint. Changing a Profile's provider or Base URL never carries a
write-only stored key to the new destination; submit a key again for that endpoint. See
[`docs/model-profile-hardening.md`](docs/model-profile-hardening.md) for the invariants
and executable evidence.

Profiles changed through WebUI, CLI, or the management API may reference only
`RIFTX_MODEL_*` credential variables. This prevents a model administrator from selecting
an unrelated Worker secret and forwarding it to a configured endpoint. Managed Base
URLs cannot contain environment interpolation and reject literal link-local,
unspecified, or multicast destinations; loopback remains available for a local model.
Operator-owned `models.yaml` may use environment references such as
`${RIFTX_MODEL_BASE_URL}` because direct filesystem access is a higher trust boundary.

All local-operator routes and model/tool administration endpoints require the same
`RIFTX_ADMIN_TOKEN`, including localhost. Startup requires at least 32 printable,
non-whitespace ASCII characters. That length check catches missing and trivially weak values;
it does not prove randomness, so generate the token from a cryptographically secure
source and set it in the Control Plane and CLI environments before using the API:

```bash
export RIFTX_ADMIN_TOKEN="$(openssl rand -hex 32)"
```

If `RIFTX_RUNNER_REGISTRATION_TOKEN` is configured, startup also enforces at least 32
printable, non-whitespace ASCII characters, and it must be generated independently.
Control Plane startup rejects weak values and reuse of the operator token as the Runner
bootstrap token. Deliver it only through an owner-scoped environment or equivalent
process-scoped secret injection; never place it in process command-line arguments.

The current release implements only `local_single_operator`. It requires a loopback
listener and loopback browser origins, rejects proxy/remote identity configuration, and
keeps remote capabilities disabled. `remote_multiuser` is recognized but unavailable;
non-loopback binds and `RIFTX_TRUST_PROXY_AUTH=true` fail at startup. This local profile
is not tenant-safe and must not be exposed through a LAN/public reverse proxy. See
`docs/deployment.md` for the exact trust boundary.

Secure LocalPrincipal publication currently requires POSIX `dir_fd`, `O_NOFOLLOW`,
owner/mode checks, hard-link no-replace publication, and directory `fsync`. A Control
Plane on Windows or another platform without those primitives fails before writing
state with `local_principal_platform_unsupported`; this does not describe Runner host
compatibility.

Every `/api/*` route is included in a fail-closed policy inventory. OpenAPI operations
publish `x-riftx-authorization` and `x-riftx-effect`; application construction fails if
a new route is not classified. Model/tool administration requires the admin token,
Runner callbacks require Runner credentials, and local operator routes require the
same token-backed server Principal described above. Agent-visible tools use a separate
effect/authorization inventory, so an unclassified new tool is never exposed to a model.

The model profile summary used by the New Run form contains only the profile name,
model, request mode, credential-ready flag, and default flags. Base URLs, credential
environment names, stored-key state, and mutations require the admin token. The WebUI
keeps the local operator token only in page memory and never persists it in browser
storage. Arbitrary browser-extension origins are not granted CORS access.

## Conversation-first Runs

Creating a Run stores its objective, success criteria, entry points, scope, exclusions,
approval mode, node, workspace, and model profile, then opens the Run conversation in
`waiting_user`. It does **not** prepare tools, call the model, or start an execution
until the user sends the first concrete instruction.

```bash
conda run --no-capture-output -n agent riftx run create \
  "Validate the authorized staging service" \
  --model primary \
  --entry url=https://staging.example.test

conda run --no-capture-output -n agent riftx run message RUN_ID \
  "Begin with passive service identification and report before active probing."
```

The WebUI follows the same flow: **New run** creates the durable context and navigates
to Conversation, while Timeline remains available as the audit/debug projection.

## Emergency stop semantics

Pause and full cancel do not depend on Temporal being reachable. The Control Plane
first fences the Run in `pausing`/`cancelling`, then directly stops and confirms every
known Execution, managed Browser session, and Target HTTP request. Temporal is notified
after those local dispositions are collected and only on a best-effort basis.

The Run reaches `paused`/`cancelled` only when every known effect is confirmed stopped.
Otherwise the API returns `execution_cancel_failed` or `safety_stop_failed`, leaves the
Run fenced, and WebUI/CLI list each unconfirmed resource ID, node, observed state, and
reason. A remote terminal close is likewise only a durable request until its Runner
reports the PTY/ConPTY execution cancelled; enqueueing the request does not mark the
Terminal `closed` or the Execution `cancelled`. The Runner persists a cancellation
tombstone before termination whenever the effect has not yet produced an owner-fenced
durable row. For an already admitted Process/PTY, that immutable owner-fenced row plus
durable physical-stop proof is itself the no-restart barrier; a degraded journal write
is reported explicitly but cannot make the same execution key spawn again. Missing-row
cancel-before-start cannot succeed without its tombstone.
Execution `failed` and `lost` states are not physical-stop evidence: both are retried
through the owning Runner and must converge to an acknowledged `cancelled` state.
Remote Terminal, Browser, or Target HTTP work without a Runner acknowledgement is never
reported as stopped; use host-level containment while resolving an unconfirmed remote
effect. See [`docs/deployment.md`](docs/deployment.md) for the acceptance procedure and
failure-containment procedure.

Host-native Process, Shell, and interactive-terminal execution requires kernel-backed
process containment by default (`execution.require_containment: true`). On Linux, set
`RIFTX_CGROUP_V2_ROOT` to a trusted delegated cgroup v2 directory owned by the Runner;
set `RIFTX_PAYLOAD_UID` and `RIFTX_PAYLOAD_GID` to a dedicated, unprivileged account
that is different from the Runner account;
every Execution receives a separate leaf and stop succeeds only after `cgroup.kill`
completes and `cgroup.events` reports `populated 0`. A process cannot escape that leaf
with `setsid()`, a double fork, or leader exit, and the launcher refuses activation if
the dropped payload identity can write the delegated root or an ancestor
`cgroup.procs`. The persisted identity is bound to the root/leaf kernel inodes and
mount/cgroup namespaces, so a restarted Runner cannot mistake the same pathname in a
different container or namespace for the original boundary.

RiftX currently has no equivalent proof boundary for native macOS execution or Windows
Process/ConPTY execution. With the safe default those starts are rejected; route
authorized high-risk work to an isolated Linux Runner with delegated cgroup v2. Setting
`require_containment: false` is a local-development escape hatch only. It does not turn
PID/process-group disappearance or `taskkill` success into complete-descendant stop
evidence, and RiftX must leave an unprovable cancellation fenced and failed.

## Runner credentials and current deployment limit

The node-scoped outbound long-poll protocol is implemented, but a remote Runner needs a
remotely reachable Control Plane. The only currently selectable trust profile requires
the Control Plane to remain on loopback, so a remote Runner deployment is not available
in this release. Do not publish the local Control Plane through a reverse proxy to work
around that gate. Same-host development may exercise the Runner protocol over loopback:

```bash
export RIFTX_RUNNER_REGISTRATION_TOKEN="$(openssl rand -hex 32)"
riftx-runner serve \
  --server-url http://127.0.0.1:8787 \
  --node-id local-runner \
  --name "Local Runner"
```

The former `--registration-token` option has been removed from both Runner commands;
old invocations now fail with an unknown-option error. Migrate by removing the flag and
supplying the bootstrap credential only through an owner-scoped
`RIFTX_RUNNER_REGISTRATION_TOKEN` environment or equivalent process-scoped secret
injection. Never put the credential in argv or shell history.

`remote_multiuser` must add TLS, real remote identity/session controls, and object ACLs
before this endpoint can be made remotely reachable. Never send the bootstrap or rotated
Runner credential over a remote plaintext connection.

Registration returns a rotated node-scoped credential. The daemon stores it with
owner-only permissions and uses it for heartbeats, command polling, execution status,
and bounded output uploads. The bootstrap token can be removed from the Runner host
after its node credential has been persisted. Commands are durable, idempotent, and
leased, so a disconnected daemon can reconnect without starting the same execution
key twice. The Runner executes independent commands concurrently, renews leases for
long-running handlers, and lets cancellation commands preempt in-flight Browser and
Target HTTP work. Cancel-before-start persists a tombstone before it can acknowledge
the stop; already admitted Process/PTY work additionally relies on its immutable
owner-fenced Execution row and durable physical-stop proof to reject replay.
Target HTTP delivery additionally uses an atomic durable claim so an expired or
replayed lease cannot resend a possibly non-idempotent request; a separate durable
physical-stop confirmation is required before a delayed cancellation acknowledgement
can be reused.

## Windows shell execution

`ShellKind.POWERSHELL` resolves PowerShell 7 (`pwsh.exe`) first and falls back to
Windows PowerShell (`powershell.exe`). RiftX always launches it with an explicit argv
(`-NoLogo -NoProfile -Command`) rather than `shell=True`. Windows child processes are
created in a new process group; best-effort cancellation escalates from normal
termination to `taskkill /T /F` after the grace period. This is not equivalent to a
kernel-owned Job Object and does not satisfy `execution.require_containment`; do not use
native Windows execution where immediate, provable whole-tree stop is a safety
requirement.

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
Windows host. ConPTY also lacks RiftX's required kernel containment boundary today, so
safe-default production configuration rejects it rather than claiming a provable stop.

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
