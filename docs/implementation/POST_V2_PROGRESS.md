# RiftX Post-V2 Implementation Progress

## Current Wave

Wave A and Wave B are complete; Wave C is active and CTX-03 is unblocked.

## Completed

- [x] RT-01 Runtime Domain 与状态机
- [x] RT-02 Agent Engine 抽象
- [x] RT-03 Runtime Coordinator 与有限 Cycle
- [x] RT-04 Transcript 与 Session Manager
- [x] EX-01 Execution Service 与幂等性
- [x] EX-02 Wait、Cancel 与 Deferred Execution
- [x] EX-03 Execution Reconciliation
- [x] EX-04 动态 Tool Search 与 Progressive Skill
- [x] CTX-01 Artifact Spill 与 Tool Result Processor
- [x] CTX-02 Context Manifest 与 Token Accounting

## Task Record

### RT-01

- Branch: `codex/rt-01-runtime-domain`
- Commit: `794b093 feat(runtime): add durable runtime domain`
- Completed at: `2026-07-30 11:46 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py`
  - Result: `348 passed, 2 skipped`; Ruff passed.
- Migrations: `e7c3a91f4b20_add_agent_runtime_domain.py`
- Known limitations:
  - `PREPARING` remains as a V2 compatibility `RunStatus`; new Runtime flows use `INITIALIZING -> READY`.
  - Runtime Coordinator, engine integration, and cycle limits are intentionally deferred to RT-02/RT-03.
- Next dependency: RT-02 is unblocked.

### RT-02

- Branch: `codex/rt-02-agent-engine`
- Commit: `980a2a4 feat(runtime): add agent engine abstraction`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py`
  - Result: `358 passed, 2 skipped`; Ruff passed.
- Migrations: None.
- Known limitations:
  - The first adapter targets OpenAI Agents SDK 0.19; additional provider adapters are deferred.
  - Context compilation and durable cycle orchestration remain RT-03 responsibilities.
- Next dependency: RT-03 is unblocked.

### RT-03

- Branch: `codex/rt-03-runtime-coordinator`
- Commit: `bbb771e feat(runtime): add finite cycle coordinator`
- Completed at: `2026-07-30 12:06 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py`
  - Result: `371 passed, 2 skipped`; Ruff passed.
- Migrations: None.
- Core delivery:
  - Database Run Lease prevents concurrent Primary cycles.
  - MinimalContextCompiler preserves the formal Context Compiler boundary.
  - RuntimeCoordinator persists ordered Runtime events, finite Cycle/Step state, Provider State, and all required Yield reasons.
- Known limitations:
  - Execution dispatch/reconciliation remains in Wave B; RT-03 stops at durable Tool proposal yields.
  - The minimal compiler is intentionally replaced by the full compiler in Wave C.
- Next dependency: RT-04 is unblocked.

### RT-04

- Branch: `codex/rt-04-transcript-session`
- Commit: `5d944cf feat(runtime): add transcript session manager`
- Completed at: `2026-07-30 12:24 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py`
  - Result: `378 passed, 2 skipped`; Ruff passed.
- Migration: `f2a6c8d91e04_add_complete_agent_transcript.py`
- Core delivery:
  - Complete provider-neutral Transcript records with per-Session atomic sequencing and stable concurrent-write conflicts.
  - `SessionManager` create/load/suspend/resume/close lifecycle, parent-child validation, and optional Provider State recovery.
  - OpenAI Agents SDK Session remains a compatibility adapter over the authoritative Transcript repository.
  - Runtime Coordinator persists user input, model-visible output, Tool/Subagent proposals, and Checkpoint Boundaries.
- Wave A gate:
  - `tests/runtime/session/test_wave_a_recovery.py` executes three model turns, yields the Cycle, disposes the database engine, creates fresh service objects, and reloads the full ordered Transcript.
  - Gate result: passed; Wave B may begin.
- Known limitations:
  - Tool Result, Approval, and Subagent Result records are defined now and will be emitted by their Wave B/F services when those durable flows are implemented.
  - The SDK compatibility methods `pop_item` and `clear_session` remain available for SDK behavior; Runtime code uses append-only Transcript operations.
- Next dependency: EX-01 is unblocked.

### EX-01

- Branch: `codex/ex-01-execution-service`
- Commit: `f1d7ec6 feat(execution): add idempotent execution service`
- Completed at: `2026-07-30 12:44 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py`
  - `git diff --check`
  - Result: `388 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `a4d7e2c19b63_add_runtime_execution_identity.py`
- Core delivery:
  - Durable execution identity stores `run_id + session_id + tool_call_id + attempt_group` and derives a deterministic bounded idempotency key.
  - `ExecutionService` validates persisted Sessions and Tool Call intents before Runner launch, synchronizes Tool Call status, and returns the existing Execution for duplicate submissions.
  - Local and remote Runner launch paths persist the runtime Execution before dispatch and rely on repository-level create-if-absent semantics for launch-once behavior.
  - Runtime executions expose `QUEUED`, `STARTING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `HARD_TIMEOUT`, and `LOST`, while preserving legacy V2 status compatibility.
  - API and CLI surfaces expose execution identity, list/show, and cancellation operations.
- Known limitations:
  - Bounded wait/deferred execution, Runtime `TOOL_RUNNING` yield integration, process-group cancellation guarantees, and reconciliation are intentionally deferred to EX-02.
  - Retry remains explicit: callers must choose a new `attempt_group`; EX-01 does not automatically retry failed or cancelled executions.
- Next dependency: EX-02 is unblocked.

### EX-02

- Branch: `codex/ex-02-deferred-execution`
- Commit: `308b9b7 feat(execution): add deferred wait and cancellation flow`
- Completed at: `2026-07-30 13:04 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py`
  - `git diff --check`
  - Result: `394 passed, 2 skipped`; Ruff passed; diff check clean.
- Migrations: None.
- Core delivery:
  - Bounded waits return stable `WAIT_TIMEOUT`, `EXECUTION_COMPLETED`, `EXECUTION_CANCELLED`, or `EXECUTION_LOST` outcomes while preserving the underlying Execution state.
  - Wait responses expose partial output cursors and a next-poll hint through Runtime services, API, and CLI; a wait timeout is never reported as tool failure.
  - Local cancellation terminates the complete process group, while Runner hard timeouts remain distinct `HARD_TIMEOUT` execution results.
  - `DeferredExecutionDispatcher` persists a stable Tool Call intent before launch and routes all process creation through `ExecutionService` and Runner.
  - Runtime `TOOL_RUNNING` yields now carry `waiting_execution_id`; repeated Runtime activity with the same engine call returns the same Execution without relaunching.
  - OpenAI Agents events retain stable tool call IDs, tool IDs, and serialized arguments for deferred dispatch.
- Stage gate:
  - Wait timeout followed by a successful second wait, user cancellation, hard timeout, Runtime retry, and full child-process-group cancellation are covered by executable tests.
  - Wave A plus EX-01/EX-02 now form the first bounded long-task Runtime loop; EX-03 may begin.
- Known limitations:
  - Runner/Worker restart recovery and PID reuse checks remain EX-03 responsibilities.
  - The dispatcher consumes launch data already resolved by the Tool Proxy; dynamic Tool Registry resolution is intentionally deferred to EX-04.
  - Temporal workflow signal wiring will consume `waiting_execution_id`; EX-02 establishes the durable Runtime contract without broadening into orchestration refactors.
- Next dependency: EX-03 is unblocked.

### EX-03

- Branch: `codex/ex-03-execution-reconciliation`
- Commit: `cf77e0e feat(execution): reconcile persisted runner state`
- Completed at: `2026-07-30 13:16 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py`
  - `git diff --check`
  - Result: `403 passed, 2 skipped`; Ruff passed; diff check clean.
- Migrations: None.
- Core delivery:
  - `ExecutionReconciler.reconcile_execution` and `reconcile_run` preserve terminal states, inspect active local processes, and reconcile multiple Executions with bounded concurrency.
  - Process identity now checks PID existence, process creation time, executable/argument summary, Runner node ID, and current Execution status to detect PID reuse safely.
  - A matching active process remains `RUNNING`; a missing or mismatched local process becomes `LOST`; an unavailable remote Runner also produces an explicit `LOST` state.
  - Online remote Runners remain authoritative for their processes, and completed Executions are never modified.
  - Reconciliation outcomes emit durable Run events with PID, Runner ID, creation time, command summary, and decision metadata.
- Required scenarios:
  - Runner restart/re-association, reused PID by timestamp, reused PID by command, process disappearance, completed Execution, offline and online remote Runners, concurrent multi-Execution reconciliation, and native PTY deferral are covered.
- Known limitations:
  - Native PTY recovery remains intentionally deferred, as required by EX-03.
  - An online remote Runner remains authoritative until it reports status or its heartbeat becomes unavailable; the Control Plane does not inspect remote host PIDs directly.
  - Reconciliation never restarts a command; explicit retry continues to require a new `attempt_group`.
- Next dependency: EX-04 is unblocked.

### EX-04

- Branch: `codex/ex-04-dynamic-tools-skills`
- Commit: `3e7bd62 feat(runtime): add dynamic tools and progressive skills`
- Completed at: `2026-07-30 13:50 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py`
  - `git diff --check`
  - Result: `418 passed, 2 skipped`; Ruff passed; diff check clean.
- Migrations: None.
- Core delivery:
  - The generation-aware `DynamicToolIndex` derives Level-0 index entries, Level-1 details, and Level-2 function schemas from the existing node-local `ToolRegistry`; it does not create a second source of truth.
  - Deterministic capability and synonym search keeps unavailable tools discoverable while preventing their schema from being selected for execution.
  - `ToolContextManager` provides independent per-Run/Session/Agent dynamic Tool Sets, keeps the required ten resident control tools visible, and records resident, selected, hidden-available, and hidden-unavailable tools in the Context Manifest.
  - Tool Registry hot reload immediately rebuilds the index and refreshes selected schemas by registry generation.
  - File-backed Progressive Skills validate YAML front matter, required procedure sections, and optional input/output JSON schemas while reading only `name`, `description`, and `required_capabilities` during initial indexing.
  - Full `SKILL.md` procedures and `REFERENCES.md` content are loaded independently and only after explicit selection; selected Skill state is isolated per Agent session and represented in compiled context.
  - Tool configuration now supports short/full descriptions, synonyms, and optional input schemas while preserving all existing V2 configurations.
- Wave B gate:
  - `tests/runtime/test_wave_b_gate.py` creates 80 node tools, confirms the initial model context contains only resident schemas, discovers and selects the SMB enumeration schema, launches the long-running tool through `DeferredExecutionDispatcher` and Runner, yields `TOOL_RUNNING`, inspects and waits on the `execution_id`, then completes a subsequent Runtime Cycle.
  - Required 10-tool, 100-tool, capability, synonym, unavailable-tool, independent Subagent Tool Set, Tool Registry hot reload, and Skill front matter scenarios are covered by executable tests.
- Known limitations:
  - Search ranking is intentionally deterministic and lexical; phase, role, and historical-success ranking can be added later without changing the Tool Index contract.
  - `DynamicToolContextCompiler` remains a transitional extension of `MinimalContextCompiler`; Wave C will replace the broader compiler while preserving the visibility manifest and progressive payload contracts.
  - Provider-specific Agent factories remain responsible for binding the resident control schemas to the existing Tool Proxy, Execution Service, and Skill context operations; execution still always crosses the Runner boundary.
- Next dependency: CTX-01 is unblocked.

### CTX-01

- Branch: `codex/ctx-01-artifact-tool-results`
- Commit: `84be341 feat(context): add artifact spill and tool result processing`
- Completed at: `2026-07-30 14:17 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py`
  - `git diff --check`
  - Result: `434 passed, 2 skipped`; Ruff passed; diff check clean.
- Migrations: None.
- Core delivery:
  - Every completed Execution can now produce the required three layers: immutable Raw Artifact references, deterministic Structured Result data, and a bounded Context Summary.
  - `ExecutionArtifactStore` reuses the existing Artifact application service and immutable Runner storage while exposing only canonical `artifact://runs/{run_id}/executions/{execution_id}/{stdout|stderr}` URIs to model-facing data.
  - Artifact reads are bounded by offset and byte count, validate immutable content through the existing service, and return stable missing/integrity failures without exposing local Runner paths.
  - `ToolResultProcessor` implements configurable inline limits, stderr-first head/tail previews, UTF-8/binary handling, parser fallback, key observations, errors, statistics, and logical Artifact references within the context token budget.
  - Deterministic parsers cover Generic Text, Generic JSON, Nmap XML, Nuclei JSONL, and Shell Result; Masscan compatibility remains intact through the existing adapter.
  - Runtime configuration and the example YAML now include the required `execution_output` defaults and validation boundaries.
- Required scenarios:
  - 1 KB inline text, 200 KB head/tail spill, 50 MB incremental preservation, UTF-8, binary output, deterministic parser selection, parser failure fallback, missing Artifact content, stderr larger than stdout, URI validation, bounded reads, and configuration validation are covered by executable tests.
- Known limitations:
  - Deterministic structured parsing is capped at 8 MiB per output; larger machine-readable output remains fully preserved as a Raw Artifact and falls back to bounded generic summarization.
  - CTX-01 provides the processing contract but does not yet persist Context Manifest or model token usage; those are CTX-02 responsibilities.
- Next dependency: CTX-02 is unblocked.

### CTX-02

- Branch: `codex/ctx-02-context-manifest`
- Commit: `5a01f08 feat(context): persist manifests and token usage`
- Completed at: `2026-07-30 14:42 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py migrations/versions/c3b8a7d5e921_add_context_compilations.py`
  - `git diff --check`
  - Result: `442 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `c3b8a7d5e921_add_context_compilations.py`
- Core delivery:
  - Added typed `ContextManifest`, `ContextCompilation`, and `ContextCategoryUsage` contracts with all nine required categories present even when empty.
  - `ManifestingContextCompiler` wraps the existing Context Compiler boundary, classifies the actual compiled payload, estimates category and total tokens, preserves dynamic Tool/Skill visibility metadata, and durably saves every compilation.
  - Tool Schema payloads receive independent item, character, token, and source-reference accounting; Working Memory, Conversation, Tool Results, Retrieved Memory, and Subagent Result items can be classified explicitly or by deterministic input type.
  - The new `context_compilations` table and SQLAlchemy repository persist manifests, estimates, loaded Memory IDs, checkpoints, and actual input/output usage with latest-by-Session and latest-by-Run lookup.
  - Runtime Coordinator automatically detects an observable compiler and backfills provider `input_tokens`/`output_tokens` (including prompt/completion aliases) from durable Agent Engine Usage events.
  - Added `GET /api/v1/sessions/{session_id}/context`, `GET /api/v1/context-compilations/{id}`, and a Run-scoped inspector convenience endpoint used by the interactive `/context` command.
  - CLI Context Inspector renders every category plus estimated and actual token totals.
- Required scenarios:
  - Empty Context, multi-category Context, Tool Schema token accounting, model Usage backfill through Runtime Coordinator, persistence/reload, migration upgrade/downgrade, API inspection, API client routing, and `/context` rendering are covered by executable tests.
- Known limitations:
  - Token estimates intentionally use a deterministic provider-neutral character heuristic; actual provider usage remains authoritative after the Usage event is received.
  - Provider SDKs that perform multiple internal turns currently attach their aggregate Usage to the compilation that launched the engine invocation; per-provider-call spans can be added without changing the persisted Manifest contract.
- Next dependency: CTX-03 is unblocked.

## Architecture Deviations

- The completed V2 domain already exposes a legacy `AgentStep`. The new durable cycle-scoped
  `AgentStep` lives under `riftx.runtime.types` to avoid breaking the V2 API contract.
- The repository uses flat persistence modules, so Runtime ORM records extend the existing
  `persistence/orm.py` metadata while Runtime mappers and repositories use dedicated modules.

## Blockers

- None.
