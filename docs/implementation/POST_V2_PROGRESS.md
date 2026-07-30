# RiftX Post-V2 Implementation Progress

## Current Wave

Wave A through Wave G are complete; Wave H is active and QA-02 is unblocked.

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
- [x] CTX-03 Working Memory 与 Reducer
- [x] CTX-04 Context Compiler 与 Token Budgeter
- [x] CTX-05 Stable Instructions
- [x] DUR-01 Temporal Durable Cycle
- [x] DUR-02 Approval 与 User Input 恢复
- [x] DUR-03 PTY Runtime 与所有权
- [x] DUR-04 Checkpoint、Compaction 与模型切换
- [x] MEM-01 Long-Term Memory Store
- [x] MEM-02 Memory Candidate 与自动 Promotion
- [x] MEM-03 Subagent 与 Hook
- [x] EXT-01 MCP Governance
- [x] EXT-02 Fact Promotion 与 Attack Graph
- [x] WEB-01 Source Registry 与 Public Fetch
- [x] WEB-02 Search Provider 与 Research Pipeline
- [x] WEB-03 Target HTTP
- [x] WEB-04 Managed Browser 与用户接管
- [x] WEB-05 Browser 与 Burp Connector
- [x] QA-01 Long-Horizon 与 Recovery Eval

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

### CTX-03

- Branch: `codex/ctx-03-working-memory`
- Commit: `b2c6e2b feat(context): add structured working memory reducer`
- Completed at: `2026-07-30 15:02 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py migrations/versions/c3b8a7d5e921_add_context_compilations.py migrations/versions/d9f4a6c2b731_add_working_memories.py`
  - `git diff --check`
  - Result: `449 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `d9f4a6c2b731_add_working_memories.py`
- Core delivery:
  - Added structured `CurrentFocus`, `RunPlan`, `ConfirmedFact`, `Hypothesis`, `AttemptRecord`, `UserDecision`, `PendingQuestion`, `ActiveExecutionRef`, `ActiveTerminalRef`, `NextAction`, and authoritative per-Run `WorkingMemory` contracts.
  - Model-originated changes are constrained to typed `PlanUpdateProposal`, `FactCandidate`, `HypothesisUpdate`, and `AttemptRecord` inputs; `WorkingMemoryReducer` deterministically merges them instead of accepting whole-state replacement.
  - Fact merging retains historical values, deduplicates source references, raises confidence only for independent evidence, marks conflicts `DISPUTED`, and resolves a unique highest-priority deterministic Parser value over model inference.
  - Hypothesis evidence links to known Fact IDs and deterministically drives `SUPPORTED`, `CONFIRMED`, `INVESTIGATING`, or `REJECTED` state without allowing statement overwrite.
  - Completed Plan items cannot regress without an explicit reopen reason, and identical failed Attempts block repeat execution unless a prior retryable Attempt is explicitly referenced with a retry reason.
  - The `working_memories` table stores structured JSON state plus a durable version; the SQLAlchemy repository uses compare-and-swap updates so concurrent stale writers fail rather than silently overwriting state.
- Required scenarios:
  - Fact addition, confidence increase from multiple sources, deterministic-vs-model Fact conflict, Hypothesis support and rejection, duplicate failed Attempt blocking, completed Plan regression, persistence/reload, migration upgrade/downgrade, reducer version mismatch, and repository optimistic-lock conflict are covered by executable tests.
- Known limitations:
  - CTX-03 intentionally defines and persists authoritative Working Memory but does not inject it into model prompts; CTX-04 owns the single Context Compiler and token-budget integration.
  - Current Fact confidence aggregation is provider-neutral and evidence-count based; future domain-specific calibration can evolve behind the same reducer contract.
- Next dependency: CTX-04 is unblocked.

### CTX-04

- Branch: `codex/ctx-04-context-compiler-budgeter`
- Commit: `d71b55f feat(context): add layered compiler and token budgeter`
- Completed at: `2026-07-30 15:40 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py migrations/versions/c3b8a7d5e921_add_context_compilations.py migrations/versions/d9f4a6c2b731_add_working_memories.py`
  - `git diff --check`
  - Result: `457 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: None.
- Core delivery:
  - Added the concrete, provider-neutral `ContextCompiler.compile(...)` as the single layered input-construction implementation; the former `MinimalContextCompiler` and `DynamicToolContextCompiler` names now delegate to it rather than assembling independent prompts.
  - Added typed `ContextItem`, `ContextLayer`, `ContextItemKind`, and `ContextSource` contracts carrying priority, estimated tokens, required/compressible/removable controls, relevance, sequence, and source references.
  - The compiler deterministically loads Runtime Contract, Stable Instructions, Run Contract, Working Memory, latest Checkpoint, recent Conversation, Tool Results, retrieved Memory, Subagent Results, dynamic Tool Schemas, and current input in the required order.
  - `WorkingMemoryContextSource` separately protects Current Plan, failed Attempts, pending Approvals, active Executions, and active Terminals; `TranscriptContextSource` classifies recent messages into budgetable Conversation, Tool Result, and Subagent layers.
  - `TokenBudgeter` applies per-category and global limits, compresses eligible large items, and evicts old Tool previews, duplicate results, low-value Memory, old Assistant detail, and chatter before high-value state. Required or non-removable items are never silently discarded; an explicit `RequiredContextOverflowError` reports the protected IDs and token deficit.
  - Resident Tool schemas remain required while selected dynamic schemas compete within the Tool Schema budget; Progressive Skill summaries, documents, and references pass through the same Stable Instruction budget.
  - Runtime Coordinator now reconstructs the full Run Contract, including Scope, success criteria, entry points, approval mode, node, and workspace, for every compilation. The OpenAI Agents adapter replaces factory prompt instructions with the compiler output before start and resume.
  - Every selected model-visible item maps to exactly one Manifest category; selected/dropped/compressed IDs and per-item tokens are recorded, and the exact final Manifest can be persisted directly without reclassification drift.
- Required scenarios:
  - Normal layered compilation, oversized Tool Result compression, excessive Tool Schema eviction, explicit required-item overflow, dynamic Tool Schema selection, final input/Manifest consistency, canonical Manifest persistence, legacy compiler delegation, full Run Contract wiring, and engine instruction application are covered by executable tests.
- Known limitations:
  - Token estimates remain deterministic and provider-neutral; actual provider Usage continues to be authoritative after the model call.
  - The filesystem-backed Stable Instruction source is intentionally deferred to CTX-05 and will plug into the Stable Instructions layer without creating another compiler.
- Next dependency: CTX-05 is unblocked.

### CTX-05

- Branch: `codex/ctx-05-stable-instructions`
- Commit: `61418ed feat(context): add stable instruction hierarchy`
- Completed at: `2026-07-30 16:54 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q tests/context/test_instructions.py tests/context/test_wave_c_gate.py tests/runtime/coordinator/test_coordinator.py`
  - `conda run --no-capture-output -n agent pytest -q tests/context tests/runtime`
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations/versions/e7c3a91f4b20_add_agent_runtime_domain.py migrations/versions/f2a6c8d91e04_add_complete_agent_transcript.py migrations/versions/a4d7e2c19b63_add_runtime_execution_identity.py migrations/versions/c3b8a7d5e921_add_context_compilations.py migrations/versions/d9f4a6c2b731_add_working_memories.py`
  - `git diff --check`
  - Result: `466 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: None.
- Core delivery:
  - Added the filesystem-backed `StableInstructionSource` for `~/.config/riftx/RIFTX.md`, `<engagement>/.riftx/RIFTX.md`, `<workspace>/.riftx/RIFTX.md`, and `<current-path>/.riftx/RIFTX.md`.
  - Instructions render from global to current path so the most specific scope is last and authoritative; duplicate roots are loaded once under their most specific scope.
  - Engagement, Workspace, and current-path containment is validated before reading, and instruction symlinks cannot escape their configured roots.
  - Stable Instructions have a configurable hard cap with a 4,096-token default. Allocation preserves more-specific files first, deterministically truncates the boundary file, and records selected, dropped, and truncated paths in the Context Manifest.
  - `MinimalContextCompiler` and `DynamicToolContextCompiler` now enable the source by default. Runtime Coordinator passes the persisted Workspace plus explicit Engagement/current-path roots into every Context compilation.
  - `processed_tool_result_context_item(...)` creates the model-facing Tool Result layer from bounded summaries and logical Artifact URIs only; raw previews and Runner paths are excluded.
- Wave C gate:
  - `tests/context/test_wave_c_gate.py` performs 30 consecutive Tool Result / model-context compilations, persists one exact Manifest for every call, keeps Objective, Scope, and failed Attempts, rejects an unexplained repeated failed Attempt, and holds an 80-tool catalog to the resident control schemas.
  - Every synthetic processed Tool Result carries a unique raw-output trap string in its preview; none appears in any compiled model input, while bounded summaries and immutable Artifact references remain available.
- Known limitations:
  - The Engagement filesystem root is an explicit Runtime Cycle input because the current persisted Engagement domain does not own a filesystem path; Workspace and current path are wired automatically by Runtime Coordinator.
  - The 30-call Wave C gate is provider-neutral and deterministic; live provider behavior and cross-platform Runner execution remain separate integration/CI evidence.
- Next dependency: DUR-01 is unblocked.

### DUR-01

- Branch: `codex/dur-01-temporal-cycle`
- Commits:
  - `2598ae2 feat(runtime): persist durable cycle outcomes`
  - `a4c28e4 feat(temporal): drive workflow with runtime cycles`
  - `7729a4b feat(temporal): wire durable runtime worker`
- Completed at: `2026-07-30 17:34 CST`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q tests/unit/temporal tests/runtime tests/execution tests/runner tests/integration/api/test_control_plane.py`
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `474 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `b7e1d2c3f4a5_persist_durable_cycle_outcomes.py`
- Core delivery:
  - `RiftXRunWorkflow` now drives the finite `RuntimeCoordinator` through `run_agent_cycle_activity` with stable per-cycle IDs. Temporal Activity retries reuse the persisted yielded Cycle result instead of invoking the model or launching an Execution again.
  - Workflow state and signals carry durable identifiers: Run, Session, Cycle, Yield reason, waiting object, checkpoint, Execution, Approval, and User Input IDs. User message content is persisted before signaling and is resolved into the authoritative Transcript inside the Activity.
  - Execution completion, Approval, User Input, pause, resume, and cancel signals deterministically resume or stop the outer Workflow. Local Runner completion persists the terminal Execution before signaling its ID, with bounded retry on transient signal failure.
  - Production Worker assembly now registers the new Runtime Activity, idempotently creates the primary Agent Session, uses the layered Context Compiler, OpenAI Agents Engine adapter, Database Run Lease, durable Transcript, and deferred Execution dispatcher.
  - Model function tools return a deferred marker and stop the SDK turn; trusted Registry resolution builds Runner launch data outside the model process, enforces Workspace containment, and preserves Execution Service idempotency.
  - Completed Executions are reloaded from persistence, synchronized through `ExecutionService`, processed into immutable Artifacts plus a bounded Context Summary, and supplied to the next Cycle without placing raw output or local paths in Temporal history.
- Required scenarios:
  - Worker restart, Workflow Replay, Tool running, Run pause/resume, Run cancel, Activity retry, stable Cycle ID, and no duplicate Execution launch are covered by executable tests.
- Known limitations:
  - Approval policy variants, rejection feedback, exact original `ToolCallIntent` recovery, and durable `UserInputRequest` records are intentionally owned by DUR-02.
  - PTY ownership/takeover is intentionally owned by DUR-03; provider-neutral checkpoint compaction and model switching remain DUR-04 scope.
  - The V2 prepare/report/cleanup Activities remain registered as compatibility boundaries while the primary Agent Cycle is now the post-V2 Runtime Activity.
- Next dependency: DUR-02 is unblocked.

### DUR-02

- Branch: `codex/dur-02-approval-input`
- Commits:
  - `5e08173 feat(runtime): persist approval and input requests`
  - `cf03453 refactor(execution): persist deferred launch snapshots`
  - `9eaade0 feat(runtime): recover durable approval decisions`
  - `df19317 feat(runtime): recover durable user input waits`
  - `de71f51 test(schema): track DUR-02 persistence tables`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `480 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `f8a2c4d6e910_add_runtime_approval_user_input.py`
- Core delivery:
  - Approval mode is enforced from the authoritative Run policy for `AUTO`, `BALANCED`, and `MANUAL`, including durable per-Run tool grants.
  - Each pending approval snapshots the original `ToolCallIntent`, trusted Runner execution specification, Context Compilation ID, Working Memory version, Provider State, and Approval ID before the Workflow waits.
  - `APPROVE_ONCE` and `APPROVE_TOOL_FOR_RUN` execute the persisted launch snapshot without asking the model to regenerate a command. `REJECT` and `REJECT_WITH_FEEDBACK` durably reject the Intent and resume the provider with the exact decision and feedback.
  - Approval decisions and execution submission are idempotent across duplicate API submissions, Activity retries, and reconstructed Worker service objects.
  - `UserInputRequest` persists the prompt and context/provider snapshots before `WAITING_USER`; the queued user message is first appended to the authoritative Transcript, then marks the request answered and resumes the next Cycle from Provider State.
- Required scenarios:
  - Approve, Reject, Reject With Feedback, Approval pending across Worker reconstruction, User Input waiting across Worker reconstruction, and duplicate Approval submission are covered by executable tests.
- Known limitations:
  - Interactive PTY ownership and user takeover are intentionally owned by DUR-03.
  - Provider-neutral checkpoint compaction and model switching remain DUR-04 scope.
- Next dependency: DUR-03 is unblocked.

### DUR-03

- Branch: `codex/dur-03-pty-runtime`
- Commits:
  - `4a5b881 feat(terminal): enforce shared read-only ownership`
  - `ace680e feat(terminal): persist takeover runtime state`
  - `3ff2e89 feat(terminal): archive takeover streams and summaries`
  - `ca7c08e feat(runtime): yield durable interactive terminals`
  - `e3440cb test(terminal): reject agent writes during takeover`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `483 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `a9c3e5f7b102_add_terminal_runtime_state.py`
- Core delivery:
  - Unix PTY and Windows ConPTY controllers retain bounded read, write, resize, interrupt, and close operations while Terminal ownership now exposes `AGENT`, `USER`, and `SHARED_READ_ONLY`; legacy `shared` rows remain readable with read-only semantics.
  - TerminalSession durably stores Runner, shell, cwd, output/takeover cursors, takeover start time, and final Transcript Artifact ID. Runner reconstruction marks unattached PTY/ConPTY sessions `LOST` without trying to reattach them.
  - User takeover immediately rejects Agent writes while preserving Agent reads. Release archives the complete takeover character delta as an immutable Artifact and emits a bounded `TerminalTakeoverSummary` before returning ownership to the Agent.
  - Terminal close archives the complete character stream as an Artifact; raw terminal bytes never enter the ordinary Transcript.
  - Runtime-selected PTY tools persist the original ToolCallIntent, launch deterministic terminal and execution IDs through TerminalService, yield `TERMINAL_OPEN`, reuse the same terminal on Activity retry, and signal Temporal with the terminal Execution ID after completion.
- Required scenarios:
  - Interactive PTY fixture, Ctrl+C, resize, user takeover, rejected Agent writes, user release and summary, Runtime retry, and Runner restart to `LOST` are covered by executable tests.
- Known limitations:
  - Real ConPTY and PowerShell smoke tests remain host-dependent and were skipped on this macOS host; mocked ConPTY lifecycle tests passed in the full suite.
  - Provider-neutral checkpoint compaction and model switching are intentionally owned by DUR-04.
- Next dependency: DUR-04 is unblocked.

### DUR-04

- Branch: `codex/dur-04-checkpoint-compaction`
- Commits:
  - `3bdb4cc feat(context): persist provider-neutral checkpoints`
  - `4123d70 feat(context): recover canonical compaction checkpoints`
  - `759a78a feat(runtime): switch models through neutral checkpoints`
  - `8cdedf7 test(runtime): recover compaction after worker restart`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `489 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `c1d4e6f8a203_add_context_checkpoints.py`
- Core delivery:
  - Provider-neutral Context Checkpoints persist the complete canonical resume state plus model-facing compilation, manifest, Working Memory, Provider State, retained Message, and retained Tool Result references.
  - The 55%, 70%, 82%, and 90% usage thresholds map to Tool Preview cleanup, Conversation Summary, Canonical Checkpoint, and Emergency Compaction stages.
  - Compaction permanently retains the authoritative Transcript, marks superseded messages with their checkpoint, and excludes only those marked messages from later model-facing Context compilation.
  - Temporal generates one deterministic checkpoint ID per compaction request; Activity retries repair a checkpoint-written/message-marker-missing crash window without deleting state or duplicating the checkpoint.
  - Model switching preserves the previous Provider State ID in a soft-reference checkpoint, changes the Session and Run model profile, clears provider-native resume state, recompiles and persists Context for the target profile, then continues the same Run.
  - `POST /api/v1/runs/{run_id}/model` and `riftx run model` expose durable model switching through the existing Workflow control boundary.
- Required scenarios:
  - Explicit required-context overflow, expired Provider State recovery, recovery after compaction, model switching, Worker shutdown during compaction, Pending Approval retention, and active Execution/Terminal retention are covered by executable tests.
- Wave D gate:
  - Tool execution Worker restart, approval and user-input restart recovery, in-flight compaction Worker replacement, PTY Runner restart to `LOST`, and same-Run model switching all pass in the full suite.
- Known limitations:
  - Real ConPTY and PowerShell smoke tests remain host-dependent and were skipped on this macOS host; all portable and mocked recovery tests passed.
- Next dependency: MEM-01 is unblocked.

### MEM-01

- Branch: `codex/mem-01-long-term-memory`
- Commits:
  - `7924ffc feat(memory): persist scoped long-term records`
  - `e7cdab1 feat(context): retrieve scoped long-term memory`
  - `e9bc765 feat(memory): expose manual management controls`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `497 passed, 2 skipped`; Ruff passed; diff check clean.
- Migration: `e2f5a7c9d104_add_long_term_memories.py`
- Core delivery:
  - Added Instruction, User Preference, Procedural, Semantic, and Episodic Memory records with explicit USER, NODE, WORKSPACE, RUN, ENGAGEMENT, ASSET, TOOL, or SKILL Scope.
  - Every Memory requires source references and persists content, summary, confidence, importance, validity window, supersede relationship, lifecycle status, and Pin state.
  - The SQLAlchemy Memory Store supports manual create, edit, soft-delete, Pin/unpin, atomic same-Scope supersede, TTL filtering, and auditable inactive records.
  - Deterministic retrieval applies Scope filtering before keyword ranking, excludes expired/deleted/superseded records, prioritizes in-Scope pinned records, and degrades to an empty result if retrieval storage fails.
  - Retrieved Memory is a first-class Context layer; selected IDs are recorded in Context Compilation manifests and production Workers load only relevant Scope values from the current Run contract.
  - Control-plane API and `riftx memory` CLI commands expose manual lifecycle operations, exact-Scope inspection, and source visibility.
- Required scenarios:
  - Cross-Engagement isolation, TTL, Supersede, deletion, Pin, missing-source rejection, keyword retrieval, Context injection, and retrieval failure degradation are covered by executable tests.
- Known limitations:
  - Embedding retrieval, automatic candidates, promotion policy, deduplication, and conflict resolution are intentionally deferred to MEM-02.
  - Asset Scope IDs use the deterministic `engagement_id::asset` form when derived from a Run contract, preventing same-address assets from crossing Engagement boundaries.
- Next dependency: MEM-02 is unblocked.

### MEM-02

- Branch: `codex/mem-02-memory-promotion`
- Commits:
  - `0f61685 feat(memory): gate candidate promotion`
  - `e5540be feat(memory): derive candidates from trusted results`
  - `c3bf219 feat(memory): promote confirmed findings`
  - `4dbcf93 fix(memory): reject sensitive promotion inputs`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `git diff --check`
  - Result: `513 passed, 2 skipped`; Ruff passed; diff check clean.
- Core delivery:
  - Added typed Memory Candidates with explicit origin, confidence, source references, suggested Scope and type, validity, retrieval hints, and conflict identity.
  - Promotion Policy permits deterministic Parser facts, independent multi-source confirmation, explicit user requests, confirmed Findings, and stable Tool/Node information while keeping single model guesses, unverified vulnerabilities, and arbitrary web content candidate-only.
  - Sensitive-content inspection rejects Cookie, authorization, API key, Token, Bearer credential, and common temporary signed-URL forms across candidate content and metadata before any durable write.
  - Deduplication merges canonical duplicates and their evidence, while conflict resolution ignores lower-confidence observations or atomically supersedes the active same-Scope fact with an equal-or-higher-confidence replacement.
  - Runtime adapters derive candidates from Working Memory facts, Findings, explicit user requests, and stable Node information without erasing source trust.
  - Confirmed Findings are promoted in both API and Temporal Worker control planes; drafts never auto-promote, and promotion failures are recorded without rolling back the authoritative Finding.
- Required scenarios:
  - Allowed and denied origins, sensitive inputs, insufficient multi-source evidence, canonical duplicate merge, confidence-based supersede, draft-versus-confirmed Finding behavior, source retention, and production control-plane wiring are covered by executable tests.
- Known limitations:
  - Engagement Fact and Attack Graph persistence remain intentionally deferred to EXT-02.
  - Candidate decisions are returned to the invoking Runtime component; a separate candidate-review inbox is not part of the MEM-02 contract.
- Next dependency: MEM-03 is unblocked.

### MEM-03

- Branch: `codex/mem-03-subagents-hooks`
- Commits:
  - `de4c912 feat(subagents): define isolated result contracts`
  - `4a7268f feat(subagents): schedule isolated sessions`
  - `d69f01b feat(subagents): merge validated result packets`
  - `e977ab8 feat(hooks): add audited runtime hook bus`
  - `f639f47 feat(runtime): invoke lifecycle hooks`
  - `73b29c9 feat(subagents): run bounded parallel delegations`
  - `186e654 feat(context): isolate subagent compilations`
  - `5df799a feat(runtime): execute model delegation batches`
  - `9212e0a test(subagents): cover Wave E continuation gate`
  - `8dc001a feat(subagents): wire durable worker execution`
  - `0692447 feat(hooks): cover memory and terminal lifecycle`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check .`
  - `git diff --check`
  - Result: `533 passed, 2 skipped`; Ruff passed; diff check clean.
- Core delivery:
  - Added validated Delegation and Result Packet contracts, independent child Agent Sessions, child-only Transcripts, selected Fact/Memory Context inputs, scoped Tool allowlists, and authoritative Primary Reducer merging.
  - Enforced depth one, four concurrent Subagents per Run, twenty total Subagents per Run, per-delegation model/tool/time budgets, and idempotent child/parent Result Packet persistence.
  - Primary Context receives only the allowlisted Summary, Fact Candidates, Hypothesis Updates, Finding Candidates, Evidence Refs, and Recommended Actions; child tool traffic and full Transcript remain private.
  - The production Worker exposes the resident `delegate` tool, executes model-requested batches through bounded child Runtime cycles, waits for deferred child executions, validates final structured results, and resumes the Primary cycle without letting child cycles acquire the Primary Run lease or complete its lifecycle.
  - Added an audited Hook Bus with deterministic priority resolution, bounded timeouts, fail-open/fail-closed policy, payload modification, approval escalation, additional Context, and emitted events.
  - Python, Command, and HTTP Hook adapters cover Context Compile, Model Call, Tool Execution, Approval, Subagent, Memory, and Terminal lifecycle points in the Worker and control plane.
- Wave E gate:
  - One Primary launches three parallel Subagents; each writes an independent Tool Result, returns a Result Packet, merges through the Primary reducer, survives Context Compaction, and allows the same Run to continue.
- Known limitations:
  - Subagents deliberately return a partial Result Packet instead of opening nested Approval or User Input waits; depth remains fixed at one by contract.
  - Real external model and security-tool execution depends on operator-supplied provider credentials and installed tools; the production dependency graph and deterministic test engines/runners are verified locally without secrets.
- Next dependency: EXT-01 is unblocked.

### EXT-01

- Branch: `codex/ext-01-mcp-governance`
- Commit: `37e7040 feat(mcp): govern concurrency and failures`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check .`
  - `git diff --check`
  - Result: `535 passed, 2 skipped`; Ruff passed; diff check clean.
- Core delivery:
  - Added an external MCP Adapter boundary with a global semaphore and lazily allocated per-server semaphores; defaults are sixteen total calls and two calls per server.
  - Consecutive adapter failures open a per-server circuit after the configured threshold, reject calls during cooldown with a typed retry delay, and admit exactly one Half-open Probe after cooldown.
  - A successful call or probe closes the circuit and resets its failure count; a failed probe reopens it and starts a fresh cooldown. Cancellation is not counted as an upstream failure and cannot strand the probe slot.
  - Health snapshots expose global and per-server active calls, completed and failed counts, circuit state, failure count, cooldown remaining, and Half-open Probe occupancy.
  - Runtime YAML and environment overrides expose all concurrency, threshold, and cooldown settings using the task-pack defaults.
- Isolation boundary:
  - Governance is implemented only by `GovernedMCPAdapter`; existing Process, Shell, PTY, target HTTP, Execution Service, and Runner paths are unchanged.
- Verification note:
  - The sandboxed full run passed all non-PTY tests but could not signal one PTY process group during cleanup. The targeted PTY/MCP tests and complete suite passed outside that sandbox under the approved `conda` test command.
- Next dependency: EXT-02 is unblocked.

### EXT-02

- Branch: `codex/ext-02-attack-graph`
- Commits:
  - `f9795cf feat(facts): persist promoted engagement graph`
  - `ba8dd54 test(facts): require confirmation for inference`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m pytest tests/runtime/test_terminal_runtime.py -q`
  - `conda run --no-capture-output -n agent python -m ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: all `537` portable tests pass when the existing PTY cleanup test is run separately; `2` host-dependent tests skipped; Ruff passed; Alembic has one head `f3a6b8c1d204`; diff check clean.
- Migration: `f3a6b8c1d204_add_engagement_facts.py`
- Core delivery:
  - Added typed Fact Promotion Candidates, Engagement Facts, Fact Relations, and Engagement-isolated Attack Graphs.
  - Promotion Candidates retain evidence references, source Run and Session, source Executions, Artifacts, confidence, validity, and the originating Working Memory Fact identity.
  - Rule promotion accepts deterministic Parser or user-decision evidence; pure model inference is rejected unless explicitly user-confirmed, so a model cannot directly create a durable Engagement Fact.
  - Matching facts merge independent evidence and provenance; conflicting lower-confidence rule candidates are rejected, while stronger or user-confirmed candidates atomically supersede the prior active Fact without deleting its history.
  - Attack Graph edges support `discovered_on`, `exploits`, `enables`, `depends_on`, and `leads_to`, validate both endpoints are in the same Engagement, retain their evidence/provenance, and expose deterministic successor traversal.
- Verification note:
  - During the monolithic suite the host intermittently denied `SIGTERM` to the existing PTY test process group after all assertions. The same PTY test passes in isolation, and every other test passes in the combined run; this is an execution-environment cleanup limitation, not an EXT-02 code failure.
- Next dependency: WEB-01 is unblocked.

### WEB-01

- Branch: `codex/web-01-source-fetch`
- Commit: `c9c837d feat(web): add canonical public source fetch`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: `554 passed, 2 skipped`; Ruff passed; Alembic has one head `a7d9e1f3c205`; diff check clean.
- Migration: `a7d9e1f3c205_add_web_source_registry.py`
- Core delivery:
  - Added durable `WebDocument`, ordered `WebDocumentChunk`, canonical `SourceReference`, and validated `EvidenceSpan` contracts. A formal Source is created only after a successful Fetch and normalization; redirect and Browser Fallback results cannot carry one.
  - Added anonymous HTTPX streaming Fetch with a decoded response-size cap, timeout, URL normalization, tracking-parameter removal, public DNS/IP validation before both cache access and each request, credential-header rejection, and redirect revalidation.
  - Same-origin redirects follow by default; cross-origin redirects return an explicit typed result, while opt-in cross-origin following still rechecks the new destination against the public-network boundary.
  - Added raw and normalized immutable Run Artifacts, persisted Run-scoped cache records, and extraction for HTML, Markdown, text, JSON, XML, PDF text layers, unknown encodings, binary-only content, and JavaScript-shell Browser handoff.
  - HTML normalization retains heading structure and page metadata while removing script, style, navigation, and footer content. Normalized documents are split into token-counted overlapping chunks with stable offsets and heading paths.
  - Added `pypdf` as a declared runtime dependency for PDF text-layer extraction.
- Verification coverage:
  - Static and large HTML, Markdown, text, JSON, XML, PDF, same-origin and cross-origin redirects, JavaScript Shell, malformed charset fallback, response truncation, cache hits, durable registry round trips, and literal/DNS private-address rejection.
- Environment note:
  - The existing `agent` environment still has `rich 13.9.4` while the project declares `rich>=14`; this pre-existing environment drift does not affect the passing WEB-01 or full-suite verification.
- Next dependency: WEB-02 is unblocked.

### WEB-02

- Branch: `codex/web-02-research-pipeline`
- Commits:
  - `d7cb2c2 feat(web): add pluggable search providers`
  - `7190e7e feat(web): build durable research pipeline`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: `568 passed, 2 skipped`; Ruff passed; Alembic has one head `b8e0f2a4d306`; diff check clean.
- Migration: `b8e0f2a4d306_add_web_research_pipeline.py`
- Core delivery:
  - Added a provider-neutral `SearchProvider` contract and normalized discovery-only Search Requests, Responses, and Results. Search candidates remain separate from the Source Registry and cannot be cited until WEB-01 successfully fetches them.
  - Added OpenAI Responses hosted Web Search and SearXNG JSON adapters with uniform domain filtering, URL normalization, duplicate removal, timestamps, Unicode handling, and typed retryability for timeout, rate-limit, transport, and upstream failures.
  - Added a bounded deterministic Query Planner, concurrent two-to-four-query execution, cross-query URL deduplication, relevance/authority/freshness ranking, domain-diverse selection, and concurrent Canonical Fetch of at most six sources.
  - Added question-focused extraction that selects relevant Document Chunks without inventing facts and retains exact Source, Chunk, heading, quote, and offset evidence.
  - Added bounded cross-source Research Packets and Web Context Packs. Primary Context receives only summaries, cited Claims, IDs, and unresolved questions under a 6000-token budget; complete search result sets and page bodies remain outside Primary Context.
  - Every Research Note, Packet, and Context Pack carries the immutable `UNTRUSTED_EXTERNAL_CONTENT` identity, so prompt-like text from a webpage remains external data rather than Runtime instructions.
  - Added durable Search Query/Result audit rows, Research Notes, and Research Packets while preserving the database boundary between discovery candidates and canonical Sources.
- Contract coverage:
  - Ordinary/empty search, domain allow/block filters, duplicate URLs, timestamps, Unicode, timeout, 429/5xx, OpenAI citation normalization, multi-query planning, source diversity, failed-query recovery, canonical Fetch gating, Evidence offsets, prompt-injection identity, Context budget, migration, and durable round trips.
- Next dependency: WEB-03 is unblocked.

### WEB-03

- Branch: `codex/web-03-target-http`
- Commits:
  - `c0b53a1 feat(web): execute scoped target HTTP on runner`
  - `6f45670 feat(runner): route target HTTP to remote nodes`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent pytest tests/target_http tests/runner/test_remote_control.py tests/integration/api/test_control_plane.py -q`
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent pytest tests/runtime/test_terminal_runtime.py::test_runtime_opens_one_durable_pty_and_yields_terminal_open -q`
  - `conda run --no-capture-output -n agent ruff check src tests migrations`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: WEB-03 targeted coverage `21 passed`; related Target HTTP, Runner, and API integration coverage `45 passed`; monolithic suite `585 passed, 2 skipped` plus the sole environment-denied PTY cleanup test `1 passed` in isolation; Ruff passed; Alembic has one head `c9f1a3b5e407`; diff check clean.
- Migration: `c9f1a3b5e407_add_target_http_requests.py`
- Core delivery:
  - Added a typed Target HTTP request/result contract for method, URL, headers, query, text or binary body, JSON body, cookies, proxy, TLS verification, Runner-local client-certificate references, redirect policy, timeout, and bounded response capture.
  - Target requests execute with the selected Runner host network and inherit its DNS, VPN, `/etc/hosts`, proxy environment, and local certificate access. Every initial request and redirect is rechecked against the immutable Run Scope before network I/O.
  - Added deterministic execution keys and request fingerprints, durable request state, ToolCallIntent readiness checks, idempotent replay, and structured Run events. Raw request and response bodies are registered as immutable Run Artifacts according to the request save policy.
  - Added a local/remote node router. Independently deployed Runners advertise `target_http`, receive durable authenticated commands, reconstruct binary-safe requests, perform the exchange locally, and upload bounded response bytes with exact-offset chunking and reconnect-safe resume behavior.
  - Control Plane command output is node-, command-kind-, lease-, chunk-, and declared-size-scoped. Structured completion metadata remains under the existing command-result cap, and the Control Plane verifies returned execution identity before accepting the response.
  - Public Fetch remains a separate anonymous public-network path with private/local destination rejection. Scope-authorized targets, host-network proxy use, and client certificates are available only through Target HTTP.
- Contract coverage:
  - Structured request construction, host proxy/TLS inheritance, client-certificate resolution, binary transport, private-IP Scope authorization, redirect reauthorization, bounded/truncated responses, request/response Artifacts, replay/conflict behavior, remote node routing, daemon chunk upload, exact-offset replay rejection, declared response caps, and durable API round trips.
- Known limitation:
  - The monolithic sandbox run consistently denies `SIGTERM` to one pre-existing PTY process group after its assertions. That test passes in isolation, and all WEB-03 plus all other combined tests pass; this is an execution-environment cleanup limitation rather than a Target HTTP regression.
- Next dependency: WEB-04 is unblocked.

### WEB-04

- Branch: `codex/web-04-managed-browser`
- Commit: `b48c158 feat(web): add managed browser runtime`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: `597 passed, 2 skipped`; Ruff passed; Alembic has one head `d1a4c7e9b205`; diff check clean.
- Migration: `d1a4c7e9b205_add_managed_browser_runtime.py`
- Core delivery:
  - Added durable Browser Session, Page, bounded Observation, stable Interactive Element, Form, Network Summary, Action, and Takeover Summary contracts with explicit `AGENT`, `USER`, and `SHARED_READ_ONLY` ownership.
  - Added Runner-owned Playwright Chromium execution for ephemeral contexts, Runner-local persistent profiles, and Chromium CDP attachment. Local and remote nodes use the same Browser command boundary; remote screenshots and downloads use bounded exact-offset command output.
  - Added structured observations containing URL, title, bounded visible text, headings, stable element references, form metadata without field values, console/alert summaries, recent network summaries, and Artifact references instead of the complete DOM.
  - Browser actions enforce latest-observation versions, stable refs, deterministic idempotency keys, Scope checks before navigation/click, disabled-element checks, durable lifecycle state, and deterministic rejection while the User owns the browser. Out-of-Scope pages reached during takeover are redacted before persistence or Agent/API return.
  - Screenshots, network summaries, Agent downloads, and downloads captured during user takeover are registered as immutable Run Artifacts. Multiple takeover downloads are bundled into one immutable ZIP Artifact.
  - Added user takeover/release tracking for URL transitions, newly opened pages, deduplicated network activity, downloads, and hashed login/storage-state change detection without exposing typed field values. Release persists a `BrowserTakeoverSummary`.
  - Added REST endpoints for open/get/close/observe/action/takeover/release and a WebSocket observation/control stream, plus bounded Agent tool contracts for `open_browser`, `observe_browser`, `act_browser`, `takeover_browser`, `release_browser`, and `close_browser`. Runner-local profile paths and CDP endpoints are excluded from API and Agent tool results.
  - Added Playwright as the browser engine dependency and documented the per-Runner Chromium installation command. Design/task documents remain local-only under the repository's existing `.gitignore` rules.
- Contract coverage:
  - Domain mode validation, bounded observations, Scope enforcement and takeover redaction, stale-version rejection, stable-ref actions, action idempotency, ownership blocking, takeover summaries, user downloads, Artifact persistence, SQL round trips, migration upgrade/downgrade, API secret redaction, Agent tool secret redaction, and remote Runner attachment upload.
- Known limitations:
  - A real Chromium smoke test requires `playwright install chromium` on the target Runner and was not executed in the current dependency-only test environment; the Playwright adapter is covered through an injectable fake engine.
  - A Runner process restart cannot reattach an in-memory managed context. Durable records and persistent profile data remain available, but an active session must currently be reopened (or reattached through CDP).
  - CDP attachment is Chromium-only and inherits Playwright's lower capability guarantees compared with a native Playwright connection. CAPTCHA solving and automated credential entry remain intentionally out of scope.
- Next dependency: WEB-05 is unblocked.

### WEB-05

- Branch: `codex/web-05-connectors`
- Commit: `91123be feat(connectors): ingest browser and Burp traffic`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `pnpm --filter @riftx/web test`
  - `pnpm --filter @riftx/web build`
  - `pnpm --filter @riftx/browser-extension test`
  - `pnpm --filter @riftx/browser-extension build`
  - `apps/burp-extension/scripts/test-core.sh`
  - Official Montoya API source compilation with `javac`.
  - `git diff --check`
  - Result: `604 passed, 2 skipped`; Ruff passed; Web `20 passed` and production build passed; Browser extension `2 passed` and production build passed; Burp core test and official Montoya API compilation passed; Alembic has one head `e4b7c1d9a305`; diff check clean.
- Migration: `e4b7c1d9a305_add_connector_submissions.py`
- Core delivery:
  - Added a unified Connector API for complete HTTP capture submission, existing/new Run selection, Run event subscription, cancellation, and WebUI routing. Connectors remain ingress clients and do not host an independent Agent Runtime.
  - Added durable, content-fingerprinted idempotency on `(source, capture_id)`, exact-one Run-target validation, derived host Scope for newly created Runs, and Scope reauthorization immediately before immutable Artifact creation.
  - Preserved Browser structured request/response bodies and exact Burp raw request/response bytes as immutable Run Artifacts. Durable submission rows retain sanitized metadata and Artifact IDs, while the model-facing manifest is explicitly marked `UNTRUSTED_EXTERNAL_CONTENT`.
  - Added a Manifest V3 Chrome DevTools extension that captures XHR/Fetch HAR entries, lets users select captures and existing/new Runs, follows SSE progress, cancels Runs, and opens the WebUI.
  - Added a Burp Montoya extension with context-menu submission, existing/new Run selection, SSE progress, cancellation, and WebUI controls. Its production source compiles against `net.portswigger.burp.extensions:montoya-api:2025.2`.
- Contract coverage:
  - Existing/new Run targeting, Scope derivation and rejection, startup-failure ingestion fallback, immutable request/response/manifest Artifacts, raw-byte preservation, durable replay/conflict behavior, sanitized persistence, Agent notification, CORS for Chrome extensions, redirect-aware SSE, browser capture selection, Burp raw HTTP parsing, and no embedded Agent Runtime.
- Known limitations:
  - Gradle is not installed locally; Burp production source was nevertheless compiled directly against the official Montoya API JAR and the dependency-free core test passed.
  - The Browser extension is covered by HAR/client unit tests and a production build, but was not exercised in an interactive Chrome DevTools smoke session.
  - The Burp UI was API-compiled and core-tested, but was not loaded into a live Burp installation.
- Next dependency: QA-01 is unblocked.

### QA-01

- Branch: `codex/qa-01-long-horizon-recovery`
- Commits:
  - `3b8e63c test(qa): add long-horizon recovery gate`
  - `37b8335 test(qa): verify real runner restart recovery`
- Completed at: `2026-07-30`
- Tests:
  - `conda run --no-capture-output -n agent pytest -q tests/evaluation`
  - `conda run --no-capture-output -n agent pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent alembic heads`
  - `git diff --check`
  - Result: QA-01 evaluation suite `6 passed`; full suite `610 passed, 2 skipped`; Ruff passed; Alembic has one head `e4b7c1d9a305`; diff check clean.
- Migration: None.
- Core delivery:
  - Added a machine-readable `LongHorizonEvaluator` with the fixed QA-01 workload contract, exact count validation, unique Tool/Execution identity checks, no-skipped-result checks, Objective/Scope/Working Memory preservation, Artifact traceability, complete recovery-boundary coverage, and bounded Temporal payload enforcement.
  - Added one durable SQLite-backed long-horizon Run covering exactly 100 Tool Calls, 10 expected Tool Failures, 5 user supplements, 3 approvals, 3 isolated Subagents, 2 canonical Compactions, 1 model switch, 1 control-plane Worker reconstruction, 1 Runner reconstruction, 20 Web Sources, and 1 Browser Takeover.
  - Added one-shot fault injection at all nine mandatory boundaries: after Context Compile, after Model Call, after ToolCallIntent persistence, after Execution start, after Execution completion before result processing, while waiting for Approval, during Compaction, during Subagent work, and during Browser Action.
  - Verified stable ToolCallIntent and Execution identities across retries, exactly one Runner launch per Tool Call, all success/failure results processed, exact Working Memory reload after database/service reconstruction, immutable Artifact integrity, and unchanged Objective and Scope.
  - Added a real Temporal time-skipping workflow test with 100 sequential Tool execution waits, two Compaction signals, one model switch, first-Worker teardown, second-Worker recovery, History replay, identifier-only activity payloads, and decoded History payloads below 64 KiB.
  - Added a real local `ProcessSupervisor` restart test that closes the Runner supervisor and database, reconstructs both from durable state, and verifies the completed Execution and exact output remain available.
- Contract coverage:
  - Long-horizon count gate, duplicate Execution rejection, skipped-result detection, all mandatory fault points, Approval resume, Compaction repair, Subagent recovery, Browser action idempotency, Worker restart, Runner restart, Temporal deterministic replay, bounded Temporal History, Web Source persistence, Browser Takeover, Working Memory consistency, Objective/Scope preservation, and Artifact traceability.
- Known limitations:
  - The 100-call workload uses a deterministic Runner adapter over the real durable repositories so the gate remains fast and provider/tool independent; a separate test exercises the real local `ProcessSupervisor` restart path.
  - Fault points use controlled one-shot exceptions at the precise durable boundaries rather than sending an operating-system `SIGKILL`; recovery reconstructs the relevant service/adapter graph and verifies persisted identity and state.
  - Browser recovery and takeover use the injectable browser engine rather than a live Chromium binary. The real Browser adapter remains covered by WEB-04's contract tests and requires Runner-side Chromium installation for an interactive smoke test.
- Next dependency: QA-02 is unblocked.

## Architecture Deviations

- The completed V2 domain already exposes a legacy `AgentStep`. The new durable cycle-scoped
  `AgentStep` lives under `riftx.runtime.types` to avoid breaking the V2 API contract.
- The repository uses flat persistence modules, so Runtime ORM records extend the existing
  `persistence/orm.py` metadata while Runtime mappers and repositories use dedicated modules.

## Blockers

- None.
