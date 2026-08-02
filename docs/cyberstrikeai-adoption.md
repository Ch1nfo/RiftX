# CyberStrikeAI adoption record

This record explains which ideas RiftX adopted after reviewing
[CyberStrikeAI](https://github.com/Ed1s0nZ/CyberStrikeAI), which ideas it deliberately
did not copy, and where the corresponding RiftX evidence lives. The review baseline was
upstream commit `5a5762e1d15d1be40a792733fb8cc26d5d23dfe8` (2026-07-29).

The comparison is architectural, not a claim of compatibility. CyberStrikeAI is a
single-host security workbench; RiftX keeps a split Control Plane, Temporal Worker, and
local/remote Runner architecture with durable domain state.

## Adopted product and engineering ideas

| Idea reviewed | RiftX decision and implementation | Executable evidence |
|---|---|---|
| A new conversation remains idle until the user sends a message | Run creation persists objective and boundaries in `waiting_user`; the first explicit instruction uses idempotent Signal-With-Start. Creation does not contact Temporal, prepare tools, or call a model. | `src/riftx/application/services/runs.py`, `apps/web/src/pages/NewRunPage.tsx`, API/Temporal Run tests, and `NewRunPage.test.tsx` |
| Streaming tokens update one assistant bubble | A dedicated reducer aggregates deltas by cycle/stream identity, accepts authoritative snapshots, deduplicates replay, and keeps raw events in a separate audit projection. | `apps/web/src/pages/runStreamReducer.ts`, `runStreamReducer.test.ts`, `useEventStream.test.tsx`, and `RunDetailPage.test.tsx` |
| Model channels are manageable product objects | Model profiles support WebUI/CLI/API CRUD, a default profile, OpenAI/OpenAI-compatible endpoints, model name, timeout/retry settings, and `chat_completions` (default) or `responses`. Credentials are write-only and stored separately. | `src/riftx/models/`, `src/riftx/api/routes/models.py`, `src/riftx/cli/app.py`, `apps/web/src/pages/ModelsPage.tsx`, and model suites |
| Tools need a searchable, understandable catalog | RiftX exposes registry metadata, capabilities, source digest/configuration provenance, availability, approval level, and a Tools management view while retaining typed arguments and registered execution specs. | `src/riftx/tools/`, `src/riftx/api/routes/tools.py`, `apps/web/src/pages/ToolsPage.tsx`, `tests/unit/tools/`, and `ToolsPage.test.tsx` |
| New capabilities must not silently bypass authorization | Every API route and model-visible Agent tool has a fail-closed authorization/effect inventory. Missing, duplicate, or stale entries fail application/Agent construction. | `src/riftx/api/policy.py`, `src/riftx/tools/policy.py`, `tests/unit/test_api_policy.py`, and `tests/unit/agent/test_tool_policy.py` |
| Multiple tool calls require independently attributable approval | RiftX keeps durable Approval and ToolCallIntent identities and resumes decisions by exact ID, including duplicate/out-of-order signals and Worker replay. | `tests/runtime/test_approval_recovery.py` and `tests/unit/temporal/test_workflow.py` |
| Long operations need get, bounded wait, and cancel semantics | Execution state is durable and independent from one HTTP/model request; a wait timeout does not change the underlying execution result. | Execution services/API, Runner supervisor suites, and remote-control tests |
| A workbench should separate conversation from operational detail | Run Detail defaults to Conversation and separates Tool Calls, Approvals, Terminal, Artifacts, Findings, Reports, high-level Timeline, and Raw events. | `apps/web/src/pages/RunDetailPage.tsx` and its component tests |
| Deployment guidance must cover streaming and persistent state | The deployment guide defines process boundaries, systemd services, Nginx SSE/WebSocket settings, secrets, persistence, backup/upgrade order, and harmless acceptance checks. | `docs/deployment.md` |
| Large tool output should not flood model context | RiftX retains immutable Artifacts and bounded summaries referenced by logical `artifact://` identifiers instead of exposing Runner-local absolute paths. | Context/Artifact services and their suites |

## RiftX-specific safety strengthening

CyberStrikeAI's cancellation model was useful as an interaction reference, but a
penetration-testing control plane cannot equate a queued cancel or context cancellation
with physical stop. RiftX therefore uses a stronger contract:

- pause/cancel first fences admission of new effects;
- Execution, Browser, and Target HTTP owners are contacted independently of Temporal;
- a Run reaches its requested terminal state only after every known effect has an
  affirmative durable stop disposition;
- `failed`, `lost`, a missing in-memory handle, or an enqueued remote command is not
  physical-stop proof;
- the UI distinguishes confirmed and unconfirmed stop and lists each unresolved
  resource;
- safe Linux execution binds a durable identity to a delegated cgroup-v2 leaf and
  requires it to be empty before stop is confirmed.

The authoritative safety evidence is listed in `docs/v2-completion-audit.md`. Any open
P1 safety finding or failed release gate keeps a candidate unqualified even when the
feature UI is complete.

## Deliberately not adopted

RiftX does not copy the following patterns because they conflict with its trust or
durability model:

- a single privileged service combining public UI, orchestration, and tool execution;
- in-memory approval channels that are cancelled or lost on restart;
- arbitrary `additional_args`, `sh -c`, or generic command strings that bypass the
  registered execution specification;
- returning Runner-local absolute file paths to the model;
- describing cancellation as successful before the effect owner confirms physical
  termination;
- moving C2, WebShell, bot, or full asset-management features into scope merely for
  feature parity;
- monolithic front-end files that mix transport, projections, and rendering.

## Intentional follow-on work

The review also identified useful enhancements that are not required to preserve the
current V2 safety and interaction contract: model connection/batch probes, richer
external-MCP authorization and health UI, role-oriented tool collections, bulk approval
UI backed by individual durable decisions, and executable deployment smoke scripts.
They should be added as separately scoped work so they cannot weaken the current
credential, approval, execution, or stop invariants.
