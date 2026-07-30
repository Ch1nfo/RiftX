# RiftX Post-V2 Implementation Progress

## Current Wave

Wave A complete; Wave B is unblocked by the recovery gate.

## Completed

- [x] RT-01 Runtime Domain 与状态机
- [x] RT-02 Agent Engine 抽象
- [x] RT-03 Runtime Coordinator 与有限 Cycle
- [x] RT-04 Transcript 与 Session Manager

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

## Architecture Deviations

- The completed V2 domain already exposes a legacy `AgentStep`. The new durable cycle-scoped
  `AgentStep` lives under `riftx.runtime.types` to avoid breaking the V2 API contract.
- The repository uses flat persistence modules, so Runtime ORM records extend the existing
  `persistence/orm.py` metadata while Runtime mappers and repositories use dedicated modules.

## Blockers

- None.
