# RiftX Post-V2 Implementation Progress

## Current Wave

Wave A

## Completed

- [x] RT-01 Runtime Domain 与状态机
- [ ] RT-02 Agent Engine 抽象
- [ ] RT-03 Runtime Coordinator 与有限 Cycle
- [ ] RT-04 Transcript 与 Session Manager

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

## Architecture Deviations

- The completed V2 domain already exposes a legacy `AgentStep`. The new durable cycle-scoped
  `AgentStep` lives under `riftx.runtime.types` to avoid breaking the V2 API contract.
- The repository uses flat persistence modules, so Runtime ORM records extend the existing
  `persistence/orm.py` metadata while Runtime mappers and repositories use dedicated modules.

## Blockers

- None.
