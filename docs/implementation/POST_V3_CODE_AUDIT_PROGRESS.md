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
- Current task: `AUD-100 — RunKind`
- Next dependency: `M0` is complete; `AUD-100` is unblocked.

## Milestone Status

| Milestone | Status | Exit evidence |
| --- | --- | --- |
| M0 Contract and development guardrails | completed | AUD-000 through AUD-002, full test suite, independence boundary, and release gate passed |
| M1 Run kind, domain, and persistence | in_progress | AUD-100 is the current unblocked task |
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
| AUD-100 RunKind | pending |
| AUD-101 Audit domain | pending |
| AUD-102 ORM and repositories | pending |
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
- Commit: Introducing commit; hash will be backfilled by the AUD-100 ledger update.
- Known limitations:
  - The flag remains admission-only. Audit domain objects, API admission, source
    preflight, signed authorization, snapshots, and deterministic analysis begin in
    M1-M3.
  - Startup isolation is one defense layer; descriptor-safe snapshot traversal and
    per-run source authorization remain mandatory in M2.
- Next unblocked task: AUD-100.

## Design Deviations and ADRs

- `ADR-0001`: RiftX Code Audit is an independent reimplementation and does not claim
  a strict clean-room process. M0 proves the scanner contract and records local bundle
  evidence; the complete candidate build/SBOM matrix remains an M10 release gate.

## Current Risks

- The independence scanner is a bounded known-identity gate, not a substitute for the
  M10 SBOM, licensing, similarity, and human copyright review.
- Audit admission and execution are intentionally unavailable until M1-M3 add durable
  domain state, signed preflight, immutable snapshots, and the deterministic slice.
- All M1-M10 runtime and product capabilities remain pending.
