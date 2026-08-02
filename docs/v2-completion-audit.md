# RiftX V2 completion and release-qualification map

This document maps the V2 design baseline to implementation and executable evidence. It is not a
claim that the current worktree, branch, or release candidate has passed. Historical pass counts
and commit-specific “complete” labels are intentionally omitted because they become stale as soon
as code or tests change.

## Authoritative release gate

A candidate is qualified only when all commands below pass on the exact candidate commit and the
operator completes the harmless deployment checks in [`deployment.md`](deployment.md):

```bash
conda run --no-capture-output -n agent ruff check src/riftx tests migrations
conda run --no-capture-output -n agent python -m pytest
conda run --no-capture-output -n agent python scripts/qa/release-gate.py
conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
conda run --no-capture-output -n agent pnpm --filter @riftx/web test
conda run --no-capture-output -n agent pnpm --filter @riftx/web build
```

The generated pytest and release-gate output is the source of truth for counts and failures. Do
not copy those numbers into this document. Real Windows PowerShell/ConPTY checks remain
host-specific; portable request, lifecycle, error, and fake-native behavior must still pass on
every supported development host.

## Acceptance coverage

“Gate-tracked” means executable evidence exists and must pass in the authoritative gate; it does
not mean the current worktree has already passed.

### Requested-upgrade traceability

| Requested outcome | Current-state evidence | Qualification evidence |
|---|---|---|
| Configure model provider, Base URL, model name, request mode, timeout/retry, default, and write-only credential from WebUI and CLI | `src/riftx/models/`, model API/application services, `src/riftx/cli/app.py`, and `apps/web/src/pages/ModelsPage.tsx` | model unit/API/CLI/Web suites plus the full gate |
| Support `chat_completions` and `responses`, with `chat_completions` as the default | Backend `ModelAPI`, frontend `ModelRequestMode`, sanitized model example, CLI defaults, and Models form defaults | config, registry, provider-wire, CLI, API, and Web tests |
| Create a Run as durable context, open Conversation, and wait for a concrete instruction before starting Temporal/model/tool work | `RunApplicationService`, lazy Temporal client, New Run and Run Detail pages | Run service, Control Plane, Temporal, New Run, and Run Detail tests |
| Aggregate streaming assistant deltas instead of rendering one event card per token | `runStreamReducer.ts`, batched `useEventStream`, Conversation/Timeline/Raw-event projections | reducer, SSE hook, and Run Detail tests |
| Pause/cancel without Temporal and never report success before every known effect has physical stop proof | Run safety/finalization services, Runner durable containment and proof, Browser/Target HTTP stop paths | safety, Runner, Browser, Target HTTP, Temporal-outage, retry, and persistence suites; open P1 findings disqualify the candidate |
| Show confirmed versus unconfirmed stop clearly | Run Detail stop-proof projection and structured API dispositions | Run Detail and Control Plane tests |
| Provide persistent dark/light mode switching | Web theme provider, Layout control, and theme CSS tokens | theme and Layout tests plus production build |
| Keep runtime configuration, credentials, generated state, and build products out of Git while retaining sanitized examples | `.gitignore`, `configs/*.example.yaml`, and metadata-only examples | `git check-ignore`, manual secret scan, and staged-file review |
| Record which CyberStrikeAI ideas were adopted or rejected | `docs/cyberstrikeai-adoption.md` | mapped implementation suites and requirement audit |
| Deliver the work as a reviewed Git commit | final staged diff and commit | `git diff --cached --check`, staged secret review, and commit identity |

| Design requirement | Code evidence | Test evidence | Qualification |
|---|---|---|---|
| Domain isolation, JSON models, guarded state transitions | `src/riftx/domain/` | `tests/unit/domain/` | Gate-tracked |
| Shared Control Plane, persistence, resumable SSE, unified errors | `src/riftx/api/`, `src/riftx/application/services/`, shared CLI/Web clients | API restart, SSE resume, unified error suites | Gate-tracked |
| Durable Temporal runtime, retry idempotency, replay, safety controls | `src/riftx/temporal/`, Run safety services | Temporal replay/control and cleanup-recovery suites | Gate-tracked |
| Dynamic Agent tools, re-planning, HITL resume, bounded context | `src/riftx/agent/`, `src/riftx/runtime/`, `src/riftx/context/` | Agent integration, context, approval recovery suites | Gate-tracked |
| Chat Completions and Responses model profiles | `src/riftx/models/`, model API/CLI/WebUI | model config, registry, wire-protocol, API and Web suites | Gate-tracked |
| Tool Registry, dynamic visibility, policy inventory | `src/riftx/tools/`, `configs/tools.example.yaml` | tool config/discovery/policy and Web suites | Gate-tracked |
| Generic Skills, machine formats, provenance, parser fallback | `src/riftx/skills/`, tool adapters | skill integration and golden adapter suites | Gate-tracked |
| Standalone local/remote Runner, durable IDs/output, cancellation | `src/riftx/runner/`, `src/riftx/executors/` | supervisor, operation-journal, remote-control and cancellation suites | Gate-tracked |
| Explicit Process/Shell, PTY/ConPTY, terminal ownership | executors and terminal services | executor, terminal, PowerShell and ConPTY suites | Gate-tracked; real Windows check required |
| Approval modes and exact decision context | approval domain/application/runtime services | policy, durable approval, CLI and Web suites | Gate-tracked |
| Scope enforcement and effect admission fences | scope guard and execution/browser/HTTP services | scope, effect-service and safety-race suites | Gate-tracked |
| SQLAlchemy persistence, SQLite, Alembic | `src/riftx/persistence/`, `migrations/` | repository, migration and restart suites | Gate-tracked |
| Event-first timeline, stream aggregation and raw audit view | event repository and Web projections | event sequence, SSE reconnect and stream reducer suites | Gate-tracked |
| Dashboard, Conversation-first Run flow, Tools, Models, Nodes, terminal | `apps/web/src/` | Web component/hook/client tests plus production build | Gate-tracked |
| CyberStrikeAI design adoption and explicit non-adoption boundaries | `docs/cyberstrikeai-adoption.md` and the mapped implementation files | The mapped API, policy, model, runtime, and Web suites | Gate-tracked |
| CLI and interactive client use shared APIs | `src/riftx/cli/` | CLI app/client/render tests and entry-point smoke | Gate-tracked |
| Immutable Artifacts, Findings and reports | domain/application/API/Web services | tamper, evidence-link and lifecycle suites | Gate-tracked |
| Layered configuration and external secrets | `src/riftx/config.py`, sanitized examples | runtime config and model secret-hardening suites | Gate-tracked |
| Local, persistent-local, remote and cross-platform modes | production assembly and Runner routing | worker assembly, remote Runner and platform suites | Gate-tracked; host checks may apply |

## End-to-end evidence

The release gate must retain an end-to-end lifecycle covering Run creation, first instruction,
deterministic Agent tool selection, real supervised execution, resumable events, Finding,
Artifact/report generation, and acknowledged cleanup. Safety-specific evidence must also cover
Temporal outage, remote acknowledgement loss, restart/replay, duplicate delivery, and all known
effect families: Execution, Browser, and Target HTTP.

## Explicit V2 boundaries

Release qualification does not broaden the documented product boundary:

- RiftX does not install or distribute external penetration-testing tools.
- It does not claim container/VM sandboxing or full shell semantic enforcement.
- Native PTY sessions are not promised to survive Runner crashes.
- PDF/DOCX export, multi-tenant RBAC, marketplace/update systems, and visual workflow builders are
  outside V2 unless a later tracked design explicitly adds them.
- Host-specific tools are represented by deterministic fixtures in portable automation; required
  real-host smoke checks are recorded separately for the candidate.

Any open P1 safety finding, failed authoritative command, missing required host check, or
unconfirmed stop keeps the candidate unqualified regardless of feature coverage.
