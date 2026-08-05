from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.artifacts import ArtifactContentSlice
from riftx.code import (
    CodeCall,
    CodeCallHierarchyResult,
    CodeDiagnostic,
    CodeDiagnosticsResult,
    CodeEntry,
    CodeListResult,
    CodeReadResult,
    CodeReference,
    CodeReferenceSearchResult,
    CodeSymbol,
    CodeSymbolSearchResult,
    GitCommitSummary,
    GitDiffResult,
    GitLogResult,
    GitStatusEntry,
    GitStatusResult,
)
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    Execution,
    ExecutorType,
)
from riftx.execution import ExecutionWaitResult, ExecutionWaitStatus
from riftx.runtime.control_tools import RuntimeControlToolService
from riftx.runtime.engine.agent_factory import RuntimeToolScope
from riftx.skills import ProgressiveSkillContextManager, SkillRegistry


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> object:
        self.rows.append((run_id, event_type, payload))
        return object()


class FakeTranscript:
    def __init__(self) -> None:
        self.rows: list[tuple[str, object]] = []

    async def append(self, session_id: str, draft: object) -> object:
        self.rows.append((session_id, draft))
        return object()


class FakeExecutions:
    def __init__(self, executions: list[Execution]) -> None:
        self.items = {execution.id: execution for execution in executions}
        self.wait_calls: list[tuple[str, dict[str, object]]] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.get_error: Exception | None = None

    async def get(self, execution_id: str) -> Execution:
        if self.get_error is not None:
            raise self.get_error
        return self.items[execution_id]

    async def wait(self, execution_id: str, **kwargs: object) -> ExecutionWaitResult:
        self.wait_calls.append((execution_id, kwargs))
        return ExecutionWaitResult(
            execution=self.items[execution_id],
            wait_status=ExecutionWaitStatus.WAIT_TIMEOUT,
            partial_output="bounded output",
            next_poll_after_seconds=10,
            stdout_cursor=14,
            stderr_cursor=0,
        )

    async def cancel(self, execution_id: str, reason: str | None = None) -> Execution:
        self.cancel_calls.append((execution_id, reason))
        return self.items[execution_id]


class FakeArtifacts:
    def __init__(self, artifacts: list[tuple[Artifact, Path]] = []) -> None:  # noqa: B006
        self.items = {artifact.id: (artifact, path) for artifact, path in artifacts}
        self.read_content_slice_calls: list[str] = []
        self.read_audit_content_slice_calls: list[str] = []

    async def get(self, artifact_id: str) -> Artifact:
        return self.items[artifact_id][0]

    async def read_content_slice(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactContentSlice:
        self.read_content_slice_calls.append(artifact_id)
        artifact, path = self.items[artifact_id]
        assert artifact.run_id == expected_run_id
        content = path.read_bytes()
        data = content[offset : offset + max_bytes]
        next_offset = offset + len(data)
        return ArtifactContentSlice(
            artifact=artifact,
            data=data,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= len(content),
        )

    async def read_audit_content_slice(
        self,
        artifact_id: str,
        *,
        audit_id: str,
        run_id: str,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactContentSlice:
        self.read_audit_content_slice_calls.append(artifact_id)
        artifact, path = self.items[artifact_id]
        assert artifact.audit_id == audit_id
        assert artifact.run_id == run_id
        content = path.read_bytes()
        data = content[offset : offset + max_bytes]
        next_offset = offset + len(data)
        return ArtifactContentSlice(
            artifact=artifact,
            data=data,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= len(content),
        )


class FakeCode:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def list_files(self, run_id: str, **kwargs: object) -> CodeListResult:
        self.calls.append(("list_files", run_id, kwargs))
        return CodeListResult(
            source="workspace",
            path=str(kwargs.get("path") or ""),
            entries=[CodeEntry(path="src/app.py", type="file", size=7)],
        )

    async def read_file(self, run_id: str, **kwargs: object) -> CodeReadResult:
        self.calls.append(("read_file", run_id, kwargs))
        return CodeReadResult(
            source="workspace",
            path=str(kwargs["path"]),
            size=7,
            offset=int(kwargs["offset"]),
            next_offset=7,
            eof=True,
            encoding="utf-8",
            content="content",
        )

    async def symbol_search(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodeSymbolSearchResult:
        self.calls.append(("symbol_search", run_id, kwargs))
        return CodeSymbolSearchResult(
            source="workspace",
            query=str(kwargs["query"]),
            symbols=[
                CodeSymbol(
                    name="Handler",
                    qualified_name="Handler",
                    kind="class",
                    language="python",
                    path="src/app.py",
                    line_number=1,
                    column=0,
                    signature="class Handler:",
                )
            ],
            files_scanned=1,
            bytes_scanned=14,
            skipped_binary_files=0,
            skipped_large_files=0,
            skipped_unsupported_files=0,
            parse_errors=0,
        )

    async def find_references(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodeReferenceSearchResult:
        self.calls.append(("find_references", run_id, kwargs))
        return CodeReferenceSearchResult(
            source="workspace",
            symbol=str(kwargs["symbol"]),
            resolution="unique",
            definitions_found=1,
            references=[
                CodeReference(
                    kind="definition",
                    language="python",
                    path="src/app.py",
                    line_number=1,
                    column=6,
                    excerpt="class Handler:",
                )
            ],
            files_scanned=1,
            bytes_scanned=14,
            skipped_binary_files=0,
            skipped_large_files=0,
            skipped_unsupported_files=0,
            parse_errors=0,
        )

    async def call_hierarchy(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodeCallHierarchyResult:
        self.calls.append(("call_hierarchy", run_id, kwargs))
        return CodeCallHierarchyResult(
            source="workspace",
            symbol=str(kwargs["symbol"]),
            direction=str(kwargs["direction"]),  # type: ignore[arg-type]
            resolution="unique",
            definitions_found=1,
            analysis_modes=["python_ast"],
            calls=[
                CodeCall(
                    caller="caller",
                    callee="Handler",
                    confidence="python_ast",
                    language="python",
                    path="src/app.py",
                    line_number=4,
                    column=4,
                    excerpt="    Handler()",
                )
            ],
            files_scanned=1,
            bytes_scanned=32,
            skipped_binary_files=0,
            skipped_large_files=0,
            skipped_unsupported_files=0,
            parse_errors=0,
        )

    async def diagnostics(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodeDiagnosticsResult:
        self.calls.append(("diagnostics", run_id, kwargs))
        return CodeDiagnosticsResult(
            source="workspace",
            analysis_modes=["python_ast"],
            diagnostics=[
                CodeDiagnostic(
                    severity="error",
                    confidence="python_ast",
                    code="python_syntax_error",
                    message="invalid syntax",
                    language="python",
                    path="src/app.py",
                    line_number=1,
                    column=4,
                    excerpt="def broken(:",
                )
            ],
            files_scanned=1,
            bytes_scanned=13,
            skipped_binary_files=0,
            skipped_large_files=0,
            skipped_unsupported_files=0,
            parse_errors=1,
        )


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def status(self, run_id: str, **kwargs: object) -> GitStatusResult:
        self.calls.append(("git_status", run_id, kwargs))
        return GitStatusResult(
            branch="main",
            entries=[
                GitStatusEntry(
                    path="src/app.py",
                    index_status=" ",
                    worktree_status="M",
                )
            ],
        )

    async def diff(self, run_id: str, **kwargs: object) -> GitDiffResult:
        self.calls.append(("git_diff", run_id, kwargs))
        return GitDiffResult(
            staged=bool(kwargs["staged"]),
            path=kwargs["path"],  # type: ignore[arg-type]
            content="diff",
            bytes_returned=4,
        )

    async def log(self, run_id: str, **kwargs: object) -> GitLogResult:
        self.calls.append(("git_log", run_id, kwargs))
        return GitLogResult(
            path=kwargs["path"],  # type: ignore[arg-type]
            commits=[
                GitCommitSummary(
                    commit="1" * 40,
                    parents=[],
                    authored_at="2026-08-05T00:00:00+00:00",
                    author="RiftX",
                    subject="commit",
                )
            ],
        )

def execution(
    execution_id: str = "execution-1",
    *,
    run_id: str = "run-1",
    session_id: str | None = "session-1",
) -> Execution:
    return Execution(
        id=execution_id,
        execution_key=f"key-{execution_id}",
        run_id=run_id,
        session_id=session_id,
        tool_call_id=f"call-{execution_id}",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        cwd="/workspace",
        stdout_path=f"/tmp/{execution_id}.stdout",
        stderr_path=f"/tmp/{execution_id}.stderr",
    )


def service(
    *,
    executions: FakeExecutions | None = None,
    artifacts: FakeArtifacts | None = None,
    skills: ProgressiveSkillContextManager | None = None,
    code: FakeCode | None = None,
    git: FakeGit | None = None,
) -> tuple[RuntimeControlToolService, FakeEvents, FakeTranscript, FakeExecutions]:
    events = FakeEvents()
    transcript = FakeTranscript()
    execution_service = executions or FakeExecutions([execution()])
    control = RuntimeControlToolService(
        tools=object(),  # type: ignore[arg-type]
        executions=execution_service,  # type: ignore[arg-type]
        artifacts=artifacts or FakeArtifacts(),  # type: ignore[arg-type]
        events=events,
        transcript=transcript,  # type: ignore[arg-type]
        skills=skills,
        code=code,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
    )
    return control, events, transcript, execution_service


SCOPE = RuntimeToolScope(run_id="run-1", session_id="session-1", agent_id="primary")


async def test_execution_controls_are_owned_by_exact_agent_session() -> None:
    sibling = execution("execution-child", session_id="session-child")
    control, events, transcript, execution_service = service(executions=FakeExecutions([sibling]))

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "cancel_execution",
            {"execution_id": sibling.id, "reason": "do not cross session"},
            "call-1",
        )

    assert captured.value.code == "execution_scope_mismatch"
    assert execution_service.cancel_calls == []
    assert transcript.rows == []
    assert events.rows[-1][1:] == (
        "runtime.control_tool_failed",
        {
            "session_id": "session-1",
            "agent_id": "primary",
            "tool": "cancel_execution",
            "tool_call_id": "call-1",
            "error_type": "ApplicationConflictError",
            "error_code": "execution_scope_mismatch",
        },
    )


async def test_unowned_legacy_execution_is_not_visible_to_agent_session() -> None:
    legacy = execution("execution-unowned", session_id=None)
    control, _, _, _ = service(executions=FakeExecutions([legacy]))

    with pytest.raises(ApplicationConflictError, match="Agent Session"):
        await control(
            SCOPE,
            "get_execution",
            {"execution_id": legacy.id},
            "call-legacy",
        )


async def test_failed_audit_does_not_copy_exception_or_argument_secrets() -> None:
    secret = "Bearer top-secret-model-token"
    execution_service = FakeExecutions([execution()])
    execution_service.get_error = RuntimeError(f"upstream body included {secret}")
    control, events, _, _ = service(executions=execution_service)

    with pytest.raises(RuntimeError, match="upstream body"):
        await control(
            SCOPE,
            "get_execution",
            {"execution_id": secret},
            "call-secret",
        )

    failed_payload = events.rows[-1][2]
    assert failed_payload["error_code"] == "internal_error"
    assert secret not in json.dumps(failed_payload)
    assert "message" not in failed_payload


async def test_cancel_reason_is_bounded_before_execution_mutation() -> None:
    control, events, _, execution_service = service()

    with pytest.raises(ValidationError):
        await control(
            SCOPE,
            "cancel_execution",
            {"execution_id": "execution-1", "reason": "x" * 1001},
            "call-long-reason",
        )

    assert execution_service.cancel_calls == []
    assert events.rows[-1][2]["error_type"] == "ValidationError"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "wait_execution",
            {"execution_id": "execution-1", "timeout_seconds": 30.01},
        ),
        (
            "wait_execution",
            {"execution_id": "execution-1", "max_bytes": 64 * 1024 + 1},
        ),
        (
            "read_artifact",
            {"artifact_id": "artifact-1", "max_bytes": 64 * 1024 + 1},
        ),
    ],
)
async def test_wait_and_artifact_reads_reject_unbounded_arguments(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    control, _, _, execution_service = service()

    with pytest.raises(ValidationError):
        await control(SCOPE, tool_name, arguments, "call-unbounded")

    assert execution_service.wait_calls == []


async def test_same_run_artifact_is_shareable_but_cross_run_artifact_is_denied(
    tmp_path: Path,
) -> None:
    content = b"immutable shared evidence"
    path = tmp_path / "evidence.txt"
    path.write_bytes(content)
    same_run = Artifact(
        id="artifact-same-run",
        run_id="run-1",
        name=path.name,
        path=str(path),
        mime_type="text/plain",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    other_run = same_run.model_copy(update={"id": "artifact-other-run", "run_id": "run-2"})
    artifact_service = FakeArtifacts([(same_run, path), (other_run, path)])
    control, _, transcript, _ = service(artifacts=artifact_service)

    result = await control(
        SCOPE,
        "read_artifact",
        {"artifact_id": same_run.id, "max_bytes": 9},
        "call-artifact",
    )

    assert result["content"] == "immutable"
    assert result["next_offset"] == 9
    assert result["eof"] is False
    assert transcript.rows[0][0] == "session-1"

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "read_artifact",
            {"artifact_id": other_run.id},
            "call-other-run",
        )
    assert captured.value.code == "artifact_run_mismatch"
    assert artifact_service.read_content_slice_calls == [same_run.id]


async def test_same_run_audit_artifact_uses_owner_bound_content_read(tmp_path: Path) -> None:
    content = b"audit source continuation"
    path = tmp_path / "source.bin"
    path.write_bytes(content)
    artifact = Artifact(
        id="artifact-audit-source",
        run_id="run-1",
        audit_id="audit-1",
        access_class=ArtifactAccessClass.AUDIT_INTERNAL,
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.CONTROL_PLANE_BYTES,
            producer_node_id="node-1",
        ),
        name=path.name,
        path=str(path),
        mime_type="application/octet-stream",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    artifact_service = FakeArtifacts([(artifact, path)])
    control, _, _, _ = service(artifacts=artifact_service)

    result = await control(
        SCOPE,
        "read_artifact",
        {"artifact_id": artifact.id, "max_bytes": 5},
        "call-audit-artifact",
    )

    assert result["content"] == "audit"
    assert artifact_service.read_audit_content_slice_calls == [artifact.id]
    assert artifact_service.read_content_slice_calls == []

    restricted = artifact.model_copy(
        update={
            "id": "artifact-restricted",
            "access_class": ArtifactAccessClass.RESTRICTED_SENSITIVE,
        }
    )
    artifact_service.items[restricted.id] = (restricted, path)
    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "read_artifact",
            {"artifact_id": restricted.id},
            "call-restricted-artifact",
        )
    assert captured.value.code == "artifact_access_denied"
    assert artifact_service.read_audit_content_slice_calls == [artifact.id]


async def test_successful_control_result_is_transcripted_and_digest_audited() -> None:
    control, events, transcript, _ = service()

    result = await control(
        SCOPE,
        "get_execution",
        {"execution_id": "execution-1"},
        "call-get",
    )

    assert result["id"] == "execution-1"
    assert [event_type for _, event_type, _ in events.rows] == [
        "runtime.control_tool_started",
        "runtime.control_tool_completed",
    ]
    completed = events.rows[-1][2]
    assert completed["result_bytes"] > 0
    assert len(str(completed["result_sha256"])) == 64
    session_id, draft = transcript.rows[0]
    assert session_id == "session-1"
    assert draft.tool_call_id == "call-get"
    assert draft.execution_id == "execution-1"
    assert draft.structured_content["status"] == "completed"


async def test_complete_run_records_bounded_completion_request() -> None:
    control, events, _, _ = service()

    result = await control(
        SCOPE,
        "complete_run",
        {"run_summary": "Authorized objective completed."},
        "call-complete",
    )

    assert result == {
        "completion_requested": True,
        "run_summary": "Authorized objective completed.",
    }
    assert [event_type for _, event_type, _ in events.rows] == [
        "runtime.control_tool_started",
        "agent.completion_requested",
        "runtime.control_tool_completed",
    ]


async def test_progressive_skill_control_tools_select_reference_and_unload(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "skills" / "http-validation"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        """---
name: http-validation
description: Validate bounded HTTP behavior
version: 1.0.0
source: operator
---
## When to use
Use for HTTP candidates.
## Preconditions
Authorized target.
## Procedure
Send one bounded request.
## Decision points
Compare the response.
## Stop conditions
Stop after proof.
## Expected output
Evidence references.
## Error handling
Preserve errors.
"""
    )
    (directory / "REFERENCES.md").write_text("HTTP REFERENCE")
    skills = ProgressiveSkillContextManager(SkillRegistry(tmp_path / "skills"))
    control, _, transcript, _ = service(skills=skills)

    search = await control(
        SCOPE,
        "search_skills",
        {"query": "HTTP"},
        "call-search-skill",
    )
    loaded = await control(
        SCOPE,
        "load_skill",
        {"skill_id": "http-validation", "reason": "verify the HTTP candidate"},
        "call-load-skill",
    )
    reference = await control(
        SCOPE,
        "load_skill_references",
        {"skill_id": "http-validation"},
        "call-load-reference",
    )
    unloaded = await control(
        SCOPE,
        "unload_skill",
        {"skill_id": "http-validation"},
        "call-unload-skill",
    )

    assert search[0]["skill"]["id"] == "http-validation"
    assert loaded["version"] == "1.0.0"
    assert len(loaded["digest"]) == 64
    assert reference["content"] == "HTTP REFERENCE"
    assert unloaded == {"skill_id": "http-validation", "active": False}
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["runtime-tool://search_skills"],
        ["skill://http-validation"],
        ["skill://http-validation"],
        ["skill://http-validation"],
    ]


async def test_native_code_control_tools_use_exact_run_scope_and_source_refs() -> None:
    code = FakeCode()
    control, _, transcript, _ = service(code=code)

    listed = await control(
        SCOPE,
        "list_files",
        {"path": "src", "recursive": True, "max_entries": 10},
        "call-list-files",
    )
    read = await control(
        SCOPE,
        "read_file",
        {"path": "src/app.py", "offset": 0, "max_bytes": 64},
        "call-read-file",
    )

    assert listed["entries"][0]["path"] == "src/app.py"
    assert read["content"] == "content"
    assert code.calls == [
        (
            "list_files",
            "run-1",
            {"path": "src", "recursive": True, "max_entries": 10},
        ),
        (
            "read_file",
            "run-1",
            {"path": "src/app.py", "offset": 0, "max_bytes": 64},
        ),
    ]
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["code://src"],
        ["code://src/app.py"],
    ]


async def test_native_symbol_search_uses_exact_run_scope() -> None:
    code = FakeCode()
    control, _, transcript, _ = service(code=code)

    result = await control(
        SCOPE,
        "symbol_search",
        {"query": "handler", "path": "src", "max_results": 10},
        "call-symbol-search",
    )

    assert result["backend"] == "builtin_static"
    assert result["symbols"][0]["qualified_name"] == "Handler"
    assert code.calls == [
        (
            "symbol_search",
            "run-1",
            {
                "query": "handler",
                "path": "src",
                "file_glob": None,
                "case_sensitive": False,
                "max_results": 10,
            },
        )
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == ["code://src"]


async def test_native_find_references_uses_exact_run_scope() -> None:
    code = FakeCode()
    control, _, transcript, _ = service(code=code)

    result = await control(
        SCOPE,
        "find_references",
        {
            "symbol": "Handler",
            "path": "src",
            "include_declarations": False,
            "max_results": 10,
        },
        "call-find-references",
    )

    assert result["backend"] == "builtin_static"
    assert result["resolution"] == "unique"
    assert code.calls == [
        (
            "find_references",
            "run-1",
            {
                "symbol": "Handler",
                "path": "src",
                "file_glob": None,
                "include_declarations": False,
                "max_results": 10,
            },
        )
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == ["code://src"]


async def test_native_call_hierarchy_uses_exact_run_scope() -> None:
    code = FakeCode()
    control, _, transcript, _ = service(code=code)

    result = await control(
        SCOPE,
        "call_hierarchy",
        {
            "symbol": "Handler",
            "direction": "incoming",
            "path": "src",
            "max_results": 10,
        },
        "call-call-hierarchy",
    )

    assert result["backend"] == "builtin_static"
    assert result["analysis_modes"] == ["python_ast"]
    assert code.calls == [
        (
            "call_hierarchy",
            "run-1",
            {
                "symbol": "Handler",
                "direction": "incoming",
                "path": "src",
                "file_glob": None,
                "max_results": 10,
            },
        )
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == ["code://src"]


async def test_native_diagnostics_uses_exact_run_scope() -> None:
    code = FakeCode()
    control, _, transcript, _ = service(code=code)

    result = await control(
        SCOPE,
        "diagnostics",
        {"path": "src", "file_glob": "*.py", "max_results": 10},
        "call-diagnostics",
    )

    assert result["backend"] == "builtin_static"
    assert result["diagnostics"][0]["code"] == "python_syntax_error"
    assert code.calls == [
        (
            "diagnostics",
            "run-1",
            {"path": "src", "file_glob": "*.py", "max_results": 10},
        )
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == ["code://src"]


async def test_native_code_argument_limits_fail_before_source_read() -> None:
    code = FakeCode()
    control, _, _, _ = service(code=code)

    with pytest.raises(ValidationError):
        await control(
            SCOPE,
            "read_file",
            {"path": "src/app.py", "max_bytes": 64 * 1024 + 1},
            "call-unbounded-code-read",
        )
    with pytest.raises(ValidationError):
        await control(
            SCOPE,
            "call_hierarchy",
            {"symbol": "Handler", "direction": "sideways"},
            "call-invalid-direction",
        )

    assert code.calls == []


async def test_native_git_control_tools_use_exact_run_scope() -> None:
    git = FakeGit()
    control, _, transcript, _ = service(git=git)

    status = await control(
        SCOPE,
        "git_status",
        {"max_entries": 10},
        "call-git-status",
    )
    diff = await control(
        SCOPE,
        "git_diff",
        {"path": "src/app.py", "staged": False, "context_lines": 2, "max_bytes": 64},
        "call-git-diff",
    )
    history = await control(
        SCOPE,
        "git_log",
        {"path": "src/app.py", "max_entries": 5},
        "call-git-log",
    )

    assert status["entries"][0]["path"] == "src/app.py"
    assert diff["content"] == "diff"
    assert history["commits"][0]["subject"] == "commit"
    assert [call[:2] for call in git.calls] == [
        ("git_status", "run-1"),
        ("git_diff", "run-1"),
        ("git_log", "run-1"),
    ]
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["runtime-tool://git_status"],
        ["code://src/app.py"],
        ["code://src/app.py"],
    ]


async def test_native_git_argument_limits_fail_before_repository_read() -> None:
    git = FakeGit()
    control, _, _, _ = service(git=git)

    with pytest.raises(ValidationError):
        await control(
            SCOPE,
            "git_diff",
            {"context_lines": 21},
            "call-unbounded-git-diff",
        )

    assert git.calls == []
