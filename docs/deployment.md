# RiftX production deployment

This guide covers the deployment details that matter for a durable security-testing
control plane: process separation, persistent state, SSE/WebSocket proxying, secrets,
backup, upgrade, and stop-path verification. The commands assume a Python environment
installed at `/opt/riftx/venv` and a built checkout at `/opt/riftx/app`; adapt paths to
the target host.

For local single-operator use, `riftx start` is the supported convenience command: it
starts or reuses local Temporal and supervises the Control Plane and Worker in one
foreground session. The separate-process instructions below remain authoritative for
production service supervision.

The current release deliberately supports only the explicit
`local_single_operator` trust profile. The Control Plane and browser origins must stay
on loopback. `remote_multiuser`, non-loopback listeners, proxy/remote identity, and a
LAN/public reverse-proxy ingress are unavailable and fail closed at startup. This
profile is for one operator on one workstation; it is not a tenant or multi-user ACL.
Docker is not required to install or run the supported RiftX product path. Any
container or VM boundary used for additional payload isolation is an operator-selected
deployment control, not a RiftX runtime dependency.

## Process and trust boundaries

Run these as separate supervised processes:

1. **Temporal** owns durable Workflow history. Use a separately managed Temporal
   deployment for production; `temporal server start-dev` is for local acceptance only.
2. **Control Plane** serves the WebUI/API and owns application persistence.
3. **Worker** polls the configured Temporal task queue, calls models, and dispatches
   approved work.
4. **Runner** executes processes. Under the current profile it must remain on the same
   workstation/loopback boundary and should have only the operating-system privileges
   required by its tools.
5. An optional **same-host loopback proxy** may forward HTTP, SSE, and terminal
   WebSockets without making the service remotely reachable.

The current Control Plane must never be public-facing. Do not combine it and a
privileged Runner in one unrestricted container or operating-system account. Keep it reachable
when Temporal is impaired: emergency stop first fences the Run and collects stop
dispositions for known Runner processes, Browser sessions, and Target HTTP requests,
then synchronizes Workflow state on a best-effort basis. Any missing Runner/effect ACK
must remain visibly unconfirmed.

## Configuration and secrets

Copy `configs/riftx.example.yaml` to `/etc/riftx/riftx.yaml`, replace relative paths
with explicit persistent paths, and restrict the file to the RiftX service accounts.
Keep credentials out of YAML and source control. Do not use one environment file for
multiple processes: the Control Plane and Worker have different inbound trust
boundaries even when they share non-secret YAML and application storage.

An example Control Plane-only `/etc/riftx/control-plane.env` is:

```text
RIFTX_ADMIN_TOKEN=<replace-with-64-random-hex-characters>
# Uncomment only when the corresponding feature/configuration needs it:
# RIFTX_RUNNER_REGISTRATION_TOKEN=<replace-with-independent-64-random-hex-characters>
# RIFTX_TEMPORAL_API_KEY=<replace-with-the-control-plane-temporal-credential>
# RIFTX_MODEL_API_KEY=<replace-with-the-referenced-provider-credential>
# RIFTX_CGROUP_V2_ROOT=<replace-with-the-control-plane-delegated-cgroup-subtree>
```

An independent Worker-only `/etc/riftx/worker.env` is:

```text
# Uncomment only when the corresponding feature/configuration needs it:
# RIFTX_TEMPORAL_API_KEY=<replace-with-the-worker-temporal-credential>
# RIFTX_MODEL_API_KEY=<replace-with-the-referenced-provider-credential>
# RIFTX_CGROUP_V2_ROOT=<replace-with-the-worker-delegated-cgroup-subtree>
```

The Worker environment must not contain `RIFTX_ADMIN_TOKEN` or
`RIFTX_RUNNER_REGISTRATION_TOKEN`; the Worker does not assemble inbound local-operator,
administration, or Runner-registration routes. A same-host Runner daemon uses a third
environment file containing only its bootstrap token during initial registration and
its own execution settings. Remove that bootstrap token after its rotated node
credential is persisted. It must not receive the admin token, model key, or Temporal
credential.

Credential ownership follows the real process assembly:

| Process | Credentials it may hold | Credentials it must not receive |
|---|---|---|
| Control Plane | operator/admin; Runner bootstrap only if registration is enabled; its Temporal credential; referenced model environment key if needed for readiness | Worker-only or Runner node credentials |
| Worker | its Temporal credential; referenced model environment key or access to the configured model secret store | operator/admin; Runner bootstrap; Runner node credential |
| Runner daemon | bootstrap only during registration, then its rotated node-scoped credential | operator/admin; Temporal; model-provider credentials |

Select the trust profile and stable Principal state location explicitly:

```yaml
server:
  host: 127.0.0.1
  port: 8787

security:
  trust_profile: local_single_operator
  local_principal_path: /var/lib/riftx/secrets/local-principal.json
  trust_proxy_auth: false
```

`RIFTX_ADMIN_TOKEN` is reused as the local operator and administration credential.
It must contain at least 32 printable, non-whitespace ASCII characters. This deterministic
minimum is only a guard against missing or trivially weak configuration; it does not
prove entropy. Generate at least 32 random bytes, for example with
`openssl rand -hex 32`. If `RIFTX_RUNNER_REGISTRATION_TOKEN` is set, it must likewise
contain at least 32 printable, non-whitespace ASCII characters and be generated
independently. Weak configured Runner values fail with
`runner_registration_credential_weak`; reuse of the operator credential fails with
`operator_runner_credential_reuse`. Missing and weak operator credentials fail before
the API security boundary is assembled with `local_operator_credential_required` or
`local_operator_credential_weak`.

Provide the Runner bootstrap credential only through an owner-scoped
`RIFTX_RUNNER_REGISTRATION_TOKEN` environment or equivalent process-scoped secret
injection. The former `--registration-token` option has been removed from both Runner
commands, so old invocations fail with an unknown-option error. Migrate by removing the
flag; never put the credential in argv or shell history, where it may be exposed to
other users, process monitors, and diagnostic tooling.

The server generates a separate stable LocalPrincipal ID on first start; its schema-only
state file contains no token and must remain owned by the service account with mode
`0600` in an owner-only directory. Rotating the token does not change that actor ID.
The Control Plane currently requires a POSIX filesystem/runtime with `dir_fd`,
`O_NOFOLLOW`, owner/mode checks, hard-link no-replace publication, and directory
`fsync`. Windows or any platform without those primitives fails before creating state
with `local_principal_platform_unsupported`. This restriction applies to the Control
Plane's Principal store, not to the separate Runner execution compatibility matrix.
Configure a canonical, symlink-free path; aliases such as macOS `/tmp` (a symlink to
`/private/tmp`) are intentionally rejected rather than followed.

Store each process's variables in its own root-owned `0600` environment file or scoped
platform-secret identity. A model key written through WebUI/CLI is stored in the
configured `models.secrets_path` with owner-only permissions; it is not written to
`models.yaml`.
Remote model-profile management may reference only `RIFTX_MODEL_*` credential
variables. Keep unrelated database, cloud, Temporal, Runner, and administration secrets
outside that namespace.

### Required process containment

Keep the production-safe default enabled:

```yaml
execution:
  require_containment: true
  # Host-specific numeric IDs may instead be supplied through
  # RIFTX_PAYLOAD_UID and RIFTX_PAYLOAD_GID.
  payload_uid: 65532
  payload_gid: 65532
```

Every Linux host that executes Process, Shell, or PTY work must provide a dedicated,
writable cgroup v2 subtree through `RIFTX_CGROUP_V2_ROOT`. The Runner creates one leaf
per durable Execution, launches the target behind an activation gate only after the
leaf identity is persisted, and confirms stop only after `cgroup.kill` and
`cgroup.events: populated 0`. Validate that the delegated root exposes
`cgroup.events`/`cgroup.procs` and that new leaves expose `cgroup.kill` and
`cgroup.max.descendants`.

The delegated subtree is a trust boundary, not merely a writable directory. Create a
dedicated payload account that is different from the Runner account and has no cgroup
administration rights. Configure its numeric UID/GID above. The trusted launcher joins
the execution leaf, clears supplementary groups, drops GID/UID, enables Linux
`no_new_privs`, verifies that the payload identity cannot open the delegated root or an
ancestor `cgroup.procs` for writing, and only then announces readiness. Missing identity,
an identity equal to the Runner, failed privilege drop, or writable ancestor all reject
the start. A separate container/runtime boundary remains recommended for adversarial
payloads and network isolation remains the external last-resort boundary.

Do not set `execution.require_containment: false` in a penetration-testing deployment.
Native macOS and Windows Process/ConPTY execution do not currently provide an equivalent
whole-tree proof and are rejected by the safe default; route such work to a contained
Linux Runner.

### Managed Temporal TLS and authentication

The Control Plane and Worker use the same Temporal connection settings. Put the
non-secret endpoint, namespace, and TLS policy in `/etc/riftx/riftx.yaml`:

```yaml
temporal:
  target: your-namespace.your-account.tmprl.cloud:7233
  namespace: your-namespace.your-account
  task_queue: riftx-v2
  workflow_id_prefix: riftx-run
  tls_enabled: true
```

With `tls_enabled: true` and no additional TLS fields, RiftX verifies the server using
the operating-system root store. For a private CA, server-name override, or mTLS, add
only PEM file paths:

```yaml
temporal:
  target: temporal.internal.example:7233
  namespace: production
  task_queue: riftx-v2
  workflow_id_prefix: riftx-run
  tls_enabled: true
  tls_server_root_ca_path: /etc/riftx/temporal/server-ca.pem
  tls_server_name: temporal.service.internal
  tls_client_cert_path: /etc/riftx/temporal/client-cert.pem
  tls_client_private_key_path: /etc/riftx/temporal/client-key.pem
```

The client certificate and private key must be configured together. API-key, custom-CA,
server-name, and mTLS settings are rejected unless TLS is enabled. Certificate and key
files must be readable by both service accounts; keep the private key owner-restricted.
Never paste API keys, certificates, or private keys into YAML. Supply API-key
authentication only through `RIFTX_TEMPORAL_API_KEY` in each process's scoped,
root-owned environment file. Use distinct Temporal credentials when the deployment
supports that separation. Equivalent non-secret environment overrides are
`RIFTX_TEMPORAL_TLS_ENABLED`, `RIFTX_TEMPORAL_TLS_SERVER_ROOT_CA_PATH`,
`RIFTX_TEMPORAL_TLS_SERVER_NAME`, `RIFTX_TEMPORAL_TLS_CLIENT_CERT_PATH`, and
`RIFTX_TEMPORAL_TLS_CLIENT_PRIVATE_KEY_PATH`.

The Control Plane rejects non-loopback listen addresses because its Run, Execution,
approval, browser, and terminal APIs are host-control capabilities rather than a
multi-tenant public service. Keep `server.host: 127.0.0.1` and
`security.trust_proxy_auth: false`. `RIFTX_TRUST_PROXY_AUTH=true` is rejected; it is not
an authentication mechanism or an escape hatch. The WebUI keeps the local token in page
memory, REST/SSE send it as Bearer authentication, and browser WebSockets send it in a
credential subprotocol while the server echoes only the fixed protocol marker.

RiftX publishes each Control Plane operation's classified authorization boundary and
effect as `x-riftx-authorization` and `x-riftx-effect` in OpenAPI. Startup fails when a
new `/api/*` route has no inventory entry, and administrator-classified routes are
also checked for the admin-token dependency. Treat this inventory as an auditable
minimum—not as proxy identity or tenant-aware RBAC, neither of which the local profile
implements.

The management API rejects literal link-local, unspecified, and multicast model
destinations while preserving loopback for local model servers. This is not a complete
SSRF boundary: DNS names and private network endpoints may still resolve to sensitive
services. In production, allowlist model-provider destinations with firewall/service
mesh egress policy, block metadata ranges after DNS resolution, and protect against DNS
rebinding at the network layer.

Persist and back up the following independently:

- the application database configured by `database.url`;
- the Temporal persistence backend and namespace history;
- `workspace.root`, including execution output and immutable artifact sources;
- each Runner's `runner.state_path`, including durable cancellation state;
- each Runner's separate `runner.credential_path`, as a host identity that must never be
  cloned to a second Runner instance;
- `security.local_principal_path`, which preserves the stable server-owned actor ID but
  contains no credential;
- model-profile metadata and its separate credential store;
- the deployed RiftX configuration and tool registry.

Never place model keys, admin tokens, Runner tokens, local YAML overrides, or `.riftx/`
state in a Git repository or image layer.

## Build and supervised services

Build the static WebUI before starting the Control Plane:

```bash
cd /opt/riftx/app
pnpm install --frozen-lockfile
pnpm web:build
/opt/riftx/venv/bin/python -m pip install --no-deps .
```

Example systemd unit for the Control Plane:

```ini
[Unit]
Description=RiftX Control Plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=riftx
Group=riftx
WorkingDirectory=/opt/riftx/app
EnvironmentFile=/etc/riftx/control-plane.env
ExecStart=/opt/riftx/venv/bin/riftx --config /etc/riftx/riftx.yaml serve
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/riftx /var/log/riftx

[Install]
WantedBy=multi-user.target
```

Run the Worker under a separate unit. The example uses the same account because the
Control Plane and Worker share the application database and owner-only model credential
store; a separate account requires an explicit group/ACL and secret-manager design:

```ini
[Unit]
Description=RiftX Temporal Worker
After=network-online.target riftx-control-plane.service
Wants=network-online.target

[Service]
Type=simple
User=riftx
Group=riftx
WorkingDirectory=/opt/riftx/app
EnvironmentFile=/etc/riftx/worker.env
# Defense in depth: remove inbound Control Plane credentials even if a service
# manager or drop-in tried to add them from another source.
UnsetEnvironment=RIFTX_ADMIN_TOKEN RIFTX_RUNNER_REGISTRATION_TOKEN
ExecStart=/opt/riftx/venv/bin/riftx --config /etc/riftx/riftx.yaml worker
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/riftx /var/log/riftx

[Install]
WantedBy=multi-user.target
```

The outbound Runner credential protocol remains in the codebase, but a remote Runner
requires a remotely reachable Control Plane. That deployment is unavailable while only
`local_single_operator` can be selected. Do not publish the Control Plane or reuse the
Runner protocol as a substitute for the missing remote trust profile. A same-host
Runner may connect to `http://127.0.0.1:8787`; keep its state and node credential files
owner-only and preserve the containment requirements above.

## Local WebUI, SSE, and terminal WebSockets

The built WebUI is served by the Control Plane on the same loopback origin. This avoids
an additional proxy in the supported deployment. REST, artifact downloads, and SSE use
the local operator Bearer token. Browser WebSockets use the fixed RiftX protocol marker
plus an encoded credential subprotocol; the credential never belongs in the URL, and
the server negotiates only the fixed marker. Treat the credential subprotocol as Bearer
credential material: access logs, reverse-proxy logs, tracing, and error reports must
never record the `Sec-WebSocket-Protocol` request header. The allowed development
origins are the configured Vite loopback origins, while the production origin is derived from
`server.host` and `server.port`.

If a same-host loopback-only proxy is needed for local operations, preserve
`Authorization` and `Sec-WebSocket-Protocol`, disable SSE buffering, and keep both its
listener and upstream on loopback. It must not inject identity headers or make RiftX
reachable from another host.

## Startup and acceptance checks

Start dependencies in this order: database/Temporal, Control Plane, Worker, then any
same-host Runner. A healthy Control Plane alone does not prove that workflows can run.

```bash
curl --fail http://127.0.0.1:8787/healthz
temporal task-queue describe --namespace default --task-queue riftx-v2
RIFTX_ADMIN_TOKEN="replace-with-a-long-random-local-token" \
  /opt/riftx/venv/bin/riftx --api-url http://127.0.0.1:8787 node list
```

Before authorizing real testing, perform a harmless acceptance Run and verify all of
the following:

1. Creating the Run returns immediately in `waiting_user`, opens Conversation, and does
   not call a model or prepare tools.
2. Sending the first instruction starts the durable Workflow and produces one streamed
   assistant bubble rather than one card per token.
3. A sensitive fixture produces an exact command/target approval; approve and reject
   paths both resume correctly.
4. Stop harmless active process, Browser, and Target HTTP fixtures. The UI must report
   success only after every known effect is confirmed stopped, even if Temporal is
   unavailable; remote effects without an ACK must remain visibly unconfirmed. Treat
   Execution `failed` and `lost` as unconfirmed until the owning Runner reports
   `cancelled` after a physical process/PTY check.
5. Disconnect and reconnect SSE; already delivered deltas must not duplicate.
6. Restart the Worker while a Run is waiting for approval/execution and confirm Temporal
   replay resumes the same durable IDs without duplicate execution.

## Unconfirmed-stop containment

Treat `safety_stop_failed`, `execution_cancel_failed`, or a Run left in `pausing` or
`cancelling` as an active safety incident, not as a successful stop. Do not resume the Run,
approve another effect, delete its state, or start a replacement Run against the same target.

1. Record the Run ID and every unconfirmed resource ID, owning node, observed state, and reason
   shown by the API, CLI, or WebUI. Preserve Control Plane, Temporal, and Runner logs.
2. Immediately contain the owning execution host at an external boundary while keeping its
   Runner control channel available when possible. Revoke the testing VPN/route, block target
   egress, or isolate the host through the organization's network/EDR controls. Do not assume
   that stopping the Worker or Control Plane stops child processes.
3. Inspect the owning Runner host using the persisted execution ID, PID/process-group identity,
   terminal record, Browser session, or Target HTTP request ID. Stop only the identified process
   tree, PTY/ConPTY, browser context/process, or network operation; avoid an unscoped host-wide
   kill unless the incident procedure explicitly requires it.
4. Restore the Runner control channel and retry pause/cancel. The Run may become
   `paused`/`cancelled` only after the owning Runner reports affirmative physical-stop evidence
   for every known effect. `failed`, `lost`, a queued close command, or an absent in-memory
   handle is not acknowledgement.
5. If any effect still cannot be proven stopped, keep network containment in place, leave the
   Run fenced, preserve its durable state, and escalate through the organization's incident and
   authorization contacts. Never edit the database to manufacture a terminal status.

Only release containment after the effect owner acknowledges the stop and the durable Run state
reflects that acknowledgement.

## Backup and upgrade

For SQLite deployments, stop the Worker first and use SQLite's online backup command or
stop the Control Plane briefly before copying the database. Copying a live database
file directly is not a backup procedure. Back up workspaces, Runner state, model
metadata, and each Runner credential store independently with permissions preserved.
Treat a Runner credential as a non-clonable host identity: a restore may replace the
identity on the same host, but it must never create two live copies. Back up Temporal
using the procedure supported by its configured persistence backend.

Upgrade sequence:

1. Pause or cancel active Runs and confirm every known Execution, Browser session, and
   Target HTTP request has stopped.
2. Back up application and Temporal state.
3. Stop Workers, then the Control Plane; do not terminate Runners with unconfirmed work.
4. Install the release, export `RIFTX_DATABASE_URL` with the exact database URL used
   by the Control Plane, run `alembic upgrade head`, and rebuild the WebUI. Alembic
   reads this environment variable; it does not read `database.url` from the RiftX YAML,
   and without it the command would migrate the development SQLite default instead.
5. Start the Control Plane and Workers, verify task-queue pollers, then reconnect Runners.
6. Repeat the harmless acceptance checks above before restoring access.

If an upgrade fails, preserve the database, Temporal history, Runner state, workspaces,
and secrets. Roll back code only to a version whose database and Workflow history are
documented as replay-compatible.
