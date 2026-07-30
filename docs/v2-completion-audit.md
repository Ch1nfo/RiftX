# RiftX V2 Completion Audit

This audit maps `RIFTX_V2_DESIGN.md` to the implementation and executable tests on branch
`codex/v2-rebuild`. The audit was completed after the end-to-end lifecycle test in commit
`2967e21`.

## Verification baseline

| Check | Result |
|---|---|
| Python lint | `ruff check src/riftx tests migrations` — passed |
| Python formatting | `ruff format --check src/riftx tests migrations` — passed |
| Python tests | `248 passed, 2 skipped` |
| Web typecheck | `pnpm --filter @riftx/web typecheck` — passed |
| Web tests | `20 passed` |
| Web production build | `pnpm --filter @riftx/web build` — passed |
| Installed CLI entry points | `riftx --help`, `riftx run --help`, `riftx tools --help`, and `riftx-runner --help` — passed in the `agent` conda environment |

The two skipped Python tests are host-specific smoke tests:

1. real Windows ConPTY requires Windows, `pywinpty`, and PowerShell 7;
2. real PowerShell execution requires PowerShell on the current host.

The portable request-building, lifecycle, error, and mocked Windows behavior remains covered by
`tests/runner/test_conpty.py` and `tests/runner/test_powershell.py`.

## Acceptance criteria

| Design requirement | Code evidence | Test evidence | Status |
|---|---|---|---|
| §6.4 Domain isolation, JSON models, guarded state transitions | `src/riftx/domain/` | `tests/unit/domain/test_dependencies.py`, `test_models.py`, `test_state_transitions.py` | Complete |
| §7.5 Shared Control Plane, persistence, resumable SSE, unified errors | `src/riftx/api/`, `src/riftx/application/services/`, `src/riftx/cli/client.py`, `apps/web/src/api/client.ts` | `test_api_restart_recovers_runs_from_sqlite`, `test_sse_resumes_from_last_event_id`, `test_unified_errors_and_temporal_outage`, Web API tests | Complete |
| §8.8 Durable Temporal runtime, retry idempotency, replay | `src/riftx/temporal/workflow.py`, `activities.py`, `runtime.py`, `worker_runtime.py` | `tests/unit/temporal/test_workflow.py`, `test_worker_runtime.py`, `test_interrupted_activity_retry_records_one_durable_approval` | Complete |
| §9.7 Dynamic Agent tools, re-planning, HITL resume, model independence, bounded context | `src/riftx/agent/`, `src/riftx/models/` | `tests/integration/agent/test_cycle.py`, `test_session.py`, `test_checkpoints.py`, `test_compact_context_activity_keeps_latest_messages` | Complete |
| §10 Model Provider profiles and failure classification | `src/riftx/models/config.py`, `provider.py`, per-Run `model_profile` wiring | `tests/unit/models/`, `test_agent_cycle_uses_run_model_profile` | Complete |
| §11.6 One-file Tool Registry, path/script commands, availability filtering, no installation, Web editing | `src/riftx/tools/`, `configs/tools.example.yaml`, `apps/web/src/pages/ToolsPage.tsx` | `tests/unit/tools/test_config.py`, `tests/integration/tools/test_registry.py`, `ToolsPage.test.tsx`, `test_tools_and_findings_share_persisted_control_plane` | Complete |
| §12.6 Generic Skills, machine formats, provenance, parser fallback | `src/riftx/skills/`, `src/riftx/tools/adapters.py` | `tests/integration/skills/test_generic_skills.py`, `tests/tools/test_adapters.py` and Nmap/Nuclei/Masscan golden files | Complete |
| §13.6 Standalone Runner, durable IDs/output, process-group cancellation, execution-key idempotency | `src/riftx/runner/`, `src/riftx/executors/` | `tests/runner/test_supervisor.py`, `test_executors.py`, `test_process_inspector.py`, `test_remote_control.py` | Complete |
| §14 Direct Process, explicit Shell, Unix PTY/Windows ConPTY, terminal ownership | `src/riftx/executors/process.py`, `shell.py`, `powershell.py`, `src/riftx/runner/unix_pty.py`, `conpty.py`, `terminal.py` | `tests/runner/test_executors.py`, `test_terminal.py`, `test_conpty.py`, `test_powershell.py`, terminal API/WebSocket tests | Complete; real Windows smoke is host-dependent |
| §15 Supervisor recovery, identity checks, byte cursors | `src/riftx/runner/supervisor.py`, `process_inspector.py`, `paths.py`, execution provenance fields | `test_recovery_marks_unidentifiable_active_execution_lost`, `test_supervisor_persists_lifecycle_and_reads_output_by_cursor`, `test_execution_api_exposes_provenance_and_cursor_output` | Complete |
| §16 AUTO/BALANCED/MANUAL approval and exact decision context | `src/riftx/domain/approval.py`, `src/riftx/application/services/approvals.py`, Agent HITL checkpoint flow | approval policy tests, Agent interruption/resume tests, API durable approval tests, CLI/Web approval tests | Complete |
| §17 Lightweight Scope guard and structured-skill enforcement | `src/riftx/scope/guard.py`, scoped Agent tools | `tests/unit/test_scope_guard.py`, `tests/integration/agent/test_cycle.py` | Complete |
| §18 SQLAlchemy repositories, SQLite, Alembic, no SQL in routes/Agent/Workflow | `src/riftx/persistence/`, `migrations/` and repository ports | persistence mapper/repository/migration/schema suites | Complete |
| §19 Durable event-first timeline and SSE projection | `RunEventRepository`, application services, `src/riftx/api/routes/events.py` | event sequence tests, SSE resume tests, complete lifecycle E2E event-order assertion | Complete |
| §20 Dashboard, New Run, Run Detail tabs, Tools, Nodes, xterm/WebSocket terminal | `apps/web/src/pages/`, `TerminalPanel.tsx`, TanStack Query hooks | all eight Web test files; 20 tests and production build | Complete |
| §21 Typer commands, prompt-toolkit interactive mode, Rich rendering, API-only client behavior | `src/riftx/cli/` | `tests/unit/cli/`; installed entry-point smoke commands | Complete |
| §22 Immutable Artifact hashes, editable Findings/evidence, restricted Markdown/HTML/JSON reports | artifact/finding/report domain, services, routes, and Web editors | artifact tamper/restart tests, finding validation/edit tests, report evidence-link tests, complete lifecycle E2E | Complete |
| §23 Layered configuration and external secrets | `src/riftx/config.py`, `configs/riftx.example.yaml`, model secret references | `tests/unit/test_runtime_config.py`, `tests/unit/models/test_config.py` | Complete |
| §25 Local, persistent local, and team/remote execution modes | production API/Worker commands, SQLite paths, authenticated Runner control channel | worker assembly tests, API restart test, remote Runner/terminal integration tests | Complete |
| §26 macOS/Linux/Windows interfaces | platform-aware executors, PowerShell discovery, ConPTY backend, remote Runner | cross-platform unit/integration tests | Complete; real Windows smoke must run on Windows |

## Milestone audit

| Milestone | Evidence | Status |
|---|---|---|
| V2-M1 Domain and Persistence | commits `55bb686`, `e829a83`, `3e42c0a`; repository and restart tests | Complete |
| V2-M2 Host Runner | commits `2dbc98f`, `9c7feff`; process, timeout, output, cancellation, recovery tests | Complete |
| V2-M3 Tool and Skill Registry | commits `d449df9`, `9c20707`; hot reload, custom scripts, unavailable filtering tests | Complete |
| V2-M4 Agent Harness | commits `e80fe20`, `6fa5d83`, `6c15af8`, `3046845`; deterministic Agent integration tests | Complete |
| V2-M5 Temporal integration | commits `f4d6a4e`, `385d37f`, `02de367`; restart/replay/retry/signal tests | Complete |
| V2-M6 WebUI and CLI | commits `2fc6a19`, `7791c32`, `7be387f`, `55f0899`, `f5c66b0`; Web and CLI suites | Complete |
| V2-M7 Approval and PTY | commits `bcd0da0`, `fe146af`; HITL, takeover/release, resize, Ctrl+C, transcript tests | Complete |
| V2-M8 Finding, Artifact, Report | commits `2ad80cd`, `f64f0ad`, `d3b057b`; evidence and report tests | Complete |
| V2-M9 Remote Runner and Windows | commits `3ea27d9`, `0c28cb8`, `1339e7d`, `09e78ac`; remote channel/terminal and Windows backend tests | Complete; real Windows smoke is host-dependent |

## Test-plan audit

| Test-plan section | Evidence | Status |
|---|---|---|
| §28.1 Unit tests | Domain, Tool config, Skill selection, argv, Scope, Approval, truncation suites | Complete |
| §28.2 Runner integration | deterministic success/failure/sleep/stream/large/child fixtures plus PTY tests | Complete |
| §28.3 Temporal tests | pause/resume, signals, retry, cancellation, replay, approval wait, stable execution keys | Complete |
| §28.4 Adapter golden tests | Nmap XML, Nuclei JSONL, Masscan JSON samples under `tests/tools/golden/` | Complete |
| §28.5 Full E2E | `test_complete_agent_runner_sse_finding_report_lifecycle` covers API Run creation → deterministic Agent Tool selection → real ProcessSupervisor execution → SSE → Finding → completion → three report formats | Complete |

## Risk-control audit

| Risk | Implemented control |
|---|---|
| §29.1 Host permissions | Host execution provenance is visible in API/WebUI; approval modes and exact command review are implemented; no sandbox claim is made. |
| §29.2 Environment reproducibility | Node OS/architecture/shell/cwd, Tool version/path, execution environment diff, and platform provenance are persisted and displayed. |
| §29.3 Raw Shell bypass | `registered_only` is supported for operators that require registry-only execution; open Shell is an explicit policy choice and remains approval-aware. |
| §29.4 Oversized Agent Activity | One bounded Agent Cycle per Activity with heartbeats and durable outer Workflow. |
| §29.5 PTY recovery limits | ordinary processes recover; unrecoverable native PTYs become `LOST`; no false full-persistence guarantee. |
| §29.6 Large output | full stdout/stderr remain files; Agent receives bounded excerpts; cursor APIs and context trimming avoid repeated full reads. |
| §29.7 Temporal complexity | separate `riftx worker` command, documented configuration, unavailable-Temporal API fallback, worker assembly tests. |

## Explicit V2 boundaries

The implementation intentionally retains the exclusions in §30:

- RiftX does not install or distribute external penetration-testing tools.
- It does not claim container/VM sandboxing or full shell semantic enforcement.
- Native PTY sessions are not promised to survive Runner crashes.
- PDF/DOCX export, multi-tenant RBAC, marketplace/update systems, and visual workflow builders are outside V2.
- The optional `tmux` PTY backend is not required for the first V2 release and is not implemented.

External security tools are therefore represented in automated tests by deterministic fixtures, as
required by §28.2, rather than by invoking unbundled tools such as Nmap or Metasploit.
