# RiftX 3.0 Code Audit Implementation Progress

> Status: active
>
> Started: 2026-08-02 (Asia/Shanghai)
>
> Product baseline: `496e260f3cb1f18ce485c5f706d20d8352d6a398`
>
> Authoritative specification: `docs/riftx-3-code-audit-development-spec.md`
>
> Specification baseline commit: `9a9b0e4d`
>
> Implementation branch: `ch1nfo/riftx-3-code-audit`

## Ledger Rules

- Task status is one of `pending`, `in_progress`, `blocked`, or `completed`.
- A task becomes `completed` only after its implementation and targeted checks pass.
- Every task is committed locally before the next task starts. The next ledger update
  backfills the preceding task's commit because a Git commit cannot contain its own hash.
- Test evidence records exact commands and results, not a cumulative test-count claim.
- Design deviations require an ADR and an update to the authoritative specification.
- Agent-related tests and runtime commands use the Conda `agent` environment.
- Unrelated worktree changes are never included in a Code Audit task commit.

## Current Wave

- Milestone: `M1 — Run kind, domain, and persistence`
- Current task: `AUD-103 — AuditApplicationService`
- Next dependency: `AUD-102` is complete; `AUD-103` is unblocked.

## Milestone Status

| Milestone | Status | Exit evidence |
| --- | --- | --- |
| M0 Contract and development guardrails | completed | AUD-000 through AUD-002, full test suite, independence boundary, and release gate passed |
| M1 Run kind, domain, and persistence | in_progress | AUD-100 through AUD-102 complete; AUD-103 is the current unblocked task |
| M2 Preflight, Snapshot, and Scope Ledger | pending | Not started |
| M3 Deterministic vertical slice | pending | Not started |
| M4 Typed Agent and Standard workflow | pending | Not started |
| M5 Evidence, Finding, Baseline, Closure, reports | pending | Not started |
| M6 API, CLI, and WebUI | pending | Not started |
| M7 Production isolation and dynamic validation | pending | Not started |
| M8 Diff and Deep | pending | Not started |
| M9 Fix, Retest, and lifecycle | pending | Not started |
| M10 Evaluation, hardening, and release | pending | Not started |

## Task Status

### M0

| Task | Status |
| --- | --- |
| AUD-000 Implementation progress ledger | completed |
| AUD-001 Independent implementation and naming boundary | completed |
| AUD-002 Configuration and feature flag | completed |

### M1

| Task | Status |
| --- | --- |
| AUD-100 RunKind | completed |
| AUD-101 Audit domain | completed |
| AUD-102 ORM and repositories | completed |
| AUD-103 AuditApplicationService | pending |
| AUD-104 API skeleton and policy | pending |
| AUD-105 Artifact access foundation | pending |
| AUD-106 RunKind workflow router | pending |

### M2

| Task | Status |
| --- | --- |
| AUD-200 Source root and Git preflight | pending |
| AUD-201 Signed preflight token | pending |
| AUD-202 Snapshot materializer | pending |
| AUD-203 Inventory and Scope | pending |
| AUD-204 Snapshot reader | pending |
| AUD-205 Snapshot Artifact and API | pending |
| AUD-206 Content Sandbox and safety stop | pending |
| AUD-207 Evaluation schema and harness | pending |
| AUD-208 StartIntent delivery skeleton | pending |

### M3

| Task | Status |
| --- | --- |
| AUD-300 Detector registry | pending |
| AUD-301 Detector runner | pending |
| AUD-302 Native baseline Detectors | pending |
| AUD-303 SARIF import | pending |
| AUD-304 Signal normalization | pending |
| AUD-305 Deterministic Evidence-to-Closure slice | pending |
| AUD-306 Deterministic Audit workflow | pending |
| AUD-307 Core seal and minimal reports | pending |

### M4

| Task | Status |
| --- | --- |
| AUD-400 Agent engine structured output | pending |
| AUD-401 Audit Agent contracts | pending |
| AUD-402 Safe code tools and model egress | pending |
| AUD-403 System Mapper | pending |
| AUD-404 Hunter and Skeptic | pending |
| AUD-405 Proof and Chain | pending |
| AUD-406 Agent-aware Reconcile, Risk, and Closure | pending |
| AUD-407 Model egress broker | pending |
| AUD-408 Standard workflow | pending |

### M5

| Task | Status |
| --- | --- |
| AUD-500 Evidence and Decision Ledger | pending |
| AUD-501 Reducer | pending |
| AUD-502 Risk Policy | pending |
| AUD-503 Finding identity | pending |
| AUD-504 Baseline comparison | pending |
| AUD-505 Closure Validator | pending |
| AUD-506 Audit reports and SARIF | pending |

### M6

| Task | Status |
| --- | --- |
| AUD-600 Complete API | pending |
| AUD-601 CLI | pending |
| AUD-602 Web types, client, and queries | pending |
| AUD-603 Layout and routing | pending |
| AUD-604 AuditsPage and NewAuditPage | pending |
| AUD-605 AuditDetailPage | pending |
| AUD-606 CodeFindingPage | pending |
| AUD-607 i18n, accessibility, and responsive UI | pending |
| AUD-608 Demo and README | pending |

### M7

| Task | Status |
| --- | --- |
| AUD-700 Sandbox backend | pending |
| AUD-701 Runner capability | pending |
| AUD-702 Validation plan and Approval | pending |
| AUD-703 Sandbox Capsule Evidence | pending |
| AUD-704 Failure and cancellation | pending |

### M8

| Task | Status |
| --- | --- |
| AUD-800 Diff Scope planner | pending |
| AUD-801 Diff classification | pending |
| AUD-802 Deep Child Workflow | pending |
| AUD-803 Saturation and Budget | pending |
| AUD-804 Deep/Diff UI and CLI | pending |

### M9

| Task | Status |
| --- | --- |
| AUD-900 Fix Advisor | pending |
| AUD-901 Isolated fix worktree | pending |
| AUD-902 Retest | pending |
| AUD-903 Lifecycle projection | pending |

### M10

| Task | Status |
| --- | --- |
| AUD-1000 Evaluation Corpus | pending |
| AUD-1001 Fault injection | pending |
| AUD-1002 Security testing | pending |
| AUD-1003 Independence, SBOM, and licensing | pending |
| AUD-1004 Release gate | pending |
| AUD-1005 Version and documentation | pending |

## Task Records

### AUD-000 — Implementation Progress Ledger

- Outcome: Created the authoritative M0-M10 task ledger and local-version-control
  conventions for RiftX Code Audit development.
- Files changed:
  - `docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md`
- Schema/migration impact: None.
- Security boundary impact: None; this task only records the implementation process.
- Tests run:
  - `git diff --check`
  - `git status --short`
- Test results: `git diff --check` passed with no output; `git status --short`
  listed only this new progress-ledger file before staging.
- Commit: `13d7e7f8` (`docs: start Code Audit implementation ledger`).
- Known limitations:
  - No product behavior is implemented by AUD-000.
- Next unblocked task: AUD-001.

### AUD-001 — Independent Implementation and Naming Boundary

- Outcome:
  - Accepted ADR-0001 for the RiftX Code Audit name, independent-reimplementation
    boundary, third-party reuse stop rule, and artifact provenance convention.
  - Added a deterministic boundary scanner for production sources, dependency/SBOM
    inputs, component inventory, and explicit build artifacts.
  - Wired the scanner contract and fail-closed canaries into the existing executable
    RiftX release qualification gate.
- Files changed:
  - `docs/architecture/decisions/0001-riftx-code-audit-independent-reimplementation.md`
  - `docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md`
  - `scripts/qa/code-audit-boundary-gate.py`
  - `scripts/qa/release-gate.py`
  - `src/riftx/evaluation/independence.py`
  - `src/riftx/evaluation/__init__.py`
  - `src/riftx/evaluation/release.py`
  - `tests/evaluation/test_independence_gate.py`
  - `tests/evaluation/test_release_gate.py`
- Schema/migration impact: None.
- Security boundary impact:
  - Adds a read-only qualification gate; it does not register an Audit endpoint,
    execute target code, contact a third-party service, or enable the feature.
  - Missing component inputs, unreadable trees, symlinks, special files, empty or
    corrupt artifacts, unsupported tar compression, unscanned tar link/PAX metadata,
    and policy matches fail closed.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/evaluation/test_independence_gate.py tests/evaluation/test_release_gate.py`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx/evaluation/independence.py src/riftx/evaluation/__init__.py src/riftx/evaluation/release.py scripts/qa/code-audit-boundary-gate.py scripts/qa/release-gate.py tests/evaluation/test_independence_gate.py tests/evaluation/test_release_gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py --require-artifact --artifact apps/web/dist --artifact apps/browser-extension/dist --artifact apps/demo/dist`
  - `git diff --check`
- Test results:
  - Targeted Pytest passed: `80 passed`.
  - Targeted Ruff passed with `All checks passed!`.
  - Repository gate passed under policy `riftx.code-audit-independence/v1`, digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`:
    9 dependency manifests, 445 production files, 0 explicit artifacts, 0 violations.
  - Explicit local bundle gate passed under the same digest: 9 dependency manifests,
    445 production files, 46 artifact files, 0 violations.
  - `git diff --check` passed with no output.
- Provenance:
  - Requirements source: authoritative specification sections 2.4, CA-INV-001,
    and section 22 / AUD-001.
  - Architecture author: Codex task `m0_docs_map`; implementation and synthetic
    fixture author: Codex task `m0_ci_map`; Git author: Ch1nfo.
  - Independent implementation review: Codex task `m0_config_map`; tar-metadata
    blocker re-review: Codex task `m0_docs_map`; final review: Codex task `/root`;
    result: approved after all P1 findings were closed.
  - Third-party expressive material: none. Public methodology was previously studied,
    so this is not represented as a strict clean-room process.
  - Production Code Audit Agent instructions: not applicable for AUD-001.
- Commit: `65298283` (`feat(qa): enforce Code Audit independence boundary`).
- Known limitations:
  - The scanner proves only that the versioned, bounded policy found no known forbidden
    identities in the inspected inputs; it is not a copyright or clean-room proof.
  - The three local bundle directories were not rebuilt from a final release candidate.
    A frozen build matrix, complete SBOM/license inventory, and candidate artifact scan
    remain mandatory in AUD-1003/AUD-1004.
  - The repository has no hosted CI attestation baseline; this CI-callable command is
    wired into the local executable release gate, while external signed evidence remains
    a release-pipeline concern.
- Next unblocked task: AUD-002.

### AUD-002 — Configuration and Feature Flag

- Outcome:
  - Added frozen `AuditConfig` deployment policy models with a default-disabled
    feature flag and explicit deny-all empty `source_roots` behavior.
  - Added complete `RIFTX_AUDIT_*` leaf mappings, strict scalar and JSON-list
    parsing, bounded numeric and enum validation, cross-field rules, normalized
    remote-model origins, and rejection of request/CLI Audit policy overrides.
  - Added a versioned Audit configuration digest that applies keyed HMAC to
    sensitive absolute paths before hashing the canonical policy document.
  - Enforced two-way realpath isolation between authorized source roots and Audit,
    workspace, Runner, credential, model-secret, principal, SQLite, and Temporal TLS
    storage. API and Worker startup revalidate before and after directory creation;
    Runner CLI post-load storage overrides are rejected when source roots are set.
  - Kept Audit-disabled startup side-effect free and documented the safe deployment
    defaults in the example configuration.
- Files changed:
  - `configs/riftx.example.yaml`
  - `docs/architecture/decisions/0001-riftx-code-audit-independent-reimplementation.md`
  - `docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md`
  - `src/riftx/api/runtime.py`
  - `src/riftx/cli/app.py`
  - `src/riftx/config.py`
  - `src/riftx/temporal/worker_runtime.py`
  - `tests/unit/cli/test_app.py`
  - `tests/unit/test_audit_config.py`
  - `tests/unit/test_runtime_config.py`
- Schema/migration impact: None.
- Security boundary impact:
  - Adds deployment-time authorization and storage-isolation policy only; it does
    not register Audit endpoints, read source content, execute target code, create
    snapshots, or invoke a model.
  - Empty source roots deny every repository. Enabling Audit requires sandboxed
    validation, and Deep mode requires the hybrid analysis profile.
  - Remote model origins are HTTPS origin-only values with strict DNS/IP parsing;
    unknown Audit environment keys, ambiguous values, broken symlinks, path aliases,
    and source/storage overlap fail closed without disclosing source paths.
  - Each shared startup check re-resolves source and storage paths. Pre/post-create
    validation closes the tested source-root and storage-root symlink replacement
    races at application assembly boundaries.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/test_audit_config.py tests/unit/test_runtime_config.py tests/unit/cli/test_app.py tests/unit/temporal/test_worker_runtime.py`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx/config.py src/riftx/api/runtime.py src/riftx/temporal/worker_runtime.py src/riftx/cli/app.py tests/unit/test_audit_config.py tests/unit/test_runtime_config.py tests/unit/cli/test_app.py tests/unit/temporal/test_worker_runtime.py`
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/config.py src/riftx/api/runtime.py src/riftx/cli/app.py tests/unit/test_audit_config.py`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Targeted Pytest passed: `204 passed`.
  - Targeted Ruff and repository Ruff passed with `All checks passed!`.
  - Targeted Mypy passed with no issues in the four selected files. The existing
    unrelated `worker_runtime.py` Mypy baseline was intentionally outside this
    targeted invocation.
  - The full Pytest suite passed; five platform/containment tests were skipped and
    one existing Pydantic warning was reported.
  - The independence boundary passed under policy
    `riftx.code-audit-independence/v1`, digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`:
    9 dependency manifests, 445 production files, 0 explicit artifacts, and 0
    violations.
  - The executable release gate reported every gate passed; `git diff --check`
    passed with no output.
- Provenance:
  - Requirements source: authoritative specification section 20 and section 22 /
    AUD-002.
  - Implementation and test author: Codex task `/root`; Git author: Ch1nfo.
  - Security review: Codex tasks `m0_ci_map` and `m0_docs_map`; result: approved
    after origin/TLS coverage, immutable policy, API/Worker/Runner override,
    nearest-parent, and source-root replacement findings were closed. The original
    adversarial canary matrix ended with `ALL_ORIGINAL_CANARIES_REJECTED`.
  - Third-party expressive material: none; implementation uses RiftX-owned models,
    validators, fixtures, and runtime boundaries.
  - Production Code Audit Agent instructions: not applicable for AUD-002.
- Commit: `be65b408` (`feat(config): define Code Audit safety policy`).
- Known limitations:
  - The flag remains admission-only. Audit domain objects, API admission, source
    preflight, signed authorization, snapshots, and deterministic analysis begin in
    M1-M3.
  - Startup isolation is one defense layer; descriptor-safe snapshot traversal and
    per-run source authorization remain mandatory in M2.
- Next unblocked task: AUD-100.

### AUD-100 — RunKind

- Outcome:
  - Added the stable `RunKind` values `general` and `code_audit`, exported them
    through the Domain API, and made `Run.kind` a required, field-frozen identity.
  - Updated every production and existing test `Run`/`RunRecord` construction point
    to set kind explicitly. Generic Run creation always writes `general`; the generic
    request schema continues to reject a client-supplied kind.
  - Added strict ORM/mapper persistence, a named database CHECK, an index, combined
    status/kind filtering, unknown-value rejection, and an immutable-kind guard on
    mutable Run updates.
  - Added Alembic revision `0d3a8b7c4e21`: legacy rows are backfilled through a
    migration-only `general` server default, the default is removed immediately, and
    SQLite batch rebuilds suspend FK enforcement only inside a verified autocommit
    boundary before running `foreign_key_check` and restoring enforcement.
  - Made no-kind Run lists default to `general`; generic Connector lists are pinned to
    `general`. Run responses and CLI/Web clients support explicit kind filtering, the
    Dashboard requests only `general`, and React Query caches include kind.
  - Preserved the existing Temporal Run Workflow input, workflow ID, signals, replay,
    and history contracts without adding a RunKind branch.
- Files changed:
  - Domain/persistence: `src/riftx/domain/{__init__,enums,run}.py`,
    `src/riftx/persistence/{orm,mappers,repositories}.py`,
    `src/riftx/application/ports/repositories.py`, and
    `src/riftx/application/services/runs.py`.
  - API/CLI: `src/riftx/api/routes/{runs,connectors}.py`,
    `src/riftx/api/schemas/runs.py`, and `src/riftx/cli/{app,client}.py`.
  - Migration: `migrations/versions/0d3a8b7c4e21_add_run_kind.py`.
  - Web: `apps/web/src/api/{types,client}.ts`,
    `apps/web/src/hooks/queries.ts`, `apps/web/src/pages/DashboardPage.tsx`, and
    their Run client/Dashboard/Run-detail tests.
  - Primary Python regressions: `tests/unit/domain/test_models.py`,
    `tests/unit/persistence/{test_mappers,test_schema}.py`,
    `tests/integration/persistence/{test_migrations,test_run_kind_migration,test_repositories}.py`,
    `tests/integration/api/test_control_plane.py`, `tests/connectors/test_api.py`,
    and `tests/unit/cli/{test_app,test_client}.py`.
  - Existing Run fixtures across browser, connector, context, evaluation, execution,
    facts, hooks, Agent, API, application, persistence, Runner, Runtime, Subagent,
    Target HTTP, Temporal, and web tests were mechanically updated to declare
    `kind="general"`. The final AST inventory contains 93 `Run(...)` and 7
    `RunRecord(...)` calls; only the intentional required-field negative test omits
    kind.
  - Progress ledger: `docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md`.
- Schema/migration impact:
  - Adds `runs.kind VARCHAR(32) NOT NULL`, `ck_runs_kind`, and `ix_runs_kind` at the
    single Alembic head `0d3a8b7c4e21`; the final column has no database or Python
    default.
  - Existing rows upgrade to `general`. General-only downgrade preserves the Run FK
    graph; any `code_audit` or unknown kind makes downgrade fail before DDL. Offline
    SQLite batch migration and offline downgrade fail closed.
- Security boundary impact:
  - Run kind becomes a mandatory immutable security identity. Missing, whitespace,
    legacy `agent`/`audit`, and unknown values fail validation; persisted unknowns do
    not fall back to `general`.
  - Generic `POST /runs` cannot mint a `code_audit` Run. Existing list consumers see
    only `general` unless they explicitly request `code_audit`, preventing Audit IDs
    from entering the generic Dashboard or Connector UI.
  - The migration protects referenced Run children during SQLite parent-table rebuild
    and refuses a lossy downgrade that would erase the Audit classification.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/domain/test_models.py tests/unit/persistence/test_mappers.py tests/unit/persistence/test_schema.py tests/integration/persistence/test_repositories.py::test_repository_lists_and_filters_runs tests/connectors/test_api.py tests/unit/cli/test_app.py::test_run_list_forwards_status_and_kind_filters tests/unit/cli/test_client.py::test_api_client_combines_run_status_and_kind_filters`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_migrations.py::test_run_kind_migration_backfills_without_default_and_preserves_fk_graph tests/integration/persistence/test_migrations.py::test_run_kind_migration_rejects_lossy_code_audit_downgrade_before_ddl tests/integration/persistence/test_migrations.py::test_run_kind_offline_downgrade_fails_closed`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_repositories.py tests/integration/persistence/test_migrations.py tests/unit/persistence/test_mappers.py tests/unit/persistence/test_schema.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/api/test_control_plane.py::test_run_crud_control_and_message_timeline tests/connectors/test_api.py tests/unit/cli/test_app.py tests/unit/cli/test_client.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/temporal/test_runtime_client.py tests/unit/temporal/test_workflow.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_run_kind_migration.py`
  - `conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck`
  - `conda run --no-capture-output -n agent pnpm --filter @riftx/web test`
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/domain/enums.py src/riftx/domain/run.py src/riftx/persistence/orm.py src/riftx/persistence/mappers.py src/riftx/application/ports/repositories.py src/riftx/application/services/runs.py src/riftx/api/schemas/runs.py`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Core Domain/mapper/schema/filter tests passed: `45 passed`; the extended
    persistence and migration matrix passed: `66 passed`.
  - API/Connector/CLI matrix passed: `72 passed`; unchanged Temporal client/workflow
    replay matrix passed: `39 passed`.
  - PostgreSQL offline upgrade SQL and SQLite offline rejection tests passed; Alembic
    reports one head, `0d3a8b7c4e21`.
  - Web TypeScript typecheck passed; the complete Web test suite passed in 20 files.
  - Targeted Mypy passed with no issues in the seven selected files. Repository,
    route, and CLI modules retain unrelated pre-existing full-module Mypy baseline
    errors and were covered by Ruff plus runtime tests.
  - The full Python suite passed on the clean rerun. An initial run hit the existing
    POSIX process-group marker timing flake; its unchanged selector then passed three
    consecutive reruns before the clean full rerun.
  - Repository Ruff, independence boundary, executable release gate, and
    `git diff --check` passed. The boundary used policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`:
    9 dependency manifests, 446 production files, 0 explicit artifacts, and 0
    violations.
- Provenance:
  - Requirements source: authoritative specification sections 4.3, 13.1, and
    section 22 / AUD-100.
  - Implementation, migration, and test author: Codex task `/root`; Git author:
    Ch1nfo.
  - Security and specification review: Codex tasks `m0_ci_map`, `m0_docs_map`, and
    `aud100_test_design`; final result: approved after the SQLite FK/data-loss P0 and
    immutable-kind/lossy-downgrade P1 findings were closed.
  - Third-party expressive material: none; the implementation and fixtures are
    RiftX-owned and use SQLAlchemy, Alembic, FastAPI, SQLite, and TypeScript contracts.
  - Production Code Audit Agent instructions: not applicable for AUD-100.
- Commit: `2b052fc7` (`feat(domain): add immutable Run kinds`).
- Known limitations:
  - `code_audit` Run creation remains internal-only until AUD-101-AUD-103 add the
    Audit domain, persistence UoW, and admission service.
  - Mutation/effect policy, Run Workflow routing, and kind-aware reconciliation are
    intentionally deferred to AUD-106; no Audit Run may enter the generic Workflow.
  - SQLite batch migration requires a maintenance window and old/new API writers must
    not overlap. PostgreSQL offline SQL is compile-checked, but RiftX does not claim
    PostgreSQL runtime support without a real database CI matrix and lock analysis.
- Next unblocked task: AUD-102.

### AUD-101 — Audit Domain

- Status: completed.
- Scope delivered:
  - Added `src/riftx/domain/audit.py` with strict, frozen, infrastructure-independent
    Code Audit value objects and the `AuditScan` aggregate.
  - Added `src/riftx/domain/code_finding.py` with only the §6.3 Candidate wire enum
    and pure transition allowlist; Finding identity, occurrence, evidence, reducer,
    triage, and baseline semantics remain deferred to M5.
  - Exported the new public domain contract from `src/riftx/domain/__init__.py`.
  - Added the exhaustive AUD-101 test matrix in
    `tests/unit/domain/test_audit_domain.py`.
  - Updated the authoritative specification with the v1 wire schemas, hard bounds,
    lifecycle/cleanup proof, capability binding, egress disclosure, and early
    no-Snapshot decisions frozen by this task.
- Frozen v1 contract decisions:
  - SourceTargetKind is `revision | working_tree`; Diff exists only as AuditMode and
    atomically binds distinct base/head identities. POSIX, Windows drive, and UNC
    paths use one fail-closed canonical syntax.
  - AuditContract is `riftx.audit-contract/v1`, bounded to 256 KiB canonical UTF-8
    JSON. Policy documents are bounded to 64 KiB; CapabilityMatrix is bounded to
    512 rows. Duplicate keys, non-canonical bytes, excessive depth/node/key/string
    shapes, unknown fields, and redundant-column/digest mismatch are rejected.
  - AuditBudget v1 freezes twelve bounded dimensions. Static/dynamic and
    deterministic/hybrid policy-budget combinations are cross-validated.
  - Capability missing outcomes are separate Start/runtime enums. Global required
    and scoped rows cannot downgrade or replace execution bindings. Source and
    analysis backend prepare proofs, Detector/parser components, hybrid execution,
    and selected dynamic validation all bind the frozen execution selection.
  - Model egress uses strict local/remote locality, canonical origin digests, typed
    `riftx.model-retention-training-disclosure/v1`, bounded scope/byte policy, and a
    deterministic consent-requirement digest. Arbitrary or unknown disclosure
    schemas fail closed.
  - `terminal_outcome` survives cleanup and publication. Analysis-phase failure or
    cancellation still requires Start/Snapshot facts. Closure/core seal require an
    immutable cleanup proof plus a matching terminal Run status. Early pre-analysis
    failed/cancelled partial-facts publication may intentionally have no Snapshot.
  - Terminal publication retry changes only seal/report/package state; it never
    reopens analysis or Run lifecycle, rewrites Closure/core facts, or pre-fills a
    distribution revision before atomic publication.
- Schema/migration impact:
  - None. AUD-101 defines the canonical domain/wire contract only. AUD-102 owns the
    ORM, migration, mapper, CAS, repository, and corrupt-row recovery implementation.
- Security boundary impact:
  - Models use strict Pydantic input, `extra=forbid`, timezone-aware timestamps,
    immutable tuples/models, and reject the unvalidated `model_copy(update=...)`
    escape hatch. The deprecated Pydantic `copy()` API is disabled entirely because
    its update/include/exclude paths bypass model validation.
  - Aggregate binding boundaries revalidate even already-typed instances, so a caller
    cannot use Pydantic `model_construct()` to inject a forged AuditScan lifecycle or
    replace canonical contract JSON while retaining the original digests.
  - Contract, SourceTarget, Budget, CapabilityMatrix, versioned policy, model-egress
    policy, and consent requirement use distinct SHA-256 domain separators.
  - Lifecycle and phase are cross-validated on deserialization as well as command
    transitions; publication cannot project terminal before a success/failure fact.
  - Absolute source paths remain sensitive (`repr=False`) and are not exposed by
    validation errors. Runtime authorization/realpath remains a SourceIngest duty.
- Tests run:
  - `conda run --no-capture-output -n agent pytest -q tests/unit/domain/test_audit_domain.py`
  - `conda run --no-capture-output -n agent pytest -q tests/unit/domain`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent mypy src/riftx/domain/audit.py src/riftx/domain/code_finding.py tests/unit/domain/test_audit_domain.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - AUD-101 targeted: `517 passed`; complete domain suite: `565 passed`.
  - Full Python suite: `3241 passed, 5 skipped`.
  - Repository Ruff and targeted Mypy passed.
  - Independence boundary passed policy
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`:
    9 dependency manifests, 448 production files, 0 explicit artifacts, and 0
    violations.
  - Executable release gate reported `ready=true`; all 16 gates passed.
  - `git diff --check` passed.
- Provenance:
  - Requirements source: authoritative specification sections 6.1-6.6, 10.5,
    13.2, 16.2-16.3, 21.1, and section 22 / AUD-101.
  - Implementation and test author: Codex task `/root`; Git author: Ch1nfo.
  - Independent specification/security review: Codex task `m0_docs_map`; final
    result: approved after cleanup proof, analysis backend proof, analysis-phase
    Snapshot, typed disclosure, and consent-binding findings were closed.
  - Independent final adversarial review: Codex task `m0_ci_map`; final result:
    approved with no remaining P0/P1/P2 after deprecated `copy()` and forged
    `model_construct()` paths were closed and regression-tested.
  - Third-party expressive material: none. The implementation is RiftX-owned and
    uses only project Pydantic/domain conventions and the independently defined
    RiftX specification.
  - Production Code Audit Agent instructions: not applicable; AUD-101 contains no
    prompt, provider, Agent workflow, or external scanner implementation.
- Commit: `9b5f435f` (`feat(domain): define Code Audit contracts`).
- Known limitations / next contracts:
  - AUD-102 must persist every canonical/redundant field and reject corrupt rows;
    AUD-103 must verify actual Start consent, reviewed contract digest, and live
    prepare/capability proofs.
  - Detector/parser per-Scope applicability completeness is enforced later by
    Inventory and Closure over the frozen matrix/scope policy.
  - Published N+1 DistributionRevision intent/rebuild belongs to AUD-506; AUD-101
    implements the initial publication and failed-publication retry projection.
- Next unblocked task: AUD-102.

### AUD-102 — ORM and Repositories

- Status: completed.
- Outcome:
  - Added the eight minimum Code Audit tables, strict domain records/mappers,
    versioned Repository Ports, SQLAlchemy repositories, and caller-owned
    transaction primitives required to recover an Audit after restart.
  - Added immutable SourceSnapshot identity/seal validation, canonical Contract
    byte/digest revalidation, Project/Engagement and Audit/Run/Node/Snapshot owner
    binding, bounded pagination, stable ordering, natural-key replay envelopes,
    monotonic CAS, lease/reclaim rules, terminal protection, and the temporary
    AUD-506 publication fence.
  - Added atomic Contract+Scan creation plus session-bound Project, StartIntent,
    Contract, Scan, and Run-convergence primitives for AUD-103/state-projector UoWs;
    the auto-commit Audit Repository cannot claim terminal Run convergence.
  - Treats the database as untrusted on every read. Orphan/corrupt rows, hostile IDs,
    mismatched redundant fields, cross-owner references, missing/cross-Run Phase
    outputs, and invalid Coverage Plan bindings fail closed with redacted errors.
  - Serialized SQLite candidate writes with `BEGIN IMMEDIATE`; unique-key recovery
    leaves the driver exception handler before re-querying so canonical Contract,
    source path, or storage-locator SQL parameters cannot survive in the public
    exception chain.
  - Added a loss-preventing migration downgrade: PostgreSQL obtains fixed-order
    `ACCESS EXCLUSIVE` locks and SQLite obtains the database writer lock before the
    all-table emptiness proof; offline/unknown-dialect downgrade fails closed.
- Files changed:
  - Architecture/spec/progress:
    `docs/architecture/decisions/0002-riftx-code-audit-persistence-contract.md`,
    `docs/riftx-3-code-audit-development-spec.md`, and this ledger.
  - Domain/application:
    `src/riftx/domain/{__init__,audit,audit_records,run}.py`,
    `src/riftx/application/errors.py`, and
    `src/riftx/application/ports/{__init__,audits}.py`.
  - Persistence/migration:
    `src/riftx/persistence/{__init__,orm,repositories,audit_mappers,audit_repositories,transactions}.py`
    and `migrations/versions/3b7f1d9e5a02_add_code_audit_persistence.py`.
  - Tests:
    `tests/unit/domain/{test_audit_domain,test_audit_persistence_domain,test_models}.py`,
    `tests/unit/persistence/{test_audit_mappers,test_audit_schema,test_schema}.py`, and
    `tests/integration/persistence/{test_audit_migration,test_audit_repositories}.py`.
- Schema/migration impact:
  - Adds Alembic revision `3b7f1d9e5a02` after `0d3a8b7c4e21` with
    `audit_projects`, `source_snapshots`, `audit_contracts`, `audit_scans`,
    `audit_start_intents`, `audit_phase_runs`, `audit_scope_units`, and
    `audit_work_items`.
  - Adds the Run candidate indexes `(id, engagement_id, kind)`,
    `(id, engagement_id, kind, node_id)`, and `(id, status)` used by composite Audit
    FKs. Existing rows are not rewritten during upgrade.
  - `AuditContract`/`AuditScan` and `Run` freeze `model_profile` at 255 characters so
    the domain and length-enforcing database columns cannot diverge.
- Security boundary impact:
  - This task does not read Git, create CAS bytes, start Temporal, expose API/UI/CLI,
    run a Detector/Agent, execute target code, or enable the feature flag.
  - Contract creation cannot leave an orphan row; Snapshot/Project/Scan/Intent/Phase/
    Scope/Work creation validates the persisted candidate by its own authorization
    owner before comparing the request envelope. Legitimate cross-Audit collisions
    are opaque conflicts; corrupted persisted ownership is an integrity failure.
  - Phase outputs are empty while queued/running. Terminal output Artifacts are
    locked and validated as existing on the same Run; raw deletion/rebinding makes
    create replay, get, list, CAS, and restart reads fail closed.
  - Distribution revision facts remain rejected until AUD-506 installs their owning
    table, composite FK, and atomic Publisher.
- Tests run:
  - `conda run --no-capture-output -n agent pytest -q tests/unit/domain/test_audit_domain.py tests/unit/domain/test_audit_persistence_domain.py tests/unit/persistence/test_audit_mappers.py tests/unit/persistence/test_audit_schema.py tests/unit/persistence/test_schema.py tests/integration/persistence/test_audit_migration.py tests/integration/persistence/test_audit_repositories.py`
  - `conda run --no-capture-output -n agent pytest -q tests/unit/persistence tests/integration/persistence`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent mypy src/riftx/domain/audit.py src/riftx/domain/audit_records.py src/riftx/domain/run.py src/riftx/application/ports/audits.py src/riftx/persistence/orm.py src/riftx/persistence/audit_mappers.py src/riftx/persistence/audit_repositories.py src/riftx/persistence/transactions.py migrations/versions/3b7f1d9e5a02_add_code_audit_persistence.py`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - The seven-file Audit suite passed: `840 passed`.
  - The complete unit/integration persistence suite passed: `280 passed`.
  - Repository Ruff and targeted Mypy passed.
  - The complete Python suite passed: `3549 passed, 5 skipped`.
  - The independence boundary reported `ready=true`: 9 dependency manifests,
    454 production files, 0 explicit artifacts, and 0 violations.
  - The executable release gate reported `ready=true`; all 16 gates passed.
  - `git diff --check` passed. Ten Python 3.12 `aiosqlite` default datetime-adapter
    deprecations plus one existing Pydantic `UnsupportedFieldAttributeWarning` were
    non-blocking warnings.
- Manual verification:
  - Independent reviewers reproduced and then reverified sensitive `IntegrityError`
    chains, cross-Audit Contract/Work ID collisions, cross-Run Phase Artifacts,
    Run-terminal convergence rollback, SQLite downgrade writer serialization, and
    hostile orphan-ID redaction.
  - Final independent review reports P0=0, P1=0, and P2=0.
- Provenance:
  - Requirements source: authoritative specification sections 8.3, 8.4, 13.2,
    13.5, 13.7, and section 22 / AUD-102; accepted ADR-0002.
  - Implementation, migration, and synthetic test author: Codex task `/root`; Git
    author: Ch1nfo. ADR requirements author: Codex task `m0_docs_map`.
  - Requirements/adversarial review: Codex task `aud102_adversarial_design`.
    Independent Repository and security reviews: Codex tasks
    `aud102_repository_review` and `aud102_final_security`; independent verification:
    Codex task `aud102_full_verify`; final review: Codex task `/root`.
  - Review result: approved only after Phase Artifact ownership, SQL-parameter
    exception-chain leakage, Contract replay ambiguity, cross-Audit collision error
    classification, and model-profile length findings were closed and regression
    tested.
  - Third-party expressive material: none. Domain names, schemas, migration,
    repositories, fixtures, and tests are RiftX-owned; no Codex Security Provider,
    code, Prompt, Schema, Skill, runtime, endpoint, or dependency was used.
- Commit: Introducing commit; hash will be backfilled by the AUD-103 ledger update.
- Known limitations:
  - PostgreSQL DDL, composite FK, offline SQL, and downgrade lock ordering are
    contract-tested; RiftX does not claim PostgreSQL runtime support before a real
    database CI matrix validates lock and concurrency behavior.
  - Phase output IDs are a bounded JSON projection rather than normalized FK rows.
    The Repository locks them during CAS and fails closed after deletion/rebinding;
    normalized immutable output-reference rows may be added with the owning Artifact
    ledger if prevention, rather than detection, becomes required.
  - SnapshotStore/CAS bytes, Git preflight, aggregate creation/client-request UoW,
    API, Temporal delivery, Inventory, Detector/Agent, and Closure remain intentionally
    outside AUD-102.
- Next unblocked task: AUD-103.

## Design Deviations and ADRs

- `ADR-0001`: RiftX Code Audit is an independent reimplementation and does not claim
  a strict clean-room process. M0 proves the scanner contract and records local bundle
  evidence; the complete candidate build/SBOM matrix remains an M10 release gate.
- `ADR-0002`: freezes the minimum Code Audit persistence, ownership, canonical digest,
  replay, CAS, terminal convergence, publication-fence, and lossless-downgrade
  contract implemented by AUD-102.

## Current Risks

- The independence scanner is a bounded known-identity gate, not a substitute for the
  M10 SBOM, licensing, similarity, and human copyright review.
- Audit admission and execution are intentionally unavailable until AUD-103 onward
  adds the aggregate creation service, API policy, signed preflight, SnapshotStore,
  Inventory, and the deterministic slice.
- PostgreSQL remains a contract-tested future runtime, not a supported deployment;
  the current persistence concurrency evidence is authoritative for SQLite only.
- AUD-106 must install the kind-aware mutation inventory, Workflow router, and
  reconciliation boundary before any `code_audit` Run can execute.
