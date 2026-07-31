# RiftX production deployment

This guide covers the deployment details that matter for a durable security-testing
control plane: process separation, persistent state, SSE/WebSocket proxying, secrets,
backup, upgrade, and stop-path verification. The commands assume a Python environment
installed at `/opt/riftx/venv` and a built checkout at `/opt/riftx/app`; adapt paths to
the target host.

## Process and trust boundaries

Run these as separate supervised processes:

1. **Temporal** owns durable Workflow history. Use a separately managed Temporal
   deployment for production; `temporal server start-dev` is for local acceptance only.
2. **Control Plane** serves the WebUI/API and owns application persistence.
3. **Worker** polls the configured Temporal task queue, calls models, and dispatches
   approved work.
4. **Runner** executes processes on each target node. Remote Runners connect outbound
   and should run with only the operating-system privileges required by their tools.
5. **Reverse proxy** terminates TLS and forwards HTTP, SSE, and terminal WebSockets.

Do not combine a public-facing Control Plane and a privileged Runner into one
unrestricted container or operating-system account. Keep the Control Plane reachable
when Temporal is impaired: emergency stop first fences the Run and collects stop
dispositions for known Runner processes, Browser sessions, and Target HTTP requests,
then synchronizes Workflow state on a best-effort basis. Any missing Runner/effect ACK
must remain visibly unconfirmed.

## Configuration and secrets

Copy `configs/riftx.example.yaml` to `/etc/riftx/riftx.yaml`, replace relative paths
with explicit persistent paths, and restrict the file to the RiftX service account.
Keep credentials out of YAML and source control:

```text
RIFTX_CONFIG=/etc/riftx/riftx.yaml
RIFTX_ADMIN_TOKEN=<high-entropy administration token>
RIFTX_MODEL_API_KEY=<provider credential>
RIFTX_RUNNER_REGISTRATION_TOKEN=<independent high-entropy runner token>
RIFTX_TEMPORAL_API_KEY=<managed Temporal credential, when required>
RIFTX_CGROUP_V2_ROOT=<trusted delegated cgroup-v2 directory on every Linux Runner>
```

Store those variables in a root-owned `0600` environment file or a platform secret
manager. A model key written through WebUI/CLI is stored in the configured
`models.secrets_path` with owner-only permissions; it is not written to `models.yaml`.
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
authentication only through `RIFTX_TEMPORAL_API_KEY` in the root-owned service
environment. Equivalent non-secret environment overrides are
`RIFTX_TEMPORAL_TLS_ENABLED`, `RIFTX_TEMPORAL_TLS_SERVER_ROOT_CA_PATH`,
`RIFTX_TEMPORAL_TLS_SERVER_NAME`, `RIFTX_TEMPORAL_TLS_CLIENT_CERT_PATH`, and
`RIFTX_TEMPORAL_TLS_CLIENT_PRIVATE_KEY_PATH`.

The Control Plane rejects non-loopback listen addresses by default because its Run,
Execution, approval, browser, and terminal APIs are host-control capabilities rather
than a multi-tenant public service. Keep `server.host: 127.0.0.1` and let an
authenticated reverse proxy be the only ingress. If that isolation is in place, set
`RIFTX_TRUST_PROXY_AUTH=true` as an explicit acknowledgement; this flag does not inspect
or validate identity headers and is not authentication by itself. Never expose the
Control Plane port directly when the flag is enabled.

RiftX publishes each Control Plane operation's classified authorization boundary and
effect as `x-riftx-authorization` and `x-riftx-effect` in OpenAPI. Startup fails when a
new `/api/v1` route has no inventory entry, and administrator-classified routes are
also checked for the admin-token dependency. Treat this inventory as an auditable
minimum—not a replacement for proxy identity, network isolation, or tenant-aware RBAC.

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
EnvironmentFile=/etc/riftx/riftx.env
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
EnvironmentFile=/etc/riftx/riftx.env
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

Run each remote Runner under a dedicated account on its execution host. Its Control Plane URL
must be the authenticated HTTPS endpoint, not the loopback upstream or a plaintext remote URL:

```ini
[Unit]
Description=RiftX Remote Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=riftx-runner
Group=riftx-runner
EnvironmentFile=-/etc/riftx/runner.env
Environment=RIFTX_RUNNER_STATE=/var/lib/riftx-runner/state
Environment=RIFTX_RUNNER_CREDENTIALS=/var/lib/riftx-runner-identity/credentials.json
Environment=RIFTX_REQUIRE_CONTAINMENT=true
Environment=RIFTX_PAYLOAD_UID=65532
Environment=RIFTX_PAYLOAD_GID=65532
# Adapt this path to the unit's actual unified-cgroup location shown in /proc/self/cgroup.
Environment=RIFTX_CGROUP_V2_ROOT=/sys/fs/cgroup/system.slice/riftx-runner.service/riftx
ExecStart=/opt/riftx/venv/bin/riftx-runner serve --server-url https://riftx.example.test --node-id kali-a --name "Kali Runner A"
Restart=always
RestartSec=2
Delegate=yes
# Required by the trusted launcher solely to clear groups and drop to the
# configured payload identity. Do not grant cgroup administration to that identity.
AmbientCapabilities=CAP_SETUID CAP_SETGID
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/riftx-runner/state /var/lib/riftx-runner-identity

[Install]
WantedBy=multi-user.target
```

Put `RIFTX_RUNNER_REGISTRATION_TOKEN` in the owner-only environment file only for initial
registration. After `/var/lib/riftx-runner-identity/credentials.json` has been created with
owner-only permissions and the node reconnects successfully, remove the bootstrap token from
that host and restart the unit. Preserve `/var/lib/riftx-runner/state` across restarts for
execution identity, cancellation tombstones, and delivery journals. Preserve the credential
file separately as this host's bearer identity; never copy it together with a state snapshot or
restore it onto a second machine. If identity recovery is unavailable, re-register the host and
receive a new instance epoch instead of cloning the old credential.

`Delegate=yes` is required so the service account can create per-Execution leaves below its
service cgroup. Verify the exact unified cgroup v2 path on the target distribution and adjust
`RIFTX_CGROUP_V2_ROOT`; startup of Process/Shell/PTY effects must fail while that root is absent,
not writable, or missing `cgroup.kill` support. Replace the example payload UID/GID with a real
dedicated account, grant that account only the workspace/tool filesystem access it needs, and
verify it cannot write any `cgroup.procs` outside its execution leaf. The Runner needs
`CAP_SETUID`/`CAP_SETGID` only for the launcher privilege drop; do not add `CAP_SYS_ADMIN`.

The exact hardening directives depend on installed security tools. Add only the
filesystem paths and Linux capabilities those tools require; do not disable the unit's
entire sandbox to accommodate one tool.

## Nginx for WebUI, SSE, and terminal WebSockets

SSE must not be buffered. Long-lived event and WebSocket connections also need proxy
timeouts longer than a normal request:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl http2;
    server_name riftx.example.test;

    # Configure certificates and organization authentication here.

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header X-Accel-Buffering no;
    }
}
```

Restrict the Control Plane with network policy and an organization authentication
layer. `RIFTX_ADMIN_TOKEN` protects model and tool administration only; it is not a
complete multi-user identity or tenancy boundary. Keep the upstream Control Plane
listener on loopback. A non-loopback listener requires `RIFTX_TRUST_PROXY_AUTH=true`
and must be isolated so clients cannot bypass the authenticated proxy.

## Startup and acceptance checks

Start dependencies in this order: database/Temporal, Control Plane, Worker, then remote
Runners. A healthy Control Plane alone does not prove that workflows can run.

```bash
curl --fail https://riftx.example.test/healthz
temporal task-queue describe --namespace default --task-queue riftx-v2
/opt/riftx/venv/bin/riftx --api-url https://riftx.example.test node list
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
