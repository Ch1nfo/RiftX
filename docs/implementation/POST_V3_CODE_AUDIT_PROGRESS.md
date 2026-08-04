# RiftX 3.0 Code Audit Implementation Progress

> Status: active
>
> Started: 2026-08-02 (Asia/Shanghai)
>
> Product baseline: `496e260f3cb1f18ce485c5f706d20d8352d6a398`
>
> Authoritative specification: `docs/riftx-3-code-audit-development-spec.md`
>
> Specification version: `riftx.code-audit-development-spec/v2`
>
> Specification revision: 2026-08-04 / AUD-202B Source Manifest and deterministic
> materializer boundary synchronized
>
> Specification baseline commit: `9a9b0e4d` (original committed baseline; later
> authoritative revisions are tracked by this ledger and Git history)
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

- Milestone: `M2 — Preflight, Snapshot, and Scope Ledger` remains `in_progress`.
- Completed task: `AUD-202B — Commit/working-tree materializer`.
- Completed AUD-202B scope: versioned Source Manifest/Capture Policy, safe commit
  blob and descriptor-bound working-tree capture, explicit deferred/excluded
  decisions, TOCTOU revalidation, private cleanup/retry, and owner-bound content plus
  Manifest CAS publication.
- Next unblocked task: `AUD-202C — Same-node mount, pin, and static ownership`, as a
  separately committed work unit.
- Production qualification remains disabled until the mandatory real-Linux
  descriptor/mount and Capsule-denial evidence is recorded.

## Milestone Status

| Milestone | Status | Exit evidence |
| --- | --- | --- |
| M0 Contract and development guardrails | completed | AUD-000 through AUD-002, full test suite, independence boundary, and release gate passed |
| M1 Run kind, domain, and persistence | completed | AUD-100 through AUD-106 complete; full repository and release gates passed |
| M2 Preflight, Snapshot, and Scope Ledger | in_progress | AUD-200, AUD-201, and AUD-202A/B completed; AUD-202C is next |
| M3 Deterministic vertical slice | pending | Not started |
| M4 Typed Agent and Standard workflow | pending | Not started |
| M5 Evidence, Finding, Baseline, Closure, reports | pending | Not started |
| M6 Standard Core API, CLI, and WebUI | pending | Not started |
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
| AUD-103 AuditApplicationService | completed |
| AUD-104 API skeleton and policy | completed |
| AUD-105 Artifact access foundation | completed |
| AUD-106 RunKind workflow router | completed |

AUD-106 replaced the temporary effect-only design with the machine-readable operation catalog,
RunWorkflowControlRouter, immutable RunnerCommand ownership, durable Workflow signal routing, and
legacy reconciliation. M1 intentionally leaves Code Audit non-executable: no authoritative Audit
effect plan exists, so Code Audit Runner enqueue remains zero until later milestone-specific plans open
individual operation families.

### M2

| Task | Status |
| --- | --- |
| AUD-200 Source root and Git preflight | completed |
| AUD-201 Signed preflight token | completed |
| AUD-202A SnapshotStore and CAS foundation | completed |
| AUD-202B Commit/working-tree materializer | completed |
| AUD-202C Same-node mount, pin, and static ownership | pending |
| AUD-206 Content Sandbox and safety stop | pending |
| AUD-203 Inventory and Scope | pending |
| AUD-204 Snapshot reader | pending |
| AUD-205 Snapshot Artifact and API | pending |
| AUD-202D Snapshot retention and GC receipts | pending |
| AUD-207 Evaluation schema and harness | pending |
| AUD-208 StartIntent delivery skeleton | pending |
| AUD-209 Security Context Bundle | pending |

### M3

| Task | Status |
| --- | --- |
| AUD-300 Detector registry | pending |
| AUD-308 Budget reservation and Usage Ledger | pending |
| AUD-301 Detector runner | pending |
| AUD-302 Native baseline Detectors | pending |
| AUD-303 SARIF import | pending |
| AUD-304 Signal normalization | pending |
| AUD-305A Deterministic Evidence, Location, and Decision ACL | pending |
| AUD-305B Reducer, risk, identity, and prepared closure | pending |
| AUD-305C Stop-proof convergence and immutable closure | pending |
| AUD-306 Deterministic Audit workflow | pending |
| AUD-307 Core seal and minimal reports | pending |
| AUD-309 Audit metrics and performance-baseline skeleton | pending |

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
| AUD-507 Triage revalidation | pending |

### M6

| Task | Status |
| --- | --- |
| AUD-600A UI read models and signed cursor | pending |
| AUD-600B Standard Core API | pending |
| AUD-601 Standard Core CLI | pending |
| AUD-602A Audit types, client, and query root | pending |
| AUD-602B SecurityContext and sensitive-cache lifecycle | pending |
| AUD-602C Durable Event transport | pending |
| AUD-602D Shared Inspector, Download, and Approval infrastructure | pending |
| AUD-603A Feature-aware layout and route metadata | pending |
| AUD-603B RunRouteGate | pending |
| AUD-604A AuditsPage | pending |
| AUD-604B NewAuditPage | pending |
| AUD-605A AuditDetail shell | pending |
| AUD-605B Summary and Coverage | pending |
| AUD-605C Findings, Signals, Evidence, and Baseline | pending |
| AUD-605D Activity, Artifacts, and Report | pending |
| AUD-606A Long-lived Code Finding and Triage | pending |
| AUD-606B CodeLocationViewer and CodeFlow | pending |
| AUD-607A Per-page i18n, accessibility, and responsive unit gate | pending |
| AUD-607B Playwright browser gate | pending |
| AUD-608 Demo and README | pending |

### M7

| Task | Status |
| --- | --- |
| AUD-700 Sandbox backend | pending |
| AUD-701 Runner capability | pending |
| AUD-702A Validation plan, Approval domain, and admission | pending |
| AUD-703 Sandbox Capsule Evidence | pending |
| AUD-704 Failure and cancellation | pending |
| AUD-702B Validation API and read models | pending |
| AUD-702C Validation CLI surface | pending |
| AUD-702D Validation WebUI surface | pending |

### M8

| Task | Status |
| --- | --- |
| AUD-800 Diff Scope planner | pending |
| AUD-801 Diff classification | pending |
| AUD-802 Deep Child Workflow | pending |
| AUD-803 Saturation and Budget | pending |
| AUD-804A Deep/Diff API and read models | pending |
| AUD-804B Deep/Diff WebUI | pending |
| AUD-804C Deep/Diff CLI | pending |

### M9

| Task | Status |
| --- | --- |
| AUD-900 Fix Advisor | pending |
| AUD-901A Fix follow-up domain, persistence, and Workflow owner | pending |
| AUD-901B Isolated fix worktree, Patch Capsule, and Artifact | pending |
| AUD-901C Fix API and read models | pending |
| AUD-901D Fix CLI surface | pending |
| AUD-901E Fix WebUI surface | pending |
| AUD-902A Patched Snapshot and Retest domain/Workflow | pending |
| AUD-902B Retest API and read models | pending |
| AUD-902C Retest CLI surface | pending |
| AUD-902D Retest WebUI surface | pending |
| AUD-903 Lifecycle projection | pending |
| AUD-904 Structural hardening portfolio | pending |

### M10

| Task | Status |
| --- | --- |
| AUD-1000 Evaluation Corpus | pending |
| AUD-1001 Fault injection | pending |
| AUD-1002 Security testing | pending |
| AUD-1003 Independence, SBOM, and licensing | pending |
| AUD-1004 Release gate | pending |
| AUD-1005 Version and documentation | pending |
| AUD-1006 Performance, capacity, and retention gate | pending |

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
- Commit: `0b48b957` (`feat(persistence): persist Code Audit aggregates`).
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

### AUD-103 — AuditApplicationService

- Status: completed.
- Outcome:
  - Accepted ADR-0003 and implemented the RiftX-owned `AuditApplicationService`,
    immutable `AuditClientRequest`, complete `AuditAggregate` read contract, pure
    draft aggregate factory, creation UoW Port, and SQLAlchemy adapters.
  - Draft creation now resolves the authoritative Project/Engagement and atomically
    writes the Code Audit Run, `run.created`, Contract+Scan, `audit.created`, and
    client-request binding in a fixed order with exactly one commit.
  - Exact client-request replay returns the current complete aggregate without
    appending Events, replacing generated identities, or lowering lifecycle/version;
    same-key/different-payload requests fail closed.
  - Added one centralized `AuditRunStateMappingPolicy` used by create, replay,
    get/list, and control planning. Pause/resume/cancel are read-only plans in this
    task and perform no database, Temporal, Runner, or filesystem effect.
  - Registered the Service as a non-optional `ControlPlane` dependency in both
    Feature Flag states. The flag gates only admission: reads, pause, cancel, and
    later safety cleanup remain reachable when creation/resume are disabled.
- Files changed:
  - Architecture/spec/progress:
    `docs/architecture/decisions/0003-riftx-code-audit-application-contract.md`,
    `docs/riftx-3-code-audit-development-spec.md`, and this ledger.
  - Domain/application:
    `src/riftx/domain/{__init__,audit_records,audit_run_state}.py`,
    `src/riftx/application/{__init__,errors}.py`,
    `src/riftx/application/ports/{__init__,audits}.py`, and
    `src/riftx/application/services/{__init__,audits}.py`.
  - Persistence/runtime/migration:
    `src/riftx/persistence/{__init__,audit_mappers,audit_repositories,audit_uow,database,orm,transactions}.py`,
    `src/riftx/api/runtime.py`, and
    `migrations/versions/7c4e1a9b2d06_add_audit_creation_requests.py`.
  - Tests:
    `tests/unit/application/test_audits.py`,
    `tests/integration/application/test_audit_application_service.py`,
    `tests/integration/persistence/test_audit_creation_migration.py`, plus the
    affected Audit domain, mapper, schema, repository, migration, database,
    composition-root, and general control-plane regression tests.
- Schema/migration impact:
  - Adds Alembic revision `7c4e1a9b2d06` after `3b7f1d9e5a02` and the immutable
    `audit_client_requests` table. The row stores only the versioned request digest,
    operation, aggregate ownership/workflow bindings, and creation time; it stores no
    caller payload, source path, preflight token, canonical Contract, or model data.
  - The idempotency key and Audit binding are unique; operation/schema CHECKs and
    RESTRICT FKs are revalidated by strict mappers and the complete aggregate loader.
  - Upgrade refuses to invent caller digests for a non-empty legacy Audit schema.
    Alembic-managed old schemas cannot be silently repaired by metadata `create_all`.
    PostgreSQL offline upgrade emits an `ACCESS EXCLUSIVE` lock before its legacy-row
    guard; non-empty and offline downgrades fail before destructive DDL.
- Security boundary impact:
  - This task does not read Git/source content, reserve preflight, create a workspace,
    expose HTTP/UI/CLI, start Temporal, register a Scanner/source adapter, or execute
    target code. The injected workspace root is used only to construct a managed
    locator string.
  - Request digests are server-computed, domain-separated canonical hashes and are
    compared in constant time. Exact-key and natural-Project duplicate rowsets fail
    closed instead of choosing an arbitrary owner.
  - Every aggregate load revalidates Scan, Contract, Project, Engagement, Run,
    request, workflow, lifecycle, and redundant ownership bindings in one consistent
    read. A corrupt item makes the entire page fail closed; bounded eager/batched
    reads prevent page-size-dependent query growth.
  - Database engines hide parameters. SQLAlchemy driver failures leave their handler
    before conversion to stable application errors; public persistence-unavailable
    failures retain no driver cause/context, SQL parameters, canonical Contract, or
    absolute source path.
  - Project natural-key races roll back temporary Engagements and re-resolve the
    winning authoritative owner before rebuilding final facts. Stateful or dishonest
    factories cannot substitute workspace/source identities between validation and
    write.
- Tests run:
  - `conda run --no-capture-output -n agent pytest -q tests/unit/application/test_audits.py tests/integration/application/test_audit_application_service.py tests/integration/persistence/test_audit_creation_migration.py tests/unit/test_api_runtime.py`
  - Repeated adversarial targeted runs over the preceding files plus affected Audit
    domain/mapper/schema/repository/migration/database/control-plane selectors.
  - `conda run --no-capture-output -n agent pytest -q tests/unit/persistence tests/integration/persistence`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent ruff check .`
  - `conda run --no-capture-output -n agent mypy src/riftx/api/runtime.py src/riftx/application/errors.py src/riftx/application/ports/audits.py src/riftx/application/services/audits.py src/riftx/domain/audit_records.py src/riftx/domain/audit_run_state.py src/riftx/persistence/audit_mappers.py src/riftx/persistence/audit_repositories.py src/riftx/persistence/audit_uow.py src/riftx/persistence/database.py src/riftx/persistence/orm.py src/riftx/persistence/transactions.py migrations/versions/7c4e1a9b2d06_add_audit_creation_requests.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Successive targeted security/application matrices passed at `154 passed` and
    `157 passed`; the wider AUD-103 regression matrix passed `392 passed`.
  - Complete unit/integration persistence passed: `290 passed, 10 warnings`.
  - The complete Python suite passed on a worktree-stable rerun:
    `3710 passed, 5 skipped, 10 warnings in 261.17s`.
  - Repository Ruff passed. Targeted Mypy passed for all 13 selected production and
    migration files. `git diff --check` passed.
  - The independence boundary reported `ready=true`: 9 dependency manifests,
    458 production files, 0 explicit artifacts, and 0 violations.
  - The executable release gate reported `ready=true`; all 16 gates passed. Ten
    Python 3.12 `aiosqlite` datetime-adapter deprecations are non-blocking.
- Manual verification:
  - Independent reviewers covered exact and conflicting replay, all creation
    failpoints, Event order, Project races, transaction rollback, corrupt duplicate
    rows, liar/stateful factories, constant-query list loading, feature-off control,
    migration/bootstrap guards, driver diagnostic redaction, and restart recovery.
  - Final independent review reports P0=0, P1=0, and P2=0; status: approved.
- Provenance:
  - Requirements source: authoritative specification sections 6.6, 13.7, 15, 17.1,
    20, and section 22 / AUD-103; accepted ADR-0003.
  - Implementation, migration, and primary test author: Codex task `/root`; Git
    author: Ch1nfo. Adversarial tests/reviews: Codex tasks
    `aud103_contract_review`, `aud103_test_arch`, and
    `aud103_independent_review`.
  - Third-party expressive material: none. No Codex Security Provider, code, Prompt,
    Schema, Skill, runtime, endpoint, dependency, or generated artifact was used.
  - Production Code Audit Agent instructions: not applicable; AUD-103 contains no
    production Agent prompt or model call.
- Commit: `51fa06cb` (`feat(application): create Code Audit drafts atomically`).
- Known limitations:
  - The Project natural-key gap race is covered by code reasoning and SQLite
    concurrency tests, but has not yet been reproduced with a real PostgreSQL barrier.
    A real PostgreSQL race/lock job is mandatory before claiming that runtime in
    release CI; SQLite evidence must not be represented as PostgreSQL proof.
  - Pause/resume/cancel return plans only. AUD-106 and later Projector/Workflow work
    own mutation, signaling, reconciliation, stopping, and cleanup effects.
  - Preflight reservation, Snapshot/source ingest, StartIntent, API policy, Workflow,
    Detector/Agent analysis, findings, reports, and UI remain deliberately deferred.
- Next unblocked task: AUD-104.

### AUD-104 — API Skeleton and Policy

- Status: completed.
- Outcome:
  - Accepted ADR-0004 and exposed only the M1 draft API:
    `POST /api/v1/audits`, `GET /api/v1/audits`, and
    `GET /api/v1/audits/{audit_id}`. Create is durable-write, draft-only, returns
    `201` on first creation and `200` on exact replay, and remains fenced by the
    default-disabled Feature Flag before replay lookup.
  - Added strict request, filter, and positive-allowlist response schemas. M1 v1
    contract-shaped proof/consent/selection values are explicitly synthetic,
    untrusted, lack a Preflight binding, and cannot Start; AUD-201 owns the v2 wire.
  - Derived request authorization identity from the authenticated principal on the
    server. Principal-scoped request identity now binds caller payload plus the
    server authorization domain without exposing that binding in the wire.
  - Added typed raw Audit authorization bindings that exclude canonical Contract,
    source path, workspace, and storage locators. Detail authorizes the raw binding
    before loading the complete aggregate in one consistent-read session; list
    pushes typed Engagement scope into SQL before stable ordering and pagination.
  - Preserved the historical general Run response while adding an Audit-rooted,
    discriminated Code Audit Run projection. Bare/orphan Code Audit Runs are not
    returned, and the default Run list remains general-only.
  - Standardized child reads as bounded owner resolution → Audit-root authorization
    → M1 RunKind read admission → full object or I/O → exact child/owner revalidation
    → safe projection. Missing, denied, post-authorization disappearance, and owner
    mismatch use the same opaque `404 resource_not_accessible` envelope.
  - Added the Code Audit Execution positive allowlist. Command, argv, executable,
    cwd, environment, output paths, PID/process group, containment identity, and
    host platform fingerprint cannot enter detail or list responses.
  - Froze the M1 generic Code Audit read allowlist to Run, Event, Execution, and
    Artifact. Finding, Report, Approval, Action, Graph, Run metrics, Target
    HTTP/Traffic, Terminal, Browser, Context, Memory, and Connector facade routes
    authorize the Audit root first and then return
    `409 run_kind_operation_unsupported` before any content getter or I/O.
  - Installed a temporary double RunKind effect bridge at API and Application
    boundaries for Run controls, Finding, Artifact/Report, RUN Memory, Approval/Run
    grant, Execution cancel, Terminal, Browser, Target HTTP, Connector, and Runner
    execution callbacks. General Run behavior remains unchanged.
  - Kept safety convergence reachable across Run kinds: stop sweeps, private
    safety-close, cleanup, and authenticated owner-matched affirmative physical-stop
    proof are not blocked by the temporary bridge. Code Audit completion cannot
    signal the legacy `riftx-run-{run_id}` workflow.
  - Removed the durable `execution.wait_completed` Event from the READ_ONLY wait
    route. Feature-off reads of already authorized allowlisted objects and internal
    cleanup/reconciliation remain registered and reachable.
  - Added complete body-validation redaction for attacker-controlled mapping keys,
    values, sequence values, unknown `loc` segments, `input`, message, and context
    literals, plus OpenAPI contracts for stable 401/403/404/409/422/503 errors.
- Files changed:
  - Architecture and execution contract:
    `docs/architecture/decisions/0003-riftx-code-audit-application-contract.md`,
    `docs/architecture/decisions/0004-riftx-code-audit-api-authorization-contract.md`,
    `docs/riftx-3-code-audit-development-spec.md`, and this ledger.
  - Audit API and policy:
    `src/riftx/api/{app,dependencies,errors,policy,runtime}.py`,
    `src/riftx/api/routes/{__init__,audits,runs,events,executions,artifacts}.py`, and
    `src/riftx/api/schemas/{__init__,audits,runs,executions}.py`.
  - Generic read admission and mutation bridge:
    the Action, Approval, Browser, Connector, Context, Finding, Graph, Memory,
    Observability, Report, Runner-control, Terminal, Traffic, and related API routes;
    Run, Approval, Artifact, Execution, Finding, Report, Runner-control, Terminal,
    Browser, Connector, Context, Memory, Target HTTP, and Worker services.
  - Authorization/persistence:
    `src/riftx/application/errors.py`, affected modules under
    `src/riftx/application/{ports,services}/`,
    `src/riftx/persistence/{audit_uow,browser_repositories,context_repositories,memory_repositories,repositories}.py`,
    and `src/riftx/security.py`.
  - Primary new tests:
    `tests/integration/api/test_audits.py`,
    `tests/integration/api/test_audit_child_read_authorization.py`,
    `tests/integration/api/test_run_kind_bridge.py`,
    `tests/unit/application/test_run_kind_effect_bridge.py`, and
    `tests/unit/models/test_audit_api_schemas.py`, plus affected Control Plane,
    Application, Browser, Connector, Context, Memory, Runner, Runtime, Target HTTP,
    policy, security, and configuration regressions.
- Schema/migration impact:
  - No Alembic revision or database table change. AUD-104 extends bounded raw-binding,
    authorized aggregate, child-owner, Run grant, and scope-filter queries over the
    AUD-100 through AUD-103 schema.
- Security boundary impact:
  - External callers can now create and read a Code Audit draft only when the
    Feature Flag and typed authorization contract allow it. Draft creation performs
    no Git/source access, realpath, Snapshot, mkdir, Temporal, Runner, model,
    Artifact, network, or stop effect.
  - Audit-root authorization is necessary but not treated as blanket permission to
    expose generic child data. Denied objects are not hydrated; Artifact file/hash,
    Runner output, Browser observation, Context Manifest, Memory content, Finding
    Evidence, Report content link, Approval command/env, and other non-allowlisted
    projections remain untouched before rejection.
  - Existing safety stop and cleanup paths do not depend on `audit.enabled=true`.
    Unknown Run kind/origin/operation remains fail-closed while AUD-106 builds the
    versioned operation catalog and workflow router.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/api/test_audit_child_read_authorization.py tests/integration/api/test_actions_api.py tests/integration/api/test_graph_api.py tests/integration/api/test_traffic_api.py`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/api/dependencies.py src/riftx/api/routes/audits.py src/riftx/api/routes/executions.py src/riftx/api/routes/memories.py src/riftx/api/schemas/audits.py src/riftx/api/schemas/executions.py src/riftx/api/schemas/runs.py src/riftx/application/ports/audits.py src/riftx/application/services/audits.py src/riftx/memory/service.py src/riftx/persistence/audit_uow.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Final generic-read authorization/OpenAPI matrix passed: `91 passed`.
  - The clean full Python rerun passed:
    `3882 passed, 5 skipped, 11 warnings in 259.67s`. The first full run observed one
    unchanged async reconciler observation race after terminal status became
    visible; its two-case selector passed three consecutive independent reruns before
    the clean full rerun.
  - Repository Ruff passed with `All checks passed!`.
  - Targeted Mypy passed with no issues in the 11 selected Audit/API/Memory/Execution
    files. This is not a repository-wide Mypy claim; a wider touched-source probe
    still reports legacy errors in `security.py` and `routes/runs.py`.
  - Independence boundary reported `ready=true`: 9 dependency manifests, 460
    production files, 0 explicit artifacts, and 0 violations.
  - Executable release qualification reported `ready=true`; all 16 gates passed.
    Eleven Python 3.12 `aiosqlite` datetime-adapter deprecations are non-blocking.
  - `git diff --check` passed with no output.
- Manual verification:
  - Independent reviews covered effectful mutation bypasses, child authorization and
    I/O ordering, response projection leakage, full-get disappearance, Runner owner
    precedence, physical-stop convergence, and the exact M1 generic-read allowlist.
  - Final contract decision is fail-closed: only Run/Event/Execution/Artifact are
    generic Code Audit reads in M1. No remaining P0/P1/P2 was accepted as deferred
    AUD-104 work; persistent RunnerCommand ownership is explicitly an AUD-106 design
    dependency and Audit execution remains impossible until then.
- Provenance:
  - Requirements source: authoritative specification sections 4.4, 16.3-16.7, 20,
    and section 22 / AUD-104; accepted ADR-0004.
  - Implementation and primary test author: Codex task `/root`; Git author: Ch1nfo.
    Independent reviews: `aud103_independent_review/effect_domain_routes`,
    `aud104_child_read_security`, and `aud104_contract_review`.
  - Third-party expressive material: none. No Codex Security Provider, code, Prompt,
    Schema, Skill, runtime, endpoint, dependency, test, or generated artifact was
    used. The implementation is RiftX-owned.
  - Production Code Audit Agent instructions: not applicable; AUD-104 contains no
    production Audit Agent prompt or model call.
- Commit: `671735be` (`feat(api): expose authorized Code Audit drafts`).
- Known limitations / next contracts:
  - AUD-105 subsequently added Artifact audit ownership, access classes,
    trust/provenance, and descriptor-safe bounded content delivery under ADR-0005.
  - AUD-106 must replace the temporary bridge with the machine-readable operation
    catalog, RunWorkflowControlRouter, versioned immutable RunnerCommand ownership,
    legacy quarantine/reconciliation, and Audit-owned control/approval/callback paths.
  - M1 v1 drafts have no authoritative Preflight plan and can never Start. AUD-201
    must create v2 drafts from server-owned Preflight/capability/consent facts rather
    than upgrading v1 Contract assertions in place.
  - PostgreSQL remains a contract-tested future runtime; AUD-104 adds no real
    PostgreSQL authorization/concurrency execution evidence.
- Next unblocked task at the time of completion: AUD-105; it is now complete.

### AUD-105 — Artifact Access Foundation

- Status: completed.
- Outcome:
  - Accepted ADR-0005 and extended Artifact with immutable Audit ownership,
    `public_export/audit_internal/restricted_sensitive` access classes,
    `generated/untrusted_source/untrusted_tool_output` trust classification,
    versioned typed ingest provenance, and canonical storage keys.
  - Enforced RunKind, Audit↔Run, and Execution↔Run ownership on create and read.
    Code Audit public Artifacts without `audit_id`, cross-Run Audit ownership, and
    cross-Run Execution ownership fail closed.
  - Restricted generic Run/Artifact list, detail, and content reads to
    `public_export` in SQL before pagination, while preserving the legacy Target HTTP
    sensitive-body filter.
  - Added the Audit-root-authorized read-only Artifact routes:
    `GET /api/v1/audits/{audit_id}/artifacts`,
    `GET /api/v1/audits/{audit_id}/artifacts/{artifact_id}`, and
    `GET /api/v1/audits/{audit_id}/artifacts/{artifact_id}/content`.
  - Replaced path-authoritative ingest/download with descriptor-safe storage: dirfd
    no-follow traversal, regular-file and single-link checks, bounded copy/hash,
    source and directory-entry fingerprint revalidation, staging fsync, read-only
    final files, atomic rename, and fd-owned bounded streaming.
  - Added repeat-cancel-safe blocking worker leases, a 128-entry verified-fingerprint
    LRU, 64 striped single-flight locks, and private storage-root/ancestor ownership
    and permission checks.
  - Made corrupt Artifact rows fail closed through a stable, path-free
    `503 artifact_persistence_unavailable` envelope for list/detail/content.
  - Added explicit HTTP, Event, Report, Context, runtime, and Agent projections.
    `add_artifact` never returns `path`, `storage_key`, or ingest provenance, and
    non-public, missing, or corrupt Artifact Event metadata cannot bypass the
    Artifact API.
- Files changed:
  - Architecture and authoritative contract:
    `docs/architecture/decisions/{0004-riftx-code-audit-api-authorization-contract,
    0005-riftx-code-audit-artifact-access-contract}.md`,
    `docs/riftx-3-code-audit-development-spec.md`, and this ledger.
  - Domain, persistence, and migration:
    `src/riftx/domain/artifact.py`, affected domain exports,
    `src/riftx/persistence/{artifact_visibility,mappers,orm,repositories}.py`, and
    `migrations/versions/91e6f4a2c8b7_partition_artifact_access.py`.
  - Descriptor storage and application boundary:
    `src/riftx/runner/{artifact_store,paths}.py`,
    `src/riftx/application/services/artifacts.py`, repository ports, and
    `src/riftx/api/{artifact_response,routes/artifacts,schemas/artifacts}.py`.
  - Safe projections and compatibility callers: Agent tools, Event/Report/Terminal,
    Context/Tool results, Browser, Connector, Runtime control, Target HTTP, Temporal,
    and Web fetch services.
  - Primary new tests:
    `tests/unit/domain/test_artifact.py`,
    `tests/unit/application/test_artifacts.py`,
    `tests/unit/api/test_artifact_response.py`,
    `tests/integration/persistence/test_artifact_access_migration.py`,
    `tests/integration/api/test_audit_artifacts.py`, and
    `tests/runner/test_artifact_store.py`, plus affected repository, API, Agent,
    Event, Report, Context, Runtime, migration, and policy regressions.
- Schema/migration impact:
  - Added Artifact Audit FK, access/trust/provenance/storage-key fields, checks, and
    visibility/owner indexes. Execution ownership is now `ON DELETE RESTRICT`.
  - SQLite performs audit, legacy backfill, batch DDL, index creation, FK validation,
    and rollback under one `BEGIN EXCLUSIVE`. Existing Code Audit Artifact rows are
    never guessed public; unsafe rows abort the migration.
  - Downgrade is allowed only when every row is losslessly representable by the old
    schema. PostgreSQL remains a dialect contract, not production runtime proof.
- Security boundary impact:
  - Audit Artifact access now has an immutable server-owned class and owner chain.
    Authorization denial and owner mismatch occur before full load, storage
    resolution, open, hash, or iterator creation.
  - The Artifact Domain intentionally supports complete serialization for internal
    round-trip. Every external or model-visible boundary must continue using an
    explicit DTO/field allowlist; direct Domain dumping is forbidden at those
    boundaries.
  - The storage integrity guarantee assumes the private state root is not writable
    by hostile scanners, target programs, model tools, or content sandboxes. A
    deployment that cannot maintain this separation must mark the capability
    unavailable.
  - No Audit Artifact write/upload route was added, and the AUD-104 RunKind effect
    bridge remains intact. Code Audit is still non-executable before AUD-106.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/domain/test_artifact.py tests/unit/persistence/test_artifact_mapper.py tests/unit/persistence/test_schema.py tests/unit/application/test_artifacts.py tests/unit/application/test_event_projection.py tests/unit/api/test_artifact_response.py tests/unit/test_api_policy.py tests/runner/test_artifact_store.py tests/integration/persistence/test_artifact_repository.py tests/integration/persistence/test_artifact_access_migration.py tests/integration/persistence/test_audit_creation_migration.py tests/integration/persistence/test_audit_migration.py tests/integration/persistence/test_audit_repositories.py tests/integration/api/test_audit_artifacts.py tests/integration/api/test_audit_child_read_authorization.py tests/integration/api/test_audits.py tests/integration/api/test_control_plane.py tests/integration/application/test_reports.py tests/context/test_tool_results.py tests/runtime/test_control_tools.py tests/integration/agent/test_cycle.py`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/api/artifact_response.py src/riftx/api/routes/artifacts.py src/riftx/api/schemas/artifacts.py src/riftx/application/event_projection.py src/riftx/application/ports/repositories.py src/riftx/application/services/artifacts.py src/riftx/application/services/events.py src/riftx/context/artifacts.py src/riftx/context/tool_results.py src/riftx/domain/artifact.py src/riftx/persistence/artifact_visibility.py src/riftx/persistence/mappers.py src/riftx/runner/artifact_store.py src/riftx/runner/paths.py src/riftx/runtime/control_tools.py`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - The extended AUD-105 target matrix passed: `506 passed, 10 warnings in 87.75s`.
  - The clean full Python run passed:
    `4097 passed, 5 skipped, 11 warnings in 310.58s`.
  - Repository Ruff passed with `All checks passed!`.
  - Targeted Mypy passed with no issues in 15 Artifact/API/Application/Context/
    Persistence/Runner files. A wider diagnostic probe still reports five existing
    errors in unchanged lines of `agent/tools.py` and `services/reports.py`, plus four
    existing errors in unchanged sections of `persistence/repositories.py`; this is
    not represented as a repository-wide Mypy pass.
  - The independence boundary reported `ready=true`: 9 dependency manifests, 463
    production files, 0 explicit artifact files, and 0 violations.
  - Executable release qualification reported `ready=true`; all 16 gates passed.
  - Eleven Python 3.12 `aiosqlite` datetime-adapter deprecations are non-blocking.
  - `git diff --check` passed with no output.
- Manual verification:
  - Independent security review covered Domain round-trip versus projection safety,
    API/Event/Agent leakage, SQL visibility ordering, RunKind/Audit/Execution owner
    validation, authorization-before-I/O, and raw-corruption repository defenses.
  - Independent review result: approved, with no P0/P1/P2 accepted as deferred
    AUD-105 work. The reviewer ran an additional 175 focused tests; the fixture
    review separately confirmed cross-Run corruption is inserted only through a
    test-only raw mapper after the production Repository rejects it.
- Provenance:
  - Requirements source: authoritative specification sections 16.7, 17.2, and 22 /
    AUD-105; accepted ADR-0005 and predecessor ADR-0004.
  - Implementation and primary tests: Codex task `/root`; Git author: Ch1nfo.
    Independent reviews: `/root/audit105_security_review`,
    `/root/audit105_test_fix_review`, and `/root/audit105_docs_review`.
  - Third-party expressive material: none. No Codex Security Provider, code, Prompt,
    Schema, Skill, runtime, endpoint, dependency, test, or generated artifact was
    used. The implementation is RiftX-owned.
  - Production Code Audit Agent instructions remain out of scope; this task only
    narrows the existing generic Agent Artifact result projection.
- Commit: `ee9adaa99df08f043a3c2a813da3728aeb81a6b6`
  (`feat(artifacts): secure Code Audit access`).
- Known limitations / next contracts:
  - AUD-106 must install the machine-readable effect inventory,
    RunWorkflowControlRouter, immutable RunnerCommand ownership envelope, and legacy
    quarantine/reconciliation before any Code Audit execution is admitted.
  - Authenticated Runner chunk upload and its lease/owner protocol are deferred to
    AUD-106 and later execution tasks. No client-supplied absolute path is accepted.
  - Atomic `max_total_artifact_bytes` enforcement must be part of the future
    authenticated creation transaction; AUD-105 deliberately does not implement a
    racy aggregate-size check without an Audit write endpoint.
  - Source Snapshot/CAS Artifact, Scanner/Detector/Agent producers, Evidence/Core
    Seal/distribution revisions, WebUI restricted-cache behavior, and real
    PostgreSQL production qualification remain assigned to their later tasks.
- Next unblocked task: AUD-106.

### AUD-106 — RunKind Workflow Router

- Status: completed.
- Outcome:
  - Accepted ADR-0006 and replaced the temporary effect-only bridge with a
    machine-readable RunKind operation/origin/effect catalog. The catalog covers the
    complete route, service, callback, reconciler, cleanup, and Runner command
    inventory and rejects unknown or incompatible combinations.
  - Added `RunWorkflowControlRouter` as the only General/Audit Workflow control
    boundary. General operations cannot be rewritten or fall back to Audit operations,
    Audit operations cannot enter the General Workflow, and safety paths may only
    reduce or prove an already-authorized effect.
  - Added owner-bound `workflow_signal_intents` for General Approval and Execution
    terminal facts. Source mutation and intent creation are atomic; delivery uses
    lease/CAS dispatch, persisted Workflow identity, outcome-unknown reconciliation,
    typed tombstones, restart recovery, and terminal supersession.
  - Added immutable Runner effect binding, command ownership, lease/envelope
    verification, result validation, and durable stop receipts. All eleven Runner
    command kinds use the shared protocol registry; output caps truncate only the
    affected stream while preserving other streams and terminal reporting.
  - Split the protocol endpoints deliberately: legacy `/finish` accepts only the
    migration-only stop-proof ACK wire, while ownership-v1 `/finish-owned` requires
    state version, verified envelope, and immutable effect binding. The RiftX Runner
    client always uses `/finish-owned`; cross-wired payloads fail validation.
  - Added `LegacyRunnerCommandEffectOwnership` for pre-AUD-106 leased stop commands.
    It contains the original node/principal/command/lease identity, is permanently
    `quarantined:legacy_ownership_missing`, carries no RunKind, and is usable only by
    the dedicated `STOP_PROOF` operation.
  - Legacy ACK admission is restricted to the original authenticated principal and
    lease, a still-`LEASED` command, one of the four safety-stop command kinds, and
    affirmative typed proof. It appends namespaced quarantine evidence only: it never
    completes the command, creates a normal receipt/projection, advances an Execution,
    closes a resource, or emits a Workflow signal. Exact replay is idempotent and any
    identity, state, command-kind, or evidence drift fails closed.
  - Legacy replacement planning is authority-ledger based. A Terminal close is issued
    only when one unambiguous Terminal belongs to the same Run, Runner, and Execution;
    duplicate or mismatched Terminal ledgers degrade to authoritative Execution cancel.
  - Preserved the M1 execution fence: no authoritative Code Audit effect plan exists,
    therefore Code Audit Runner enqueue is unconditionally zero. AUD-106 does not
    start M2 and does not make Code Audit executable.
- Files changed:
  - Architecture/specification: ADR-0005 cross-reference, ADR-0006, the authoritative
    development specification, and this ledger.
  - Domain/application/API: RunKind effects and router, Audit control service,
    Workflow signal domain/ports/services, Runner ownership/protocol models, control
    service/client/routes/schemas, and all cataloged effect entrypoints.
  - Persistence/runtime: Runner ownership, command effect binding, stop receipt and
    projection repositories; Workflow signal outbox and transport; API/Temporal
    dispatcher and reconciler assembly; kind-aware Execution/runtime completion.
  - Migrations:
    `4f9a6c1d2e30_add_workflow_signal_intents.py` and
    `8d7c2e4f1a90_add_runner_command_ownership.py`.
  - Tests: catalog/policy/domain, Runner protocol/client/daemon, API callback and
    cross-wire behavior, Workflow signal atomicity/restart recovery, repository
    ownership/receipt projection, migration/downgrade protection, Temporal/runtime,
    Browser/Terminal/Target HTTP/Connector, and long-horizon recovery regressions.
- Schema/migration impact:
  - Added the durable Workflow signal-intent outbox with owner, identity, lease,
    attempt, reconciliation, delivery, supersession, and typed tombstone state.
  - Added immutable Runner command ownership/effect binding and durable stop
    receipt/projection facts, including the quarantined legacy ownership variant.
  - Upgrade quarantines every legacy pending/leased/terminal command without inferring
    ownership from payload, command kind, target, result, or lease metadata.
  - PostgreSQL offline upgrade emits stable SQL. Offline downgrade fails closed with a
    deliberate migration guard. Online downgrade locks all seven Runner fact tables
    before any safety read or DDL and refuses downgrade when protocol capability,
    non-zero command state version, ownership/effect binding, receipt/projection,
    replacement, reconciliation, or Execution Runner-binding evidence would be lost.
  - SQLite upgrade/downgrade retains exclusive transaction and foreign-key validation
    behavior; restart, rollback, retry, and legacy ACK evidence preservation are tested.
- Security boundary impact:
  - Generic Run and Code Audit controls now have separate typed operations, services,
    Workflow owners, callbacks, and Runner effect identities. No fallback crosses the
    boundary, including failure, cancellation, cleanup, replay, or legacy recovery.
  - Runner completion is accepted only for the authenticated principal, current lease,
    exact command state version, verified envelope, and immutable effect binding.
    Repository checks repeat the service checks before durable projection.
  - Legacy compatibility is a quarantine evidence sink, not an authority-upgrade path.
    It cannot mint RunKind, Workflow ownership, ordinary completion, or resource state.
  - M1 Code Audit Runner enqueue remains zero. M2/M3 may later open only explicitly
    registered `AuditStaticEffectPlan` families; M7/M9 dynamic Build/Test/PoC/Fix must
    remain on the separate `AuditExecutionPlan` plus `mandatory_one_plan` approval.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/runner`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_runner_ownership_migration.py`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/application/run_kind_effects.py src/riftx/application/workflow_router.py src/riftx/application/services/audit_controls.py src/riftx/application/services/workflow_signals.py src/riftx/domain/runner.py src/riftx/domain/workflow_signal.py src/riftx/persistence/workflow_signals.py src/riftx/persistence/audit_control_uow.py src/riftx/temporal/workflow_signal_transport.py src/riftx/api/routes/runner_control.py src/riftx/api/schemas/runner_control.py src/riftx/application/services/runner_control.py src/riftx/runner/control_client.py src/riftx/runner/daemon.py src/riftx/persistence/repositories.py`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Full repository: `4365 passed, 5 skipped, 11 warnings in 356.87s`.
  - Runner matrix: `410 passed, 5 skipped`; migration matrix: `14 passed`.
  - Additional focused matrices passed: API `78`, Runner ownership `140`,
    Execution/Runtime/Temporal `341`, Browser/Terminal/Connector/Evaluation `158`,
    Target HTTP `53`, and Workflow signals `56` tests.
  - Targeted Mypy passed with `Success: no issues found in 15 source files`.
    Repository Ruff and `compileall` passed; `git diff --check` passed with no output.
  - The independence boundary reported `ready=true` with policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
    The executable release gate also reported `ready=true`.
  - Independent completion review found no remaining P0/P1 and passed an additional
    focused `152` tests. The five skips are platform-only Windows/PowerShell/cgroup
    conditions; the eleven warnings are the known Python 3.12 datetime-adapter notices.
- Manual verification:
  - Reviewed the complete operation/effect inventory, General/Audit non-fallback,
    M1 zero-enqueue invariant, signal outbox restart semantics, all eleven Runner
    command bindings, endpoint cross-wiring, legacy ACK replay/drift behavior,
    ambiguous Terminal replacement, and downgrade evidence preservation.
  - Independent review result: accepted, with no remaining P0/P1. No issue was
    deferred into M2 as a substitute for the AUD-106 contract.
- Provenance:
  - Requirements source: authoritative specification sections 4.3 through 4.5, 14,
    20.4, and 22 / AUD-106; accepted ADR-0006.
  - Implementation inputs: RiftX repository baseline `ee9adaa9`, ADR-0001
    through ADR-0005, and the current RiftX route/service/Runner/Temporal code.
  - Third-party expressive material: none. No Codex Security Provider, code, Prompt,
    Schema, Skill, runtime, endpoint, dependency, test, or generated artifact was
    used. The implementation and protocol are RiftX-owned.
- Commit: this AUD-106 local commit; its hash is backfilled by the next ledger update
  because a commit cannot contain its own hash.
- Known limitations / next contracts:
  - At AUD-106 completion, M2 remained `pending / not started`. Its future static
    operations required explicit `AuditStaticEffectPlan` registrations; AUD-106
    granted no implicit capability.
  - Real PostgreSQL production qualification, dynamic Audit execution, Content
    Sandbox, Scanner/Detector/Agent producers, and product UI remain assigned to their
    later milestones and are not represented as implemented here.
- Next unblocked task at the time of completion: AUD-200, as a new, separately
  committed M2 work unit.

### AUD-200 — Source Root and Git Preflight

- Status: completed.
- Outcome:
  - Accepted ADR-0007 and delivered a durable, non-Run-scoped
    `AuditPreflightJob`, dedicated owner/lease/result/exit/stop contracts,
    bounded Operator projection, local-only Runner wire, and fail-closed expiry
    reconciliation.
  - Added the local-Linux SourceIngest backend and standalone Capsule worker.
    Git parsing never runs in the Control Plane or ordinary Worker.
  - Added descriptor-bound source admission and mount identity proof, immutable
    prepare/process/result bindings, affirmative stop/destruction proof, and
    crash/replay/orphan recovery without blind redispatch.
  - Added strict Git config/admin/object guards, pre/post inventory `git fsck`,
    SHA-1/SHA-256 object-name binding, pack-pair/MIDX-sidecar validation,
    graft rejection, shallow handling, hardlink/symlink/special-file rejection,
    and bounded resource/output handling.
  - Preserved the M1 fence: ordinary Code Audit Run-scoped Runner enqueue remains
    zero. AUD-200 creates no Audit, Run, Snapshot/CAS handle, plan/token,
    Context Bundle, model, Scanner, Detector, or Workflow.
- Independent security review:
  - Final result: no remaining P0/P1 in the reviewed AUD-200 scope.
  - Mount-probe late create, ambiguous Docker start, Git object-integrity, and
    restricted reconciler projection findings are closed.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit tests/runner/test_audit_preflight_control_client.py tests/runner/test_audit_preflight_runner.py tests/runner/test_audit_preflight_journal.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit/test_source_ingest_backend.py tests/unit/audit/test_source_ingest_worker.py tests/integration/api/test_audit_preflight_runner.py tests/integration/persistence/test_audit_preflight_repository.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit tests/unit/domain/test_audit_preflight.py tests/unit/domain/test_audit_preflight_wire.py tests/unit/models/test_audit_preflight_api_schemas.py tests/unit/application/test_audit_preflight.py tests/unit/test_audit_preflight_public_api.py tests/runner/test_audit_preflight_control_client.py tests/runner/test_audit_preflight_journal.py tests/runner/test_audit_preflight_runner.py tests/runner/test_control_client_protocol.py tests/integration/api/test_audit_preflight.py tests/integration/api/test_audit_preflight_runner.py tests/integration/api/test_audits.py tests/integration/api/test_control_plane.py tests/integration/persistence/test_audit_preflight_migration.py tests/integration/persistence/test_audit_preflight_repository.py tests/integration/persistence/test_audit_creation_migration.py tests/integration/persistence/test_runner_ownership_migration.py tests/integration/persistence/test_workflow_signal_migration.py tests/unit/application/test_run_kind_effect_policy.py tests/unit/persistence/test_schema.py tests/unit/test_api_policy.py tests/unit/test_api_runtime.py tests/unit/test_audit_config.py tests/unit/test_local_operator_auth.py tests/unit/test_runner_daemon_cli.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/runner`
  - `conda run --no-capture-output -n agent python -m pytest -q -k 'not test_pre_patch_cleanup_exhaustion_keeps_intent_for_worker_recovery'`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/temporal/test_workflow.py::test_pre_patch_cleanup_exhaustion_keeps_intent_for_worker_recovery`
  - `conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Final AUD-200 changed-surface matrix: `660 passed`.
  - Independent review matrices passed `154` Audit/Runner tests and `42` Git
    worker tests with no remaining P0/P1.
  - Runner suite: `440 passed, 5 skipped`; migration/schema matrix:
    `134 passed, 231 deselected`; OpenAPI/preflight API matrix: `6 passed`.
  - Strict Mypy passed for all 18 newly introduced AUD-200 source files.
    Repository Ruff, `compileall`, and the 35-file new-Python format check passed;
    `git diff --check` passed with no output.
  - The independence boundary reported `ready=true` under policy
    `riftx.code-audit-independence/v1`, digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
    The executable release gate reported `ready=true` with every gate passed.
  - Full-repository partition: `4650 passed, 5 skipped, 1 deselected`; the
    deselected Temporal node passed separately as `1 passed`. Together the two
    commands cover every non-skipped repository test.
  - The ordinary one-shot full-suite command exposed the pre-existing event
    observation race in
    `test_pre_patch_cleanup_exhaustion_keeps_intent_for_worker_recovery`: the test
    observes the terminal status and cancels the reconciler before the following
    event append becomes visible. The test and Temporal runtime are outside the
    AUD-200 diff; no unrelated runtime change was included.
- Provenance:
  - No Codex Security Provider, code, Prompt, Schema, Skill, runtime,
    endpoint, dependency, test, or generated artifact was used. The
    implementation and protocol are RiftX-owned.
- Commit: `3ee0cbf9` (`feat(code-audit): complete source root and Git preflight`).
- Known limitations / production qualification:
  - The completion review ran on macOS. A real local-Linux Docker
    descriptor/mount round-trip smoke was not executed in this work unit.
  - This does not claim macOS/Windows backend support or production Linux
    qualification. Non-Linux remains fail-closed/backend unavailable; fake and
    in-process tests are not production evidence.
  - Before enabling or claiming production SourceIngest capability, the exact
    image/policy/descriptor-mount/identity/stop-proof smoke must pass on a
    supported same-host Linux environment and be recorded as release evidence.
- Next unblocked task: AUD-201, as a separately committed work unit.

### AUD-201 — Signed Preflight Token (Plan/Create v2 Step)

- Status: completed as the first committed AUD-201 step.
- Outcome of this committed step:
  - Accepted ADR-0008 and added durable `AuditPreflightPlan` identity, lifecycle,
    HMAC token codec, key rotation verification, expiry, reservation, and safe replay.
    Raw bearer tokens are never persisted and are excluded from object repr/log facts.
  - Added migration `5d8c1a7e3b24`, the Plan repository, issuance eligibility on
    successful Preflight Jobs, and the canonical-empty
    `AuditSecurityContextBinding` with exact Plan/owner/Audit composite ownership.
  - Added `POST /api/v1/audits/preflight/{job_id}/plan` with authorized 201/200
    issuance replay and `Cache-Control: no-store`. Missing token key configuration
    fails closed; production assembly has no fallback or development key.
  - Published caller-only `riftx.audit-create-draft-request/v2`, immutable
    `riftx.audit-contract/v2`, and `riftx.model-data-egress/v2`. Contract output is
    honestly `preflight_bound_draft` and `start_eligible=false`; no Snapshot or
    execution proof is fabricated.
  - Extended the creation UoW so one transaction locks the Plan by token hash,
    validates principal/scope/preferences, reserves it, and inserts Engagement,
    Project, Run/events, Contract, Scan, Binding, and request facts. Exact replay
    returns the original aggregate; ten injected failure stages prove rollback of
    both the reservation and every aggregate fact.
  - Production new-create admission rejects the synthetic v1 path. Historical v1
    persistence/read behavior remains available, and tests can opt into the legacy
    draft-only wire explicitly without granting Start authority.
  - Stabilized the pre-existing Temporal cleanup recovery test so it waits for both
    the terminal status and its following audit event before cancelling the
    reconciler; production Temporal behavior is unchanged.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/domain/test_audit_preflight_plan.py tests/unit/domain/test_audit_contract_v2.py tests/unit/application/test_audit_preflight_plan_issuance.py tests/integration/application/test_audit_create_v2.py tests/integration/persistence/test_audit_preflight_plan_migration.py tests/integration/persistence/test_audit_preflight_plan_repository.py tests/integration/persistence/test_audit_creation_migration.py tests/integration/persistence/test_audit_preflight_migration.py tests/integration/persistence/test_runner_ownership_migration.py tests/integration/api/test_audits.py tests/integration/api/test_control_plane.py tests/unit/models/test_audit_api_schemas.py tests/unit/application/test_run_kind_effect_policy.py tests/unit/domain/test_audit_persistence_domain.py tests/unit/persistence/test_audit_schema.py tests/unit/persistence/test_schema.py tests/unit/test_api_policy.py tests/unit/test_api_runtime.py tests/unit/test_audit_config.py tests/unit/test_runtime_config.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/temporal/test_workflow.py::test_pre_patch_cleanup_exhaustion_keeps_intent_for_worker_recovery`
  - `conda run --no-capture-output -n agent python -m ruff check src tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `git diff --check`
- Test results:
  - AUD-201 changed-surface matrix: `695 passed`.
  - Temporal cleanup recovery regression: `1 passed`.
  - Repository Ruff and `compileall` passed; `git diff --check` passed with no output.
  - Full repository suite: `4767 passed, 5 skipped, 12 warnings` in `388.04s`.
  - The independence boundary reported `ready=true`, scanned 499 production files,
    and retained policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
  - The executable release gate reported `ready=true` with every registered gate
    passed. This does not replace the explicitly unexecuted real-Linux AUD-201
    production qualification noted below.
- Provenance:
  - No Codex Security Provider, code, Prompt, Schema, Skill, runtime, endpoint,
    dependency, test, or generated artifact was used. The implementation and
    protocol are RiftX-owned.
- Commit: `f10a0f74` (`feat(code-audit): add signed preflight plans and create v2`).
- Boundary carried into the next committed AUD-201 step:
  - The persisted Create v2 Contract remains `start_eligible=false` because
    Snapshot/start-delivery capabilities are absent. The following Start contract
    step reconciles the previous contradictory success requirement without
    fabricating those capabilities.
  - No Snapshot/CAS/Manifest/materializer/mount/pin, Temporal dispatch, ordinary
    Runner enqueue, model, Agent, Scanner, Detector, network fetch, dependency
    installation, or execution capability was introduced.
  - The real local-Linux descriptor/mount and Capsule deny smoke was not executed on
    this macOS work unit. Release qualification remains disabled.

### AUD-201 — Signed Preflight Token (Start Admission Contract Step)

- Status: completed.
- Outcome:
  - Reconciled ADR-0008 and the authoritative specification so the immutable
    `preflight_bound_draft/start_eligible=false` Contract remains capability-honest.
    Current v1 and v2 drafts reject Start before source revalidation or any Start UoW;
    they cannot be upgraded or hot-filled later.
  - Added strict `start_request_id + reviewed_contract_digest` validation and an
    Audit-root `HOST_EXECUTE` authorized application service. Validation order is
    Feature Flag, wire, authorization/read, reviewed digest, draft/created state,
    historical v1, then current v2 capability eligibility.
  - Added domain-separated, short-lived `AuditStartRevalidationRequest/Proof`
    contracts. The request binds Audit, Run, Plan, Contract, Context, principal,
    authorization scope, Node, source root, repository, content, backend, image,
    policy, and a hidden canonical repository path digest.
  - Added the future `AuditStartAdmissionUnitOfWork` request/projection contract with
    exact Plan/Contract/source/context/revalidation/Intent bindings. Its successful
    projection is Plan consumed, Audit queued, Run preparing, and pending
    `AuditStartIntent` in one transaction; no current Contract can construct it.
  - Integration evidence proves rejected current-v2 Start preserves Plan reserved,
    Audit draft, Run created, two existing create events, zero StartIntents, zero
    revalidation calls, and zero admission-UoW calls. No public Start route,
    Snapshot implementation, Runner enqueue, or Temporal dispatch was added.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/application/test_audit_start.py tests/integration/application/test_audit_start.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/api/test_control_plane.py::test_terminal_websocket_takeover_io_resize_interrupt_and_release`
  - `conda run --no-capture-output -n agent python -m ruff check src tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Start contract unit/integration matrix: `7 passed` in `1.31s` after final
    authoritative-binding hardening.
  - The unrelated terminal takeover test that previously encountered one transient
    SQLite lock in a broad mixed matrix passed alone: `1 passed` in `2.15s`.
  - Repository Ruff and `compileall` passed.
  - Full repository suite on the final worktree state: `4774 passed, 5 skipped,
    11 warnings` in `409.59s`.
  - The independence boundary reported `ready=true`, scanned 501 production files,
    found zero violations, and retained policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
  - The executable release gate reported `ready=true`; every registered gate passed,
    including the PTY takeover gate.
  - `git diff --check` passed with no output.
- Provenance:
  - No Codex Security Provider, code, Prompt, Schema, Skill, runtime, endpoint,
    dependency, test, or generated artifact was used. The implementation and
    protocol are RiftX-owned.
- Commit: `f24771cc` (`feat(code-audit): close AUD-201 start admission contract`).
- Known limitations / production qualification:
  - Successful Plan consume + Audit queued + Run preparing + pending StartIntent is
    deliberately deferred until later capabilities can create a new immutable
    `start_eligible=true` Contract; AUD-208 will integrate that admission and reliable
    delivery. Historical drafts remain permanently non-startable.
  - The completion review ran on macOS. The mandatory real local-Linux descriptor/
    mount round-trip and Capsule write/create/chmod/rename/unlink denial smoke was not
    executed, so production release qualification remains disabled.
- Next unblocked task at AUD-201 completion: AUD-202A; it is now completed below.

### AUD-202A — SnapshotStore and CAS Foundation

- Status: completed.
- Depends on: AUD-201 (`f24771cc`).
- Exact modules/files:
  - `src/riftx/audit/snapshot.py`
  - `src/riftx/audit/snapshot_store.py`
  - `src/riftx/audit/__init__.py`
  - `src/riftx/persistence/audit_snapshot.py`
  - `src/riftx/persistence/orm.py`
  - `src/riftx/persistence/__init__.py`
  - `migrations/versions/8a1f3c5e7b90_add_snapshot_references.py`
  - `tests/unit/audit/test_snapshot_store.py`
  - `tests/unit/persistence/test_audit_schema.py`
  - `tests/unit/persistence/test_schema.py`
  - `tests/integration/persistence/test_snapshot_references.py`
  - `tests/integration/persistence/test_snapshot_reference_migration.py`
  - `docs/architecture/decisions/0009-riftx-code-audit-snapshot-store-cas-foundation.md`
  - `docs/architecture/decisions/0007-riftx-code-audit-preflight-job-and-source-ingest-contract.md`
- Outcome:
  - Accepted ADR-0009 and added `riftx.snapshot-cas-object/v1` with canonical,
    domain-separated object identity. Project, Snapshot digest, Manifest digest,
    object type, sorted blob paths, blob type/mode/digest/size, and aggregate counters
    all participate in the descriptor and opaque locator.
  - Added `LocalSnapshotStore` under the configured private Snapshot root. It copies
    an exact declared regular-file/symlink staging tree into store-owned same-filesystem
    staging, fsyncs bytes/index/directories, atomically renames, then seals and fully
    verifies the final object read-only.
  - Exact replay requires identical canonical metadata, bytes, modes, sizes, digests,
    Manifest binding, and sealed permissions. Corrupt or half-written objects are
    atomically quarantined and the request fails; they are never overwritten in place.
  - Added owner/Manifest-bound `verify` and `open_blob`; relative paths must be in the
    descriptor allowlist. regular files use no-follow verified descriptors and bounded
    readers; symlinks return verified target bytes without following the target.
  - Added cross-process per-object publication locks and explicit power-loss stages.
    Pre-publish crash leaves a private staging orphan that supports dry-run cleanup;
    post-publish crash retries through full verify and exact replay.
  - Added `snapshot_references` migration/ORM/Repository. Composite FKs bind Audit and
    Snapshot to the same Project; exact replay is idempotent, cross-owner/digest
    corruption fails closed, and a non-empty table blocks lossy downgrade before DDL.
- Schema/migration owner:
  - Revision `8a1f3c5e7b90`, down revision `5d8c1a7e3b24`.
  - Owns only `snapshot_references`; existing insert-is-seal `source_snapshots` schema
    is unchanged. The new table primary key is `(audit_id, snapshot_id, role)` and
    carries `project_id`, schema version, reference digest, and creation time.
- API surface: none. No ordinary API, CLI, WebUI, Event, Artifact, Start, Runner, or
  Temporal surface receives a CAS locator or Snapshot bytes in this task.
- Fail-closed conditions:
  - invalid/noncanonical IDs, digests, paths, modes, size limits, source/store overlap,
    undeclared or missing entries, linked/mutating regular files, Manifest/owner drift,
    writable/corrupt persisted objects, noncanonical index bytes, cross-Project
    references, damaged reference digests, and lossy downgrade all reject.
- Explicit non-goals:
  - Git object/index capture, commit/dirty materialization, final Source Manifest
    decisions, `SourceSnapshot` seal UoW, SourceIngest production write protocol,
    mount/pin/static ownership, retention/GC/pressure eviction, API projection,
    Start/Workflow, Detector/Scanner/model, and network access remain absent.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit/test_snapshot_store.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_snapshot_references.py tests/integration/persistence/test_snapshot_reference_migration.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/persistence/test_audit_schema.py tests/unit/persistence/test_schema.py tests/integration/persistence/test_migrations.py tests/integration/persistence/test_audit_repositories.py tests/integration/persistence/test_snapshot_references.py tests/integration/persistence/test_snapshot_reference_migration.py tests/unit/audit/test_snapshot_store.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_audit_creation_migration.py tests/integration/persistence/test_audit_preflight_migration.py tests/integration/persistence/test_audit_preflight_plan_migration.py tests/integration/persistence/test_runner_ownership_migration.py tests/integration/persistence/test_snapshot_reference_migration.py`
  - `conda run --no-capture-output -n agent python -m ruff check src tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - SnapshotStore contract/power-loss/concurrency matrix: `7 passed` in `0.07s`.
  - Reference Repository/migration matrix: `6 passed` in `3.21s`.
  - Related persistence/schema/CAS matrix on the final implementation:
    `113 passed, 10 warnings` in `30.89s`.
  - Historical migration/head/no-partial-DDL regression matrix after synchronizing
    the new head and cross-boundary guards: `45 passed` in `33.46s`.
  - The first full-suite run exposed only ten stale migration-head/no-partial-DDL
    expectations: `4778 passed, 10 failed, 5 skipped, 12 warnings` in `416.02s`.
    The CAS, reference, application, Runner, and other business paths passed; the
    migration compatibility contract was corrected without weakening its safety rule.
  - Final full repository suite: `4788 passed, 5 skipped, 11 warnings` in `428.94s`.
  - Repository Ruff and `compileall` passed; `git diff --check` passed with no output.
  - The independence boundary reported `ready=true`, scanned 505 production files,
    found zero violations, and retained policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
  - The executable release gate reported `ready=true`; every registered gate passed.
- Provenance:
  - No Codex Security Provider, code, Prompt, Schema, Skill, runtime, endpoint,
    dependency, test, or generated artifact was used. The implementation and
    protocol are RiftX-owned.
- Commit: `7abf1fce feat(code-audit): add snapshot store CAS foundation`.
- Known limitations / production qualification:
  - The CAS index freezes only storage-level blob metadata and the future Manifest
    digest; AUD-202B still owns deterministic Git/working-tree materialization and
    final capture-decision Manifest semantics.
  - This work ran on macOS with synthetic staging trees. It does not replace the
    mandatory real local-Linux SourceIngest descriptor/mount/Capsule deny smoke;
    production release qualification remains disabled.
- Next unblocked task: AUD-202B, as a separately committed work unit.

### AUD-202B — Commit/Working-tree Materializer

- Status: completed.
- Depends on: AUD-202A (`7abf1fce`).
- Exact modules/files:
  - `src/riftx/audit/source_manifest.py`
  - `src/riftx/audit_worker/materializer.py`
  - `src/riftx/audit_worker/preflight.py`
  - `src/riftx/audit/__init__.py`
  - `tests/unit/audit/test_source_materializer.py`
  - `docs/architecture/decisions/0010-riftx-code-audit-source-materializer-and-manifest.md`
  - `docs/riftx-3-code-audit-development-spec.md`
- Outcome:
  - Accepted ADR-0010 and added `riftx.source-manifest/v1`,
    `riftx.source-materializer/v1`, and `riftx.source-capture-policy/v1`.
    Manifest entries use unique raw-byte ordering, canonical POSIX UTF-8 paths where
    representable, and opaque base64 plus path digest for legal non-UTF-8/noncanonical
    Git paths. Every entry freezes origin, object type, mode, size, SHA-256, Git object
    identity, language, classification, decision, and reason.
  - Commit capture reuses the reviewed AUD-200 Git structure/config/object-store
    snapshot. It resolves a commit, enumerates `ls-tree`, and reads only eligible blobs
    through fixed `cat-file blob <lower-hex-id>` argv with exact size bounds. The
    general SafeGitAdapter command allowlist does not expose `cat-file`, filters,
    textconv, drivers, or arbitrary object expressions.
  - Working-tree capture combines stage-0 index, porcelain status, tracked/untracked/
    ignored inventories, and a descriptor-bound filesystem leaf walk. regular files
    use one no-follow fd for initial/final fingerprint and bounded read; symlink targets
    are copied as bytes and never followed. Final publication requires unchanged
    fingerprints, candidate sets, status, Git admin/object guard, and strict fsck.
  - Capture decisions are explicit: hardlink, special file, invalid UTF-8 content/path,
    oversized/budget-limited file, and LFS pointer are deferred; submodule and ignored
    input are excluded; untracked/generated/vendor inclusion is policy-bound. No
    deferred/excluded bytes enter the content tree.
  - Source content and canonical Manifest publish as separate Project/Snapshot/
    Manifest-bound CAS trees. Both locators remain opaque and outside Run Artifact
    ownership; Manifest bytes do not appear in the analysis source root.
  - Concurrent identical captures exact-replay both CAS objects. Content changes create
    new tree/Snapshot/Manifest identities. Partial materialization is removed on error;
    cleanup failure leaves only a private, prefix-bounded orphan supporting dry-run
    cleanup and retry.
- API surface: none. No ordinary API, CLI, WebUI, Event, Artifact, Start, Runner static
  effect, Temporal, Scanner, or model surface receives Snapshot bytes or CAS locators.
- Fail-closed conditions:
  - invalid request/policy/path/Manifest shape, unmerged index, Git tree/index/blob
    mismatch, Git admin/object drift, candidate/status/fingerprint TOCTOU, path escape,
    source/output overlap, staging write failure, Manifest entry limit, owner binding
    mismatch, and cleanup failure all reject with typed/path-free errors.
- Explicit non-goals:
  - `SourceSnapshot` insert-is-seal UoW, Audit reference creation, mount/pin/static
    effect ownership, remote hydration, recursive submodule/LFS materialization,
    retention/GC, Scope Ledger/reader, Artifact/API projection, Start/Workflow,
    Detector/Scanner/model remain absent.
- Tests run:
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit/test_source_materializer.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit/test_source_materializer.py tests/unit/audit/test_source_ingest_worker.py tests/unit/audit/test_snapshot_store.py`
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/audit/test_source_ingest_backend.py`
  - `conda run --no-capture-output -n agent python -m ruff check src tests migrations scripts/qa`
  - `conda run --no-capture-output -n agent python -m compileall -q src/riftx tests`
  - `conda run --no-capture-output -n agent python -m pytest -q`
  - `conda run --no-capture-output -n agent python scripts/qa/code-audit-boundary-gate.py`
  - `conda run --no-capture-output -n agent python scripts/qa/release-gate.py`
  - `git diff --check`
- Test results:
  - Materializer/Manifest decision, TOCTOU, concurrency, cleanup, retry, and owner-bound
    publication matrix: `16 passed` in `5.42s`.
  - Materializer plus SafeGit/Preflight/SnapshotStore regression matrix: `65 passed`
    in `15.30s`.
  - Complete Audit unit package: `141 passed` in `13.66s`.
  - SourceSnapshot domain plus durable reference regression matrix: `197 passed` in
    `1.22s`.
  - SourceIngest backend regression matrix: `21 passed` in `0.28s`.
  - Final full repository suite: `4804 passed, 5 skipped, 11 warnings` in `430.39s`.
  - Repository Ruff and `compileall` passed; `git diff --check` passed with no output.
  - The independence boundary reported `ready=true`, scanned 507 production files,
    found zero violations, and retained policy digest
    `bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8`.
  - The executable release gate reported `ready=true`; every registered gate passed.
- Provenance:
  - No Codex Security Provider, code, Prompt, Schema, Skill, runtime, endpoint,
    dependency, test, or generated artifact was used. The implementation and protocol
    are RiftX-owned.
- Commit: this AUD-202B local commit; its hash is backfilled by the next ledger update
  because a commit cannot contain its own hash.
- Known limitations / production qualification:
  - The materializer is SourceIngest-only and has no product dispatch surface in this
    task. Production enablement still requires the real local-Linux descriptor/mount/
    Capsule denial and staging-to-CAS round-trip evidence; macOS fixtures do not
    qualify the backend.
  - Audit-bound mount/read authorization is AUD-202C; the current CAS remains private,
    opaque, and unavailable to ordinary API callers.
- Next unblocked task: AUD-202C, as a separately committed work unit.

## Design Deviations and ADRs

- `ADR-0001`: RiftX Code Audit is an independent reimplementation and does not claim
  a strict clean-room process. M0 proves the scanner contract and records local bundle
  evidence; the complete candidate build/SBOM matrix remains an M10 release gate.
- `ADR-0002`: freezes the minimum Code Audit persistence, ownership, canonical digest,
  replay, CAS, terminal convergence, publication-fence, and lossless-downgrade
  contract implemented by AUD-102.
- `ADR-0003`: freezes the always-registered Audit Application Service, immutable
  request-level idempotency record, single-commit draft creation order, complete
  aggregate loader, centralized Audit↔Run state mapping, read-only control plans,
  Feature Flag admission order, and driver-error redaction implemented by AUD-103.
- `ADR-0004`: freezes the M1 Audit API wire, typed raw-binding authorization,
  scope-before-pagination, positive-allowlist projections, opaque child-read ordering,
  strict generic-read allowlist, temporary double RunKind effect bridge, safety-stop
  exceptions, Runner callback precedence, and Feature-Flag cleanup contract implemented
  by AUD-104.
- `ADR-0005`: freezes Artifact RunKind/Audit/Execution ownership, access/trust/provenance,
  canonical storage keys, lossless migration, generic public-only visibility, explicit
  Audit-root reads, descriptor-safe ingest/download, corrupt-row redaction, and
  external projection boundaries implemented by AUD-105.
- `ADR-0006`: freezes the machine-readable RunKind effect catalog, full entrypoint
  inventory, General-compatible Workflow router, Audit-owned controls, kind-aware
  cleanup/completion, immutable Runner effect/command ownership, legacy quarantine,
  protocol capability gate, M1 zero-enqueue fence, future family-specific static and
  dynamic plan extensions, and safety-reduce-only semantics for AUD-106.
- `ADR-0007`: freezes the non-Run-scoped AuditPreflightJob owner, dedicated
  Runner protocol, staged AuditPreflightResult, SourceIngest Capsule,
  descriptor/mount identity, affirmative stop/recovery, safe projection, and
  AUD-201/AUD-202/AUD-206/AUD-209 boundaries implemented by AUD-200.
- `ADR-0008`: freezes durable Plan/token identity and lifecycle, issuance API,
  Create v2 ownership, canonical-empty Context Binding, historical v1 isolation,
  strict Start proof/UoW contracts, current-version zero-side-effect rejection, and
  the future start-ready AUD-208 admission/delivery boundary. AUD-201 is implemented.
- `ADR-0009`: freezes Project-bound Snapshot CAS identity, opaque locators, exact
  staging/fsync/atomic publish, full verify and bounded open, corrupt-object
  quarantine, staging crash cleanup, and durable composite Snapshot references
  implemented by AUD-202A.
- `ADR-0010`: freezes versioned Source Manifest/Capture Policy identity, fixed commit
  blob reads, descriptor-bound working-tree capture, explicit capture decisions,
  TOCTOU revalidation, private staging cleanup/retry, and dual content/Manifest CAS
  publication implemented by AUD-202B.

## Current Risks

- The independence scanner is a bounded known-identity gate, not a substitute for the
  M10 SBOM, licensing, similarity, and human copyright review.
- Production new-draft admission is now Plan-bound Create v2. The legacy v1 wire and
  current `preflight_bound_draft` v2 wire remain permanently non-startable. The
  SnapshotStore/CAS and Git/working-tree materializer exist but have no sealed
  `SourceSnapshot` UoW, mount/pin, Scope Inventory, or Start-ready Contract;
  deterministic scanning remains unavailable.
- Restricted Artifact metadata and content now have the ADR-0005 access and descriptor
  foundation. Authenticated Runner upload, atomic Audit aggregate byte limits,
  Snapshot/CAS producers, and the final restricted WebUI cache lifecycle remain
  deliberately unavailable.
- Artifact integrity assumes the private state root and its service identity are not
  shared with hostile scanner, target, model-tool, or content-sandbox writers. A
  deployment that cannot maintain that separation must disable the capability.
- PostgreSQL remains a contract-tested future runtime, not a supported deployment;
  the current persistence concurrency evidence is authoritative for SQLite only, and
  the Project natural-key gap race still requires a real PostgreSQL barrier test.
- Ordinary Audit/Run-scoped Code Audit execution remains fenced and its Runner
  enqueue remains zero. The only executable AUD-200 path is the dedicated
  `preflight_job_owner_v1` SourceIngest protocol; it grants no
  `AuditStaticEffectPlan` or `AuditExecutionPlan` authority.
- AUD-201 converts a completed Result into a durable, owner-bound Plan, atomically
  reserves it for Create v2, and rejects current Start attempts without effects. The
  frozen future proof/UoW contract does not broaden the current Plan into Snapshot,
  static-effect, dynamic-effect, or delivery authority.
- The completion review ran on macOS and did not execute the real local-Linux
  Docker descriptor/mount smoke. Production backend qualification remains a
  mandatory Linux release gate.
