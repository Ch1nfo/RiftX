<div align="center">

# RiftX

### A Pentest-first Agent that gets better with the operator

RiftX turns authorized objectives, scope, approvals, execution, evidence, findings,
reports, and operator-maintained methods into one recoverable Pentest workflow.

<p>
  <img alt="Version 2.0.0 Alpha" src="https://img.shields.io/badge/version-2.0.0--alpha.0-245dc7?style=flat-square">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Host-native, no Docker required" src="https://img.shields.io/badge/runtime-host--native-2ea44f?style=flat-square">
  <img alt="Local single operator" src="https://img.shields.io/badge/trust-local__single__operator-55d9ff?style=flat-square&labelColor=071632">
  <a href="./LICENSE"><img alt="Apache License 2.0" src="https://img.shields.io/badge/license-Apache--2.0-ffd45a?style=flat-square&labelColor=071632"></a>
</p>

<p>
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="Temporal" src="https://img.shields.io/badge/Temporal-durable_workflows-101010?style=flat-square">
  <img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-Asyncio-D71F00?style=flat-square&logo=sqlalchemy&logoColor=white">
  <img alt="OpenAI Agents SDK" src="https://img.shields.io/badge/OpenAI-Agents_SDK-412991?style=flat-square&logo=openai&logoColor=white">
  <img alt="React 19" src="https://img.shields.io/badge/React-19-149ECA?style=flat-square&logo=react&logoColor=white">
  <img alt="TypeScript 7" src="https://img.shields.io/badge/TypeScript-7-3178C6?style=flat-square&logo=typescript&logoColor=white">
  <img alt="Vite 8" src="https://img.shields.io/badge/Vite-8-646CFF?style=flat-square&logo=vite&logoColor=white">
  <img alt="pnpm 10" src="https://img.shields.io/badge/pnpm-10-F69220?style=flat-square&logo=pnpm&logoColor=white">
</p>

<p>
  <strong>English</strong> · <a href="./README_ZH.md">中文</a>
</p>

</div>

> [!IMPORTANT]
> RiftX `2.0.0-alpha.0` is alpha software for one local professional operator. The
> current trust profile is loopback-only `local_single_operator`. Use RiftX only on
> assets for which you have explicit authorization, and never expose the Control Plane
> to a LAN or the public Internet.

> [!NOTE]
> The Code Audit product surface is retired. Historical records, migrations, read-only
> Snapshot compatibility, and Safety Stop cleanup remain; there is no supported Code
> Audit CLI, API, Worker, Runner, Demo, or Web workflow.

## Pentest-first quick start

This is the supported path from a clean checkout to the first evidence-backed report.
Generic platform commands are optional and are not substitutes for a real authorized
Pentest.

### 1. Install and onboard

The only prerequisites are Python `3.12` and a local Temporal CLI. Use any standard
Python virtual environment; regular users do not need Conda or Docker. Onboard, Control
Plane, Worker, Runner, and the WebUI bundled in the Python package all run host-native.
The `core_path_excludes_docker` release gate checks distribution dependencies and
deployment assets, then verifies Onboard, Doctor, Control Plane, and Pentest admission
with an empty executable `PATH`.

```bash
python -m pip install .
riftx onboard
```

`onboard` interactively creates the user configuration, Model Profile, Tool Registry,
database, and Official Packs without overwriting an existing setup. Configure model
credentials through the selected `RIFTX_MODEL_*` environment variable or the WebUI after
startup. Missing optional executables are reported as degraded capabilities and do not
block the basic Pentest path; run `riftx doctor` when troubleshooting.

Stateful browser Pentests are optional. Install them only on a Runner that needs them:

```bash
python -m pip install ".[browser]"
playwright install chromium
```

Browser, MCP, and Connector integrations are optional extensions. Missing or disabled
integrations degrade only their own capabilities and do not block the basic Pentest path.

### 2. Start the local services

Start the complete local stack with one foreground command:

```bash
riftx start
```

`start` reuses the configured Temporal service. When the default local endpoint is not
running, it starts the Temporal CLI automatically, then launches the Control Plane and
Worker and opens the WebUI. If `RIFTX_ADMIN_TOKEN` is not already set, it generates a
session-only token. Press `Ctrl+C` to stop the processes owned by this command; pass
`--no-open` to keep the browser closed.

The current release supports one local professional operator and keeps the Control Plane
on loopback. Production deployments should continue to supervise separate processes;
deployment, backup, and upgrade details are in [`docs/deployment.md`](docs/deployment.md).

### 3. Start an authorized Pentest

Replace the example target, Scope, and authorization reference with real authorized
values:

When using the CLI from another terminal, inject the same `RIFTX_ADMIN_TOKEN` printed by
`riftx start` into that terminal's environment.

```bash
riftx pentest start \
  --objective "Assess the authorized staging service" \
  --authorization "ticket://SEC-1234" \
  --target "https://staging.example.test" \
  --scope "https://staging.example.test" \
  --model primary

riftx pentest status RUN_ID
riftx approvals RUN_ID
riftx approve APPROVAL_ID
```

Scope, approval, budget, Credential Reference, and stop checks remain authoritative; a
Skill cannot bypass them or add undeclared Tools.

### 4. Generate the report

After the Run reaches `completed`, `failed`, or `cancelled`:

```bash
riftx report generate RUN_ID \
  --format markdown \
  --format json

riftx report list RUN_ID
riftx report show REPORT_ID
```

Professional users can add and iterate local methods through `riftx skills`; see
[`docs/operator-skill-lifecycle.md`](docs/operator-skill-lifecycle.md).

## Why RiftX

RiftX treats every Run as durable operational state. WebUI and CLI are projections,
while resumable workflows and node-local effect owners keep work recoverable and
attributable after a client disconnects.

- **Conversation first** — creating a Run stores its objective and boundaries in
  `waiting_user`; no model or tool starts until the operator sends a concrete instruction.
- **Attributable control** — approvals, terminal takeover, and browser takeover bind to
  stable identities and immutable decisions.
- **Durable execution** — Temporal-backed cycles and Runner-owned effects make retry,
  recovery, and interruption explicit.
- **Evidence by construction** — Artifacts, Findings, Reports, traffic metadata, and
  deterministic graph projections retain provenance.
- **Stop means confirmed** — RiftX fences new effects first and reports a Run stopped
  only after every known owner returns affirmative stop evidence.

## Advanced: product tour

These screens illustrate implemented surfaces; they are not the default Quickstart. Click any
image for the full-resolution view.

<table>
  <tbody>
    <tr>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/01-overview.webp"><img src="docs/assets/readme/en/01-overview.webp" alt="RiftX sanitized operations overview" width="100%"></a>
        <p><strong>Operations overview</strong><br>Durable Runs, scope, approvals, and stop state at a glance.</p>
      </td>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/02-new-run.webp"><img src="docs/assets/readme/en/02-new-run.webp" alt="Create an authorized RiftX Run" width="100%"></a>
        <p><strong>Authorized Run</strong><br>Lock objectives, scope, exclusions, Node, Model, and approval mode first.</p>
      </td>
    </tr>
    <tr>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/03-conversation.webp"><img src="docs/assets/readme/en/03-conversation.webp" alt="Conversation-first RiftX Run" width="100%"></a>
        <p><strong>Conversation first</strong><br>Persist operator intent before the first bounded Agent cycle begins.</p>
      </td>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/04-actions-approval.webp"><img src="docs/assets/readme/en/04-actions-approval.webp" alt="Action approval and execution records" width="100%"></a>
        <p><strong>Actions and approvals</strong><br>Keep Action, Approval, and Execution identities separate and auditable.</p>
      </td>
    </tr>
    <tr>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/05-operation-graph.webp"><img src="docs/assets/readme/en/05-operation-graph.webp" alt="Task evidence and operation graph" width="100%"></a>
        <p><strong>Evidence lineage</strong><br>Trace Task, Asset, Evidence, and Finding relationships without inferred links.</p>
      </td>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/06-http-traffic.webp"><img src="docs/assets/readme/en/06-http-traffic.webp" alt="Metadata-only HTTP traffic inspector" width="100%"></a>
        <p><strong>HTTP metadata</strong><br>Inspect sanitized exchanges with Artifact provenance and no replay surface.</p>
      </td>
    </tr>
    <tr>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/07-terminal-takeover.webp"><img src="docs/assets/readme/en/07-terminal-takeover.webp" alt="PTY transcript and operator takeover" width="100%"></a>
        <p><strong>Terminal takeover</strong><br>Move PTY ownership between Agent and operator while retaining the transcript.</p>
      </td>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/10-reports.webp"><img src="docs/assets/readme/en/10-reports.webp" alt="Markdown HTML and JSON reports" width="100%"></a>
        <p><strong>Evidence-backed reports</strong><br>Assemble Markdown, HTML, and JSON outputs from durable evidence.</p>
      </td>
    </tr>
    <tr>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/13-emergency-stop.webp"><img src="docs/assets/readme/en/13-emergency-stop.webp" alt="Emergency stop owner confirmations" width="100%"></a>
        <p><strong>Affirmative stop proof</strong><br>Fence new effects and wait for every known owner to confirm its disposition.</p>
      </td>
      <td width="50%" valign="top">
        <a href="docs/assets/readme/en/17-connectors.webp"><img src="docs/assets/readme/en/17-connectors.webp" alt="Managed Browser Chrome and Burp connectors" width="100%"></a>
        <p><strong>Browser and connectors</strong><br>Bring Managed Browser, Chrome, and Burp captures into one evidence chain.</p>
      </td>
    </tr>
  </tbody>
</table>

## Advanced: platform capability map

| Area | Implemented capabilities |
| --- | --- |
| Durable Agent runtime | Bounded Agent cycles, dynamic Tool discovery, progressive Skills, structured Working Memory, context compilation and compaction, long-term Memory, Subagents, Hooks, governed MCP integration, retry, replay, and idempotent execution identities |
| Host execution | Registered-only Process, Shell, and PTY; node-local Runner; bounded output; cancel/wait; Linux delegated cgroup v2 containment; Runner-scoped Target HTTP |
| Evidence and observability | Immutable Artifacts, evidence-backed Findings, Markdown/HTML/JSON Reports, deterministic Task/Evidence/Operation projections, resumable SSE, Raw Events, runtime metrics, and logical `artifact://` references |
| Browser and research | Runner-owned Playwright Chromium, stable element references, sanitized observations, takeover summaries, public source registry, research pipeline, Chrome DevTools connector, and Burp Montoya connector |
| Operator configuration | Node inventory, searchable Tool Registry, OpenAI/OpenAI-compatible Model Profiles, `chat_completions` and `responses` request modes, write-only credentials, bilingual WebUI/CLI, and persistent dark/light themes |

## Advanced: architecture

```mermaid
flowchart LR
    Clients["WebUI / CLI<br/>Chrome / Burp"] --> CP["FastAPI Control Plane"]
    CP --> DB["SQLite + Alembic<br/>durable domain state"]
    CP --> Temporal["Temporal Server"]
    Temporal --> Worker["RiftX Worker<br/>Agent + model runtime"]
    CP --> RunnerControl["Runner control<br/>commands + leases"]
    Worker --> RunnerControl
    RunnerControl --> Runner["Node-local Runner"]
    Runner --> Effects["Process / PTY<br/>Browser / Target HTTP"]
    Effects --> Evidence["Artifacts / Workspaces<br/>Findings / Reports"]
    Evidence --> DB
```

The Control Plane, Temporal Worker, and Runner are separate responsibility boundaries.
The outbound Runner protocol exists, but the only selectable trust profile in this
release keeps the Control Plane on loopback; a true remote Runner deployment is not yet
available.

## Contributor and legacy platform setup

### Prerequisites

- Python `3.12` and a Conda environment named `agent`
- Node.js with pnpm `10.32.1`
- Temporal CLI for executing the first Agent instruction
- An isolated Linux Runner with delegated cgroup v2 for high-risk execution that
  requires provable whole-process-tree containment

### Install and start the Control Plane

```bash
conda run --no-capture-output -n agent python -m pip install -e ".[dev]"
conda run --no-capture-output -n agent pnpm install

cp -n configs/tools.example.yaml configs/tools.yaml
cp -n configs/models.example.yaml configs/models.yaml

conda run --no-capture-output -n agent pnpm web:build
export RIFTX_ADMIN_TOKEN="$(openssl rand -hex 32)"

conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml serve
```

Open <http://127.0.0.1:8787/>. The example registries are sanitized templates;
`configs/tools.yaml`, `configs/models.yaml`, and local secret files are ignored by Git.
The WebUI keeps the operator token in page memory rather than browser storage.

At this point the API, read-only UI, and conversation-only Run creation are available.
Sending the first concrete instruction additionally requires Temporal, a RiftX Worker,
and a credential-ready Model Profile.

### Start Temporal and the Worker

```bash
# Terminal 1 — local durable workflow service
mkdir -p .riftx
temporal server start-dev \
  --ip 127.0.0.1 \
  --port 7233 \
  --ui-port 8233 \
  --db-filename .riftx/temporal.db

# Terminal 2 — RiftX workflow/activity worker
conda run --no-capture-output -n agent riftx \
  --config configs/riftx.example.yaml worker
```

Configure a Model Profile in the WebUI before sending a Run's first instruction. For a
managed Temporal service, TLS, authentication, backup, upgrade, and supervised-service
guidance, read [`docs/deployment.md`](docs/deployment.md).

### Create an advanced generic Run

```bash
conda run --no-capture-output -n agent riftx run create \
  "Validate the authorized staging service" \
  --model primary \
  --entry url=https://staging.example.test

conda run --no-capture-output -n agent riftx run message RUN_ID \
  "Begin with passive service identification and report before active probing."
```

## Security model and current limits

> [!CAUTION]
> RiftX is intended only for explicitly authorized security testing. Do not publish the
> current Control Plane through a reverse proxy. Keep administration credentials,
> Runner bootstrap credentials, model keys, Temporal credentials, and target material
> in separate owner-scoped secret channels.

- **Local trust boundary:** `local_single_operator` requires loopback listeners and
  origins; it is not tenant-safe and does not provide multi-user RBAC.
- **Governed effects:** model keys are write-only, and every Agent-visible Process,
  Shell, PTY, Browser, and Target HTTP capability is classified and attributable.
- **Affirmative stop proof:** `failed`, `lost`, an enqueued cancellation, or a missing
  process is not reported as confirmed stopped.
- **Current limit:** provable native process containment requires delegated Linux
  cgroup v2. macOS/Windows lack equivalent whole-tree proof, and the loopback-only
  trust profile does not yet support a remote Runner deployment.

See [Deployment and safety acceptance](docs/deployment.md) and
[Model Profile hardening](docs/model-profile-hardening.md) for the exact invariants.

## Development and verification

All Agent-related tests and runtime commands in this repository use the `agent` Conda
environment.

```bash
# Python and release gates
conda run --no-capture-output -n agent ruff check src/riftx tests migrations
conda run --no-capture-output -n agent python -m pytest
conda run --no-capture-output -n agent python scripts/qa/release-gate.py

# Production WebUI
conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
conda run --no-capture-output -n agent pnpm --filter @riftx/web test
conda run --no-capture-output -n agent pnpm --filter @riftx/web build

```

The Release Gate includes `core_path_excludes_docker` to prevent Docker runtime
dependencies, Docker/Compose deployment assets, or regressions in the host-native core
path.

The authoritative feature-to-evidence matrix and release commands live in
[`docs/v2-completion-audit.md`](docs/v2-completion-audit.md). Run them against the exact
commit being qualified; this README intentionally does not publish stale test counts.

## Documentation

- [Release qualification and implementation coverage](docs/v2-completion-audit.md)
- [Deployment, trust boundaries, backup, and stop acceptance](docs/deployment.md)
- [Model Profile and credential hardening](docs/model-profile-hardening.md)
- [Chrome Connector](apps/browser-extension/README.md)
- [Burp Connector](apps/burp-extension/README.md)

## Contributing

Describe the affected safety boundary and add executable evidence for new behavior.
Never commit credentials, real target details, captured traffic, or generated reports.

## License

RiftX is licensed under the [Apache License 2.0](LICENSE).
Third-party components retain their respective license notices.

## Contact

- Email: [ch1nfo@foxmail.com](mailto:ch1nfo@foxmail.com)
