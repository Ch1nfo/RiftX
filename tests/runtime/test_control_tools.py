from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError
from tests.integration.persistence.test_capability_repository import version

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    QueryReasoningGraph,
    ReasoningGraphQueryResult,
)
from riftx.application.services.artifacts import ArtifactContentSlice
from riftx.application.traffic import TrafficStatusClass
from riftx.browser.service import ActBrowser, BrowserView, OpenBrowser
from riftx.capabilities import CapabilityKind, CapabilityVersion, TechniqueContextManager
from riftx.code import (
    CodeCall,
    CodeCallHierarchyResult,
    CodeDiagnostic,
    CodeDiagnosticsResult,
    CodeEntry,
    CodeListResult,
    CodePatchResult,
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
    GitWorktreeResult,
)
from riftx.context import AttemptRecord, PlanUpdateProposal, WorkingMemory
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    BrowserAction,
    BrowserActionStatus,
    BrowserMode,
    BrowserObservation,
    BrowserPage,
    BrowserSession,
    BrowserSessionStatus,
    Execution,
    ExecutorType,
    FormFieldSummary,
    FormSummary,
    InteractiveElement,
    NetworkEventSummary,
    Objective,
    Run,
    RunKind,
    Scope,
)
from riftx.domain.base import utc_now
from riftx.execution import ExecutionWaitResult, ExecutionWaitStatus
from riftx.mcp import MCPInvocationResult
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningEdge,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
)
from riftx.runtime.control_tools import RuntimeControlToolService
from riftx.runtime.engine.agent_factory import RuntimeToolScope
from riftx.skills import ProgressiveSkillContextManager, SkillRegistry
from riftx.target_http import TargetHttpResult, TargetHttpSubmission
from riftx.tasks import (
    AddTaskCommand,
    ClaimReadyTaskCommand,
    CompleteTaskCommand,
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskMutationResult,
)
from riftx.tools import ToolContextManager, ToolRegistry
from riftx.web import (
    EvidenceSpan,
    FetchRequest,
    FetchResult,
    FetchResultStatus,
    ResearchClaim,
    ResearchRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceReference,
    WebDocument,
    WebDocumentChunk,
    WebResearchPacket,
)


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


class FakeTaskPlanner:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.task = Task(id="task-1", run_id="run-1", sequence=1, title="Discover")
        self.attempt = TaskAttempt(
            id="attempt-1",
            run_id="run-1",
            task_id="task-1",
            sequence=1,
            status=TaskAttemptStatus.RUNNING,
            session_id="session-1",
            worker_id="worker-1",
            started_at=utc_now(),
        )

    async def list_ready(self, run_id: str, *, limit: int = 100) -> tuple[Task, ...]:
        self.calls.append(("list_ready", run_id, limit))
        return (self.task,)

    async def add_task(self, command: AddTaskCommand) -> TaskMutationResult:
        self.calls.append(command)
        return TaskMutationResult(graph_version=1, task=self.task)

    async def claim_ready_task(
        self,
        command: ClaimReadyTaskCommand,
    ) -> TaskMutationResult | None:
        self.calls.append(command)
        return TaskMutationResult(graph_version=2, task=self.task, attempt=self.attempt)

    async def complete_task(self, command: CompleteTaskCommand) -> TaskMutationResult:
        self.calls.append(command)
        return TaskMutationResult(graph_version=3, task=self.task, attempt=self.attempt)


class FakeWorkingMemoryProposals:
    def __init__(self) -> None:
        self.plan_calls: list[tuple[str, int, PlanUpdateProposal]] = []
        self.attempt_calls: list[tuple[str, int, AttemptRecord]] = []

    async def propose_plan_update(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        proposal: PlanUpdateProposal,
    ) -> WorkingMemory:
        self.plan_calls.append((run_id, expected_memory_version, proposal))
        return WorkingMemory(run_id=run_id, version=max(1, expected_memory_version + 1))

    async def record_attempt(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        attempt: AttemptRecord,
    ) -> WorkingMemory:
        self.attempt_calls.append((run_id, expected_memory_version, attempt))
        return WorkingMemory(
            run_id=run_id,
            version=max(1, expected_memory_version + 1),
            attempts=[attempt],
        )


class FakeReasoningProposals:
    def __init__(self) -> None:
        self.graph: ReasoningGraph | None = None
        self.queries: list[QueryReasoningGraph] = []

    async def create_node(
        self,
        node: ReasoningNode,
        *,
        expected_graph_version: int,
    ) -> ReasoningGraph:
        if self.graph is None:
            assert expected_graph_version == 0
            self.graph = ReasoningGraph(run_id=node.run_id, nodes=[node])
        else:
            assert expected_graph_version == self.graph.version
            self.graph = self.graph.model_copy(
                update={
                    "version": self.graph.version + 1,
                    "nodes": [*self.graph.nodes, node],
                }
            )
        return self.graph

    async def record_negative_result(
        self,
        negative_result: ReasoningNode,
        *,
        invalidated_node_id: str,
        expected_graph_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph:
        assert self.graph is not None
        assert expected_graph_version == self.graph.version
        edge = ReasoningEdge(
            id=edge_id or "negative-edge",
            run_id=negative_result.run_id,
            source_node_id=negative_result.id,
            target_node_id=invalidated_node_id,
            relation_type=ReasoningRelationType.INVALIDATES,
            evidence_ids=negative_result.evidence_ids,
            creator_type=ReasoningCreatorType.REDUCER,
            created_by="reasoning-reducer",
        )
        self.graph = ReasoningGraph(
            run_id=self.graph.run_id,
            version=self.graph.version + 1,
            nodes=[*self.graph.nodes, negative_result],
            edges=[*self.graph.edges, edge],
            created_at=self.graph.created_at,
        )
        return self.graph

    async def query(self, command: QueryReasoningGraph) -> ReasoningGraphQueryResult:
        self.queries.append(command)
        graph = self.graph
        return ReasoningGraphQueryResult(
            run_id=command.run_id,
            graph_version=graph.version if graph is not None else 0,
            total_matching_nodes=len(graph.nodes) if graph is not None else 0,
            offset=command.offset,
            nodes=tuple(graph.nodes) if graph is not None else (),
            edges=tuple(graph.edges) if graph is not None else (),
        )


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

    async def apply_patch(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodePatchResult:
        self.calls.append(("apply_patch", run_id, kwargs))
        return CodePatchResult(
            action="applied",
            operation="update",
            path="src/app.py",
            original_sha256="1" * 64,
            result_sha256="2" * 64,
            receipt_artifact_id="patch-receipt-1",
            diff="diff",
        )

    async def revert_patch(
        self,
        run_id: str,
        **kwargs: object,
    ) -> CodePatchResult:
        self.calls.append(("revert_patch", run_id, kwargs))
        return CodePatchResult(
            action="reverted",
            operation="update",
            path="src/app.py",
            original_sha256="2" * 64,
            result_sha256="1" * 64,
            receipt_artifact_id=str(kwargs["receipt_artifact_id"]),
            diff="reverse diff",
        )


class FakeControlIntents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.begin_arguments: list[dict[str, object]] = []

    async def begin_control_intent(self, **kwargs: object) -> object:
        self.begin_arguments.append(kwargs)
        self.calls.append(("begin", str(kwargs["engine_call_id"])))
        return SimpleNamespace(id=f"intent-{kwargs['engine_call_id']}")

    async def finish_control_intent(self, **kwargs: object) -> None:
        outcome = "success" if kwargs["succeeded"] else "failed"
        self.calls.append((outcome, str(kwargs["engine_call_id"])))


class FakeMCP:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search_tools(self, query: str, *, max_results: int) -> list[dict[str, object]]:
        return [{"id": "mcp__docs__read", "query": query, "max_results": max_results}]

    def get_tool(self, tool_id: str) -> dict[str, object]:
        return {"entry": {"id": tool_id}, "schema": {"tool_id": tool_id}}

    async def invoke(self, **kwargs: object) -> MCPInvocationResult:
        self.calls.append(kwargs)
        return MCPInvocationResult(
            tool_call_id=str(kwargs["tool_call_id"]),
            execution_key="execution:v1:mcp",
            tool_id=str(kwargs["tool_id"]),
            server_id="docs",
            tool_name="read_doc",
            status="completed",
            artifact_id="mcp-artifact-1",
            result_sha256="a" * 64,
            result_bytes=128,
            content=[{"type": "text", "text": "answer", "truncated": False}],
        )


class FakeTechniqueCatalog:
    def __init__(self, versions: list[CapabilityVersion]) -> None:
        self.versions = versions

    async def list_active_versions(
        self,
        kind: CapabilityKind,
    ) -> tuple[CapabilityVersion, ...]:
        assert kind is CapabilityKind.TECHNIQUE
        return tuple(self.versions)


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

    async def create_worktree(
        self,
        run_id: str,
        **kwargs: object,
    ) -> GitWorktreeResult:
        self.calls.append(("create_worktree", run_id, kwargs))
        return GitWorktreeResult(
            action="created",
            name=str(kwargs["name"]),
            path=".riftx-wt-owner-fix",
            head_commit="1" * 40,
        )


class FakeRuns:
    def __init__(self, *, kind: RunKind = RunKind.GENERAL) -> None:
        self.run = Run(
            id="run-1",
            kind=kind,
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Authorized Target HTTP test"),
            scope=Scope(domains=["target.internal"]),
            workspace_path="/workspace",
        )

    async def get(self, run_id: str) -> Run | None:
        return self.run if run_id == self.run.id else None


class _DumpPayload:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return self.payload


class FakeTraffic:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def list_for_runtime(self, run_id: str, **kwargs: object) -> _DumpPayload:
        self.calls.append(("list", run_id, kwargs))
        return _DumpPayload(
            {
                "scope": {"run_id": run_id, "engagement_id": "engagement-1"},
                "items": [{"exchange_id": "exchange-1", "method": "GET"}],
                "has_more": False,
                "next_cursor": None,
            }
        )

    async def get_for_runtime(self, run_id: str, exchange_id: str) -> _DumpPayload:
        self.calls.append(("get", run_id, {"exchange_id": exchange_id}))
        return _DumpPayload(
            {
                "scope": {"run_id": run_id, "engagement_id": "engagement-1"},
                "item": {
                    "exchange_id": exchange_id,
                    "method": "POST",
                    "url_summary": {"origin": "https://target.internal", "redacted": True},
                },
            }
        )


class FakeTargetHttp:
    def __init__(self) -> None:
        self.submissions: list[TargetHttpSubmission] = []
        self.result = TargetHttpResult(
            request_id="exchange-1",
            execution_key="existing-key",
            request_hash="1" * 64,
            status_code=200,
            reason_phrase="OK",
            elapsed_ms=12,
            content_type="application/json",
            content_length=12,
            body_excerpt='{"ok":true}',
            request_artifact_id="target-request-1",
            response_artifact_id="target-response-1",
            final_url="https://target.internal/private?token=redacted",
        )

    async def execute(self, submission: TargetHttpSubmission) -> TargetHttpResult:
        self.submissions.append(submission)
        return self.result.model_copy(
            update={
                "execution_key": submission.request.execution_key,
                "request_hash": submission.request.fingerprint,
            }
        )

    async def get_result(self, run_id: str, request_id: str) -> TargetHttpResult:
        assert run_id == "run-1"
        assert request_id == self.result.request_id
        return self.result


class FakeTargetArtifacts(FakeArtifacts):
    async def get(self, artifact_id: str) -> object:
        if artifact_id in {"target-request-1", "target-response-1"}:
            return SimpleNamespace(run_id="run-1")
        return await super().get(artifact_id)


class ForeignTargetArtifacts(FakeTargetArtifacts):
    async def get(self, artifact_id: str) -> object:
        artifact = await super().get(artifact_id)
        if artifact_id == "target-response-1":
            return SimpleNamespace(run_id="run-foreign")
        return artifact


class FakeBrowser:
    def __init__(self, *, agent_session_id: str = "session-1") -> None:
        self.calls: list[tuple[str, object]] = []
        self.session = BrowserSession(
            id="browser-1",
            run_id="run-1",
            agent_session_id=agent_session_id,
            node_id="local",
            mode=BrowserMode.MANAGED_EPHEMERAL,
            status=BrowserSessionStatus.ACTIVE,
            current_page_id="page-1",
            page_ids=["page-1"],
        )

    def _view(
        self,
        *,
        version: int,
        action: BrowserAction | None = None,
    ) -> BrowserView:
        return BrowserView(
            session=self.session,
            pages=[
                BrowserPage(
                    id="page-1",
                    browser_session_id=self.session.id,
                    url="https://example.test/",
                    title="Example",
                    last_observation_version=version,
                )
            ],
            observation=(
                BrowserObservation(
                    id=f"observation-{version}",
                    browser_session_id=self.session.id,
                    page_id="page-1",
                    url="https://example.test/",
                    title="Example",
                    visible_text_excerpt="Untrusted page content",
                    screenshot_artifact_id="screenshot-1",
                    network_artifact_id="network-1",
                    observation_version=version,
                )
                if self.session.status is BrowserSessionStatus.ACTIVE
                else None
            ),
            action=action,
        )

    async def open(self, command: OpenBrowser) -> BrowserView:
        self.calls.append(("open", command))
        return self._view(version=1)

    async def get(
        self,
        session_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> BrowserView:
        self.calls.append(("get", (session_id, expected_run_id)))
        assert session_id == self.session.id
        assert expected_run_id == self.session.run_id
        return self._view(version=1)

    async def observe(self, session_id: str, **kwargs: object) -> BrowserView:
        self.calls.append(("observe", (session_id, kwargs)))
        return self._view(version=2)

    async def act(self, session_id: str, command: ActBrowser) -> BrowserView:
        self.calls.append(("act", (session_id, command)))
        return self._view(
            version=2,
            action=BrowserAction(
                action_key=command.action_key,
                browser_session_id=session_id,
                page_id=command.page_id,
                observation_version=command.observation_version,
                action=command.action,
                element_ref=command.element_ref,
                value=command.value,
                url=command.url,
                options=command.options or {},
                status=BrowserActionStatus.COMPLETED,
                result_observation_id="observation-2",
            ),
        )

    async def close(self, session_id: str) -> BrowserView:
        self.calls.append(("close", session_id))
        self.session = self.session.model_copy(
            update={"status": BrowserSessionStatus.CLOSED}
        )
        return self._view(version=2)


class LargeFakeBrowser(FakeBrowser):
    def _view(
        self,
        *,
        version: int,
        action: BrowserAction | None = None,
    ) -> BrowserView:
        view = super()._view(version=version, action=action)
        assert view.observation is not None
        long_text = "x" * 8192
        long_url = f"https://example.test/{'x' * 8100}"
        fields = [
            FormFieldSummary(
                ref=f"field-{index}",
                name=long_text[:255],
                label=long_text[:1000],
                input_type=long_text[:128],
            )
            for index in range(100)
        ]
        observation = view.observation.model_copy(
            update={
                "headings": [long_text] * 100,
                "interactive_elements": [
                    InteractiveElement(
                        ref=f"e-{index + 1}",
                        role=long_text[:128],
                        name=long_text[:1000],
                        text=long_text[:1000],
                        input_type=long_text[:128],
                        href=long_url,
                        frame_id=long_text[:255],
                    )
                    for index in range(300)
                ],
                "forms": [
                    FormSummary(
                        ref=f"form-{index}",
                        action=long_url,
                        method="POST",
                        fields=fields,
                    )
                    for index in range(50)
                ],
                "alerts": [long_text] * 50,
                "console_errors": [long_text] * 100,
                "recent_network_summary": [
                    NetworkEventSummary(
                        sequence=index + 1,
                        method="GET",
                        url=long_url,
                        failed=True,
                        failure_text=long_text[:2000],
                    )
                    for index in range(100)
                ],
            }
        )
        return BrowserView(
            session=view.session,
            pages=view.pages,
            observation=observation,
            action=view.action,
        )


class FakeWebFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, FetchRequest]] = []

    async def fetch(self, run_id: str, request: FetchRequest) -> FetchResult:
        self.calls.append((run_id, request))
        content = "Untrusted public source content " + "x" * 20_000
        digest = hashlib.sha256(content.encode()).hexdigest()
        document = WebDocument(
            id="document-1",
            run_id=run_id,
            requested_url=str(request.url),
            final_url=str(request.url),
            fetched_at=utc_now(),
            mime_type="text/html",
            raw_artifact_id="web-raw-1",
            normalized_artifact_id="web-normalized-1",
            content_hash=digest,
            text_length=len(content),
            extraction_status="complete",
        )
        source = SourceReference(
            id="source-1",
            document_id=document.id,
            url=document.final_url,
            domain="example.test",
            fetched_at=document.fetched_at,
            content_hash=digest,
        )
        chunk = WebDocumentChunk(
            id="chunk-1",
            document_id=document.id,
            sequence=0,
            heading_path=["Public source"],
            content=content,
            token_count=5_000,
            start_offset=0,
            end_offset=len(content),
        )
        return FetchResult(
            status=FetchResultStatus.FETCHED,
            requested_url=document.requested_url,
            final_url=document.final_url,
            document=document,
            chunks=[chunk],
            source=source,
        )


class FakeWebResearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def search(
        self,
        run_id: str,
        session_id: str,
        model_profile: str,
        request: SearchRequest,
    ) -> SearchResponse:
        self.calls.append(("search", (run_id, session_id, model_profile, request)))
        query_id = "query-1"
        return SearchResponse(
            query_id=query_id,
            provider="fixture",
            request=request,
            artifact_id="search-artifact-1",
            results=[
                SearchResult(
                    id="candidate-1",
                    title="Advisory candidate",
                    url="https://example.test/advisory",
                    normalized_url="https://example.test/advisory",
                    domain="example.test",
                    snippet="untrusted candidate",
                    provider="fixture",
                    provider_rank=1,
                    search_query_id=query_id,
                )
            ],
        )

    async def research(
        self,
        request: ResearchRequest,
        *,
        model_profile: str,
    ) -> WebResearchPacket:
        self.calls.append(("research", (request, model_profile)))
        source = SourceReference(
            id="source-1",
            document_id="document-1",
            url="https://example.test/advisory",
            domain="example.test",
            fetched_at=utc_now(),
            content_hash="1" * 64,
        )
        span = EvidenceSpan(
            source_id=source.id,
            chunk_id="chunk-1",
            start_offset=0,
            end_offset=12,
            quote="fixed in 2.0",
        )
        return WebResearchPacket(
            id="packet-1",
            run_id=request.run_id,
            session_id=request.session_id,
            question=request.question,
            summary="The canonical source says it was fixed in 2.0.",
            key_claims=[
                ResearchClaim(
                    statement="Fixed in 2.0",
                    evidence=[span],
                    confidence=0.9,
                )
            ],
            sources=[source],
            search_query_ids=["query-1"],
            document_ids=["document-1"],
            artifact_ids=["raw-1", "normalized-1", "research-artifact-1"],
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
    tools: object | None = None,
    executions: FakeExecutions | None = None,
    artifacts: FakeArtifacts | None = None,
    skills: ProgressiveSkillContextManager | None = None,
    techniques: TechniqueContextManager | None = None,
    code: FakeCode | None = None,
    git: FakeGit | None = None,
    browser: FakeBrowser | None = None,
    web_fetcher: FakeWebFetcher | None = None,
    web_research: FakeWebResearch | None = None,
    runs: FakeRuns | None = None,
    traffic: FakeTraffic | None = None,
    target_http: FakeTargetHttp | None = None,
    mcp: FakeMCP | None = None,
    control_intents: FakeControlIntents | None = None,
    task_planner: FakeTaskPlanner | None = None,
    working_memory_proposals: FakeWorkingMemoryProposals | None = None,
    reasoning_proposals: FakeReasoningProposals | None = None,
    worker_id: str = "runtime",
) -> tuple[RuntimeControlToolService, FakeEvents, FakeTranscript, FakeExecutions]:
    events = FakeEvents()
    transcript = FakeTranscript()
    execution_service = executions or FakeExecutions([execution()])
    control = RuntimeControlToolService(
        tools=tools or object(),  # type: ignore[arg-type]
        executions=execution_service,  # type: ignore[arg-type]
        artifacts=artifacts or FakeArtifacts(),  # type: ignore[arg-type]
        events=events,
        transcript=transcript,  # type: ignore[arg-type]
        skills=skills,
        techniques=techniques,
        code=code,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        browser=browser,  # type: ignore[arg-type]
        web_fetcher=web_fetcher,  # type: ignore[arg-type]
        web_research=web_research,  # type: ignore[arg-type]
        runs=runs,  # type: ignore[arg-type]
        traffic=traffic,  # type: ignore[arg-type]
        target_http=target_http,  # type: ignore[arg-type]
        mcp=mcp,  # type: ignore[arg-type]
        control_intents=control_intents,  # type: ignore[arg-type]
        task_planner=task_planner,  # type: ignore[arg-type]
        working_memory_proposals=working_memory_proposals,
        reasoning_proposals=reasoning_proposals,
        worker_id=worker_id,
    )
    return control, events, transcript, execution_service


SCOPE = RuntimeToolScope(
    run_id="run-1",
    session_id="session-1",
    agent_id="primary",
    model_profile="test-profile",
)


async def test_task_planner_tools_bind_run_worker_and_session_identity() -> None:
    planner = FakeTaskPlanner()
    control, _, _, _ = service(task_planner=planner, worker_id="worker-1")

    ready = await control(SCOPE, "list_ready_tasks", {"limit": 5}, "ready-call")
    added = await control(
        SCOPE,
        "add_task",
        {
            "expected_graph_version": 0,
            "task_id": "task-1",
            "title": "Discover",
        },
        "add-call",
    )
    claimed = await control(
        SCOPE,
        "claim_ready_task",
        {"preferred_task_id": "task-1"},
        "claim-call",
    )
    completed = await control(
        SCOPE,
        "complete_task",
        {
            "expected_graph_version": 2,
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "completion_summary": "Done",
        },
        "complete-call",
    )

    assert ready[0]["id"] == "task-1"
    assert added["graph_version"] == 1
    assert claimed["attempt"]["id"] == "attempt-1"
    assert completed["graph_version"] == 3
    assert planner.calls[0] == ("list_ready", "run-1", 5)
    add_command = planner.calls[1]
    claim_command = planner.calls[2]
    complete_command = planner.calls[3]
    assert isinstance(add_command, AddTaskCommand) and add_command.run_id == "run-1"
    assert isinstance(claim_command, ClaimReadyTaskCommand)
    assert claim_command.worker_id == "worker-1"
    assert claim_command.session_id == "session-1"
    assert isinstance(complete_command, CompleteTaskCommand)
    assert complete_command.actor_session_id == "session-1"


async def test_subagent_cannot_change_task_graph_topology() -> None:
    planner = FakeTaskPlanner()
    control, _, _, _ = service(task_planner=planner)
    subagent_scope = RuntimeToolScope(
        run_id="run-1",
        session_id="session-subagent",
        agent_id="subagent",
        model_profile="test-profile",
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            subagent_scope,
            "add_task",
            {"expected_graph_version": 0, "title": "Forbidden"},
            "add-call",
        )

    assert captured.value.code == "task_planner_primary_required"
    assert planner.calls == []


async def test_primary_cognitive_tools_inject_scope_and_fixed_candidate_states() -> None:
    working_memory = FakeWorkingMemoryProposals()
    reasoning = FakeReasoningProposals()
    control, _, _, _ = service(
        working_memory_proposals=working_memory,
        reasoning_proposals=reasoning,
    )

    plan_result = await control(
        SCOPE,
        "propose_plan_update",
        {
            "expected_memory_version": 0,
            "current_focus": {
                "phase": "recon",
                "objective": "Inspect the authorized target",
            },
        },
        "plan-call",
    )
    attempt_result = await control(
        SCOPE,
        "record_attempt",
        {
            "expected_memory_version": 0,
            "attempt_id": "attempt-1",
            "action_signature": "http-probe:v1",
            "target": "https://192.0.2.10",
            "tool_id": "target_http_request",
            "result_status": "failed",
            "result_summary": "Connection was refused",
            "retryable": True,
        },
        "attempt-call",
    )
    graph_version = 0
    expected = [
        ("record_observation", "observation-1", ReasoningNodeKind.OBSERVATION),
        ("propose_fact", "fact-1", ReasoningNodeKind.FACT_CANDIDATE),
        ("propose_hypothesis", "hypothesis-1", ReasoningNodeKind.HYPOTHESIS),
        (
            "propose_finding",
            "finding-candidate-1",
            ReasoningNodeKind.VULNERABILITY_CANDIDATE,
        ),
    ]
    for tool_name, node_id, kind in expected:
        arguments: dict[str, object] = {
            "expected_graph_version": graph_version,
            "node_id": node_id,
            "task_id": "task-1",
            "claim": f"Claim for {node_id}",
        }
        if tool_name != "propose_hypothesis":
            arguments["evidence_ids"] = ["evidence-1"]
        result = await control(SCOPE, tool_name, arguments, f"{tool_name}-call")
        graph_version = result["graph_version"]
        assert result["node"]["kind"] == kind.value

    negative = await control(
        SCOPE,
        "record_negative_result",
        {
            "expected_graph_version": graph_version,
            "node_id": "negative-1",
            "task_id": "task-1",
            "claim": "The endpoint did not accept the tested input",
            "evidence_ids": ["evidence-2"],
            "invalidated_node_id": "observation-1",
        },
        "negative-call",
    )
    queried = await control(
        SCOPE,
        "query_reasoning_graph",
        {"kinds": ["observation"], "limit": 10},
        "query-call",
    )

    assert plan_result["accepted"] is True
    assert attempt_result["attempt_id"] == "attempt-1"
    assert negative["node"]["kind"] == "negative_result"
    assert queried["run_id"] == "run-1"
    assert reasoning.queries[0].run_id == "run-1"
    assert reasoning.graph is not None
    statuses = {node.kind: node.status for node in reasoning.graph.nodes}
    assert statuses == {
        ReasoningNodeKind.OBSERVATION: ReasoningNodeStatus.RECORDED,
        ReasoningNodeKind.FACT_CANDIDATE: ReasoningNodeStatus.CANDIDATE,
        ReasoningNodeKind.HYPOTHESIS: ReasoningNodeStatus.UNVERIFIED,
        ReasoningNodeKind.VULNERABILITY_CANDIDATE: ReasoningNodeStatus.CANDIDATE,
        ReasoningNodeKind.NEGATIVE_RESULT: ReasoningNodeStatus.RECORDED,
    }
    assert all(node.run_id == "run-1" for node in reasoning.graph.nodes)
    assert all(node.session_id == "session-1" for node in reasoning.graph.nodes)
    assert all(node.created_by == "primary" for node in reasoning.graph.nodes)


async def test_cognitive_tools_reject_model_owned_status_and_subagents() -> None:
    reasoning = FakeReasoningProposals()
    control, _, _, _ = service(reasoning_proposals=reasoning)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        await control(
            SCOPE,
            "propose_finding",
            {
                "expected_graph_version": 0,
                "claim": "Untrusted confirmed claim",
                "evidence_ids": ["evidence-1"],
                "status": "confirmed",
            },
            "forged-status",
        )

    subagent_scope = RuntimeToolScope(
        run_id="run-1",
        session_id="session-subagent",
        agent_id="subagent",
        model_profile="test-profile",
    )
    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            subagent_scope,
            "query_reasoning_graph",
            {},
            "subagent-query",
        )
    assert captured.value.code == "cognitive_tools_primary_required"
    assert reasoning.queries == []


async def test_effectful_control_tool_fails_closed_without_approved_intent() -> None:
    control, events, transcript, _ = service()

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "apply_patch",
            {"path": "src/app.py"},
            "patch-call",
        )

    assert captured.value.code == "control_tool_approval_missing"
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"


async def test_mcp_call_requires_approval_and_uses_durable_intent_identity() -> None:
    mcp = FakeMCP()
    control, events, transcript, _ = service(mcp=mcp)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "call_mcp_tool",
            {"tool_id": "mcp__docs__read", "arguments": {"query": "hello"}},
            "mcp-call",
        )

    assert captured.value.code == "control_tool_approval_missing"
    assert mcp.calls == []
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"

    tracker = FakeControlIntents()
    control, _, transcript, _ = service(mcp=mcp, control_intents=tracker)
    result = await control(
        SCOPE,
        "call_mcp_tool",
        {"tool_id": "mcp__docs__read", "arguments": {"query": "hello"}},
        "mcp-call",
    )

    assert result["artifact_id"] == "mcp-artifact-1"
    assert mcp.calls == [
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "tool_call_id": "intent-mcp-call",
            "tool_id": "mcp__docs__read",
            "arguments": {"query": "hello"},
        }
    ]
    assert tracker.calls == [("begin", "mcp-call"), ("success", "mcp-call")]
    assert tracker.begin_arguments[0]["attempt_group"] == "mcp"
    draft = transcript.rows[0][1]
    assert draft.artifact_ids == ["mcp-artifact-1"]
    assert draft.structured_content["source_refs"] == [
        "mcp-tool://mcp__docs__read",
        "mcp-execution://execution:v1:mcp",
        "artifact://mcp-artifact-1",
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("open_browser", {"url": "https://example.test/"}),
        (
            "act_browser",
            {
                "browser_session_id": "browser-1",
                "page_id": "page-1",
                "observation_version": 1,
                "action": "click",
                "element_ref": "e-1",
            },
        ),
    ],
)
async def test_effectful_browser_tools_have_no_side_effect_without_approval(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    browser = FakeBrowser()
    control, events, transcript, _ = service(browser=browser)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(SCOPE, tool_name, arguments, f"{tool_name}-call")

    assert captured.value.code == "control_tool_approval_missing"
    assert browser.calls == []
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"


async def test_public_web_fetch_has_no_network_side_effect_without_approval() -> None:
    web_fetcher = FakeWebFetcher()
    control, events, transcript, _ = service(web_fetcher=web_fetcher)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "web_fetch",
            {"url": "https://example.test/advisory"},
            "web-fetch-call",
        )

    assert captured.value.code == "control_tool_approval_missing"
    assert web_fetcher.calls == []
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("web_search", {"query": "public advisory"}),
        ("web_research", {"question": "Which version fixed the issue?"}),
    ],
)
async def test_web_search_and_research_have_no_egress_without_approval(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    web_research = FakeWebResearch()
    control, events, transcript, _ = service(web_research=web_research)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(SCOPE, tool_name, arguments, f"{tool_name}-call")

    assert captured.value.code == "control_tool_approval_missing"
    assert web_research.calls == []
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"


async def test_approved_patch_and_revert_are_receipt_bound_and_transcripted() -> None:
    code = FakeCode()
    tracker = FakeControlIntents()
    control, _, transcript, _ = service(code=code, control_intents=tracker)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch"
    )

    applied = await control(
        SCOPE,
        "apply_patch",
        {"patch": patch, "expected_sha256": "1" * 64},
        "patch-call",
    )
    reverted = await control(
        SCOPE,
        "revert_patch",
        {"receipt_artifact_id": "patch-receipt-1"},
        "revert-call",
    )

    assert applied["receipt_artifact_id"] == "patch-receipt-1"
    assert reverted["action"] == "reverted"
    assert code.calls == [
        (
            "apply_patch",
            "run-1",
            {"patch": patch, "expected_sha256": "1" * 64},
        ),
        (
            "revert_patch",
            "run-1",
            {"receipt_artifact_id": "patch-receipt-1"},
        ),
    ]
    assert tracker.calls == [
        ("begin", "patch-call"),
        ("success", "patch-call"),
        ("begin", "revert-call"),
        ("success", "revert-call"),
    ]
    assert [row[1].artifact_ids for row in transcript.rows] == [
        ["patch-receipt-1"],
        ["patch-receipt-1"],
    ]
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["code://src/app.py", "artifact://patch-receipt-1"],
        ["code://src/app.py", "artifact://patch-receipt-1"],
    ]


async def test_managed_browser_is_approved_scoped_bounded_and_transcripted() -> None:
    browser = FakeBrowser()
    tracker = FakeControlIntents()
    control, _, transcript, _ = service(browser=browser, control_intents=tracker)

    opened = await control(
        SCOPE,
        "open_browser",
        {"url": "https://example.test/"},
        "browser-open-call",
    )
    observed = await control(
        SCOPE,
        "observe_browser",
        {"browser_session_id": "browser-1"},
        "browser-observe-call",
    )
    acted = await control(
        SCOPE,
        "act_browser",
        {
            "browser_session_id": "browser-1",
            "page_id": "page-1",
            "observation_version": 2,
            "action": "click",
            "element_ref": "e-1",
        },
        "browser-act-call",
    )
    closed = await control(
        SCOPE,
        "close_browser",
        {"browser_session_id": "browser-1"},
        "browser-close-call",
    )

    assert opened["observation"]["content_trust"] == "UNTRUSTED_EXTERNAL_CONTENT"
    assert observed["observation"]["observation_version"] == 2
    assert acted["action"]["action_key"] == "browser-act-call"
    assert closed["session"]["status"] == "closed"
    open_command = browser.calls[0][1]
    assert isinstance(open_command, OpenBrowser)
    assert open_command.run_id == "run-1"
    assert open_command.agent_session_id == "session-1"
    assert open_command.headless is True
    act_call = next(item for item in browser.calls if item[0] == "act")[1]
    assert isinstance(act_call, tuple)
    act_command = act_call[1]
    assert isinstance(act_command, ActBrowser)
    assert act_command.action_key == "browser-act-call"
    assert tracker.calls == [
        ("begin", "browser-open-call"),
        ("success", "browser-open-call"),
        ("begin", "browser-act-call"),
        ("success", "browser-act-call"),
    ]
    assert [set(row[1].artifact_ids) for row in transcript.rows] == [
        {"screenshot-1", "network-1"},
        {"screenshot-1", "network-1"},
        {"screenshot-1", "network-1"},
        set(),
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == [
        "browser://browser-1",
        "browser-page://page-1",
        "artifact://network-1",
        "artifact://screenshot-1",
    ]


async def test_managed_browser_rejects_sibling_agent_session() -> None:
    browser = FakeBrowser(agent_session_id="session-sibling")
    control, events, transcript, _ = service(browser=browser)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "observe_browser",
            {"browser_session_id": "browser-1"},
            "browser-scope-call",
        )

    assert captured.value.code == "browser_scope_mismatch"
    assert [item[0] for item in browser.calls] == ["get"]
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "browser_scope_mismatch"


async def test_managed_browser_compacts_maximal_observation_before_transcript() -> None:
    control, events, transcript, _ = service(browser=LargeFakeBrowser())

    result = await control(
        SCOPE,
        "observe_browser",
        {"browser_session_id": "browser-1"},
        "browser-large-observe-call",
    )

    observation = result["observation"]
    assert len(observation["headings"]) == 30
    assert len(observation["interactive_elements"]) == 30
    assert len(observation["forms"]) == 5
    assert len(observation["forms"][0]["fields"]) == 10
    assert len(observation["recent_network_summary"]) == 15
    assert all(observation["truncated"].values())
    assert len(json.dumps(result).encode()) < 256 * 1024
    assert transcript.rows[0][1].structured_content["content"] == result
    assert events.rows[-1][1] == "runtime.control_tool_completed"
    assert events.rows[-1][2]["result_bytes"] < 256 * 1024


async def test_public_web_fetch_is_approved_bounded_untrusted_and_transcripted() -> None:
    web_fetcher = FakeWebFetcher()
    tracker = FakeControlIntents()
    control, events, transcript, _ = service(
        web_fetcher=web_fetcher,
        control_intents=tracker,
    )

    result = await control(
        SCOPE,
        "web_fetch",
        {
            "url": "https://example.test/advisory",
            "cache_policy": "refresh",
            "max_response_bytes": 1_000_000,
            "timeout_seconds": 20,
        },
        "web-fetch-call",
    )

    assert len(web_fetcher.calls) == 1
    run_id, request = web_fetcher.calls[0]
    assert run_id == "run-1"
    assert str(request.url) == "https://example.test/advisory"
    assert request.cache_policy.value == "refresh"
    assert request.max_response_bytes == 1_000_000
    assert request.timeout_seconds == 20
    assert result["content_trust"] == "UNTRUSTED_EXTERNAL_CONTENT"
    assert result["source"]["id"] == "source-1"
    assert result["document"]["id"] == "document-1"
    assert len(result["chunks"][0]["content_excerpt"]) == 6_000
    assert result["chunks"][0]["content_truncated"] is True
    assert len(json.dumps(result).encode()) < 256 * 1024
    assert tracker.calls == [
        ("begin", "web-fetch-call"),
        ("success", "web-fetch-call"),
    ]
    draft = transcript.rows[0][1]
    assert set(draft.artifact_ids) == {"web-raw-1", "web-normalized-1"}
    assert draft.structured_content["source_refs"] == [
        "web-document://document-1",
        "web-source://source-1",
        "artifact://web-normalized-1",
        "artifact://web-raw-1",
    ]
    assert events.rows[-1][2]["result_bytes"] < 256 * 1024


async def test_approved_web_search_returns_only_artifact_backed_untrusted_candidates() -> None:
    web_research = FakeWebResearch()
    tracker = FakeControlIntents()
    control, events, transcript, _ = service(
        web_research=web_research,
        control_intents=tracker,
    )

    result = await control(
        SCOPE,
        "web_search",
        {
            "query": "public advisory",
            "search_type": "security_advisory",
            "max_results": 5,
        },
        "web-search-call",
    )

    assert result["candidate_status"] == "DISCOVERY_ONLY_NOT_A_CANONICAL_SOURCE"
    assert result["content_trust"] == "UNTRUSTED_EXTERNAL_CONTENT"
    assert result["artifact_id"] == "search-artifact-1"
    assert result["results"][0]["id"] == "candidate-1"
    assert web_research.calls[0][0] == "search"
    _, (_, _, model_profile, request) = web_research.calls[0]
    assert model_profile == "test-profile"
    assert request.search_type.value == "security_advisory"
    draft = transcript.rows[0][1]
    assert draft.artifact_ids == ["search-artifact-1"]
    assert draft.structured_content["source_refs"] == [
        "web-search://query-1",
        "artifact://search-artifact-1",
    ]
    assert tracker.calls == [("begin", "web-search-call"), ("success", "web-search-call")]
    assert events.rows[-1][2]["result_bytes"] < 256 * 1024


async def test_approved_web_research_returns_only_canonical_sources_and_artifacts() -> None:
    web_research = FakeWebResearch()
    tracker = FakeControlIntents()
    control, _, transcript, _ = service(
        web_research=web_research,
        control_intents=tracker,
    )

    result = await control(
        SCOPE,
        "web_research",
        {"question": "Which version fixed the issue?", "max_sources": 3},
        "web-research-call",
    )

    assert result["canonical_sources_only"] is True
    assert result["content_trust"] == "UNTRUSTED_EXTERNAL_CONTENT"
    assert result["packet_id"] == "packet-1"
    assert result["sources"][0]["id"] == "source-1"
    assert result["key_claims"][0]["evidence"][0]["source_id"] == "source-1"
    draft = transcript.rows[0][1]
    assert set(draft.artifact_ids) == {"raw-1", "normalized-1", "research-artifact-1"}
    assert draft.structured_content["source_refs"] == [
        "web-research://packet-1",
        "web-source://source-1",
        "artifact://raw-1",
        "artifact://normalized-1",
        "artifact://research-artifact-1",
    ]


async def test_target_http_has_no_network_side_effect_without_approval() -> None:
    target_http = FakeTargetHttp()
    control, events, transcript, _ = service(
        artifacts=FakeTargetArtifacts(),
        runs=FakeRuns(),
        target_http=target_http,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "target_http_request",
            {"method": "GET", "url": "https://target.internal/health"},
            "target-http-call",
        )

    assert captured.value.code == "control_tool_approval_missing"
    assert target_http.submissions == []
    assert transcript.rows == []
    assert events.rows[-1][2]["error_code"] == "control_tool_approval_missing"


async def test_http_traffic_query_and_exchange_read_are_redacted_and_transcripted() -> None:
    traffic = FakeTraffic()
    target_http = FakeTargetHttp()
    control, _, transcript, _ = service(
        artifacts=FakeTargetArtifacts(),
        runs=FakeRuns(),
        traffic=traffic,
        target_http=target_http,
    )

    page = await control(
        SCOPE,
        "query_http_traffic",
        {"method": "GET", "status_class": "success", "limit": 10},
        "traffic-query-call",
    )
    detail = await control(
        SCOPE,
        "read_http_exchange",
        {"exchange_id": "exchange-1"},
        "traffic-read-call",
    )

    assert page["content_trust"] == "REDACTED_SENSITIVE_METADATA"
    assert page["items"] == [{"exchange_id": "exchange-1", "method": "GET"}]
    assert detail["exchange_id"] == "exchange-1"
    assert detail["response"]["content_trust"] == "UNTRUSTED_EXTERNAL_CONTENT"
    assert detail["response"]["response_excerpt"] == '{"ok":true}'
    assert detail["request_artifact_id"] == "target-request-1"
    assert detail["response_artifact_id"] == "target-response-1"
    assert traffic.calls == [
        (
            "list",
            "run-1",
            {
                "method": "GET",
                "status_class": TrafficStatusClass.SUCCESS,
                "limit": 10,
                "cursor": None,
            },
        ),
        ("get", "run-1", {"exchange_id": "exchange-1"}),
    ]
    assert transcript.rows[0][1].structured_content["source_refs"] == [
        "target-http://exchange-1"
    ]
    assert transcript.rows[1][1].artifact_ids == [
        "target-request-1",
        "target-response-1",
    ]
    assert transcript.rows[1][1].structured_content["source_refs"] == [
        "target-http://exchange-1",
        "artifact://target-request-1",
        "artifact://target-response-1",
    ]


async def test_approved_target_http_reuses_durable_intent_and_forces_artifacts() -> None:
    tracker = FakeControlIntents()
    target_http = FakeTargetHttp()
    control, _, transcript, _ = service(
        artifacts=FakeTargetArtifacts(),
        runs=FakeRuns(),
        target_http=target_http,
        control_intents=tracker,
    )

    result = await control(
        SCOPE,
        "target_http_request",
        {
            "method": "post",
            "url": "https://target.internal/private?token=secret",
            "headers": {"X-Test": "value"},
            "json_body": {"probe": True},
            "follow_redirects": True,
            "timeout_seconds": 20,
            "max_response_bytes": 100_000,
        },
        "target-http-call",
    )

    assert len(target_http.submissions) == 1
    submission = target_http.submissions[0]
    assert submission.run_id == "run-1"
    assert submission.session_id == "session-1"
    assert submission.tool_call_id == "intent-target-http-call"
    assert submission.node_id == "local"
    assert submission.request.method == "POST"
    assert submission.request.save_request is True
    assert submission.request.save_response is True
    assert submission.request.proxy is None
    assert submission.request.client_cert_ref is None
    assert "intent-target-http-call" not in submission.request.execution_key
    assert result["exchange_id"] == "exchange-1"
    assert result["final_url_summary"] == {
        "scheme": "https",
        "origin": "https://target.internal",
        "path_shape": "/…",
        "path_segment_count": 1,
    }
    assert "secret" not in json.dumps(result)
    assert tracker.calls == [
        ("begin", "target-http-call"),
        ("success", "target-http-call"),
    ]
    draft = transcript.rows[0][1]
    assert draft.artifact_ids == ["target-request-1", "target-response-1"]
    assert draft.structured_content["source_refs"] == [
        "target-http://exchange-1",
        "artifact://target-request-1",
        "artifact://target-response-1",
    ]


async def test_target_http_rejects_foreign_artifact_before_transcript() -> None:
    tracker = FakeControlIntents()
    target_http = FakeTargetHttp()
    control, events, transcript, _ = service(
        artifacts=ForeignTargetArtifacts(),
        runs=FakeRuns(),
        target_http=target_http,
        control_intents=tracker,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "target_http_request",
            {"method": "GET", "url": "https://target.internal/health"},
            "target-http-foreign-artifact",
        )

    assert captured.value.code == "target_http_artifact_run_mismatch"
    assert transcript.rows == []
    assert tracker.calls == [
        ("begin", "target-http-foreign-artifact"),
        ("failed", "target-http-foreign-artifact"),
    ]
    assert events.rows[-1][2]["error_code"] == "target_http_artifact_run_mismatch"


async def test_target_http_runtime_tools_reject_code_audit_before_service_use() -> None:
    tracker = FakeControlIntents()
    traffic = FakeTraffic()
    target_http = FakeTargetHttp()
    control, _, transcript, _ = service(
        artifacts=FakeTargetArtifacts(),
        runs=FakeRuns(kind=RunKind.CODE_AUDIT),
        traffic=traffic,
        target_http=target_http,
        control_intents=tracker,
    )

    for tool_name, arguments in (
        ("query_http_traffic", {}),
        ("read_http_exchange", {"exchange_id": "exchange-1"}),
        (
            "target_http_request",
            {"method": "GET", "url": "https://target.internal/health"},
        ),
    ):
        with pytest.raises(ApplicationConflictError) as captured:
            await control(SCOPE, tool_name, arguments, f"{tool_name}-call")
        assert captured.value.code == "run_kind_operation_unsupported"

    assert traffic.calls == []
    assert target_http.submissions == []
    assert transcript.rows == []
    assert tracker.calls == [
        ("begin", "target_http_request-call"),
        ("failed", "target_http_request-call"),
    ]


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
    skill_path = directory / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text().replace(
            "description: Validate bounded HTTP behavior",
            "description: Validate updated HTTP behavior",
        )
    )
    reloaded = await control(
        SCOPE,
        "reload_skill",
        {"skill_id": "http-validation", "reason": "refresh the pinned procedure"},
        "call-reload-skill",
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
    assert reloaded["description"] == "Validate updated HTTP behavior"
    assert reloaded["digest"] != loaded["digest"]
    assert unloaded == {"skill_id": "http-validation", "active": False}
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["runtime-tool://search_skills"],
        ["skill://http-validation"],
        ["skill://http-validation"],
        ["skill://http-validation"],
        ["skill://http-validation"],
    ]


async def test_tool_control_tools_require_explicit_reload_and_support_unload(
    tmp_path: Path,
) -> None:
    config = tmp_path / "tools.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "scanner": {
                        "command": [sys.executable],
                        "description": "Original scanner",
                    }
                },
            }
        )
    )
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    tools = ToolContextManager(registry)
    control, _, transcript, _ = service(tools=tools)

    selected = await control(
        SCOPE,
        "get_tool",
        {"tool_id": "scanner"},
        "call-get-tool",
    )
    config.write_text(
        config.read_text().replace("Original scanner", "Updated scanner")
    )
    await registry.reload_if_changed()
    stale = await control(
        SCOPE,
        "get_tool",
        {"tool_id": "scanner"},
        "call-get-stale-tool",
    )
    reloaded = await control(
        SCOPE,
        "reload_tool",
        {"tool_id": "scanner"},
        "call-reload-tool",
    )
    unloaded = await control(
        SCOPE,
        "unload_tool",
        {"tool_id": "scanner"},
        "call-unload-tool",
    )

    assert selected["stale"] is False
    assert stale["stale"] is True
    assert stale["digest"] == selected["digest"]
    assert reloaded["stale"] is False
    assert reloaded["digest"] != selected["digest"]
    assert reloaded["full_schema"]["description"] == "Updated scanner"
    assert unloaded == {"tool_id": "scanner", "unloaded": True}
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["tool://scanner"],
        ["tool://scanner"],
        ["tool://scanner"],
        ["tool://scanner"],
    ]


async def test_technique_control_tools_select_reload_and_unload() -> None:
    _, first = version("1.0.0")
    catalog = FakeTechniqueCatalog([first])
    techniques = TechniqueContextManager(catalog)  # type: ignore[arg-type]
    control, _, transcript, _ = service(techniques=techniques)

    listed = await control(
        SCOPE,
        "list_techniques",
        {"max_results": 10},
        "call-list-techniques",
    )
    loaded = await control(
        SCOPE,
        "load_technique",
        {
            "technique_id": "web.request-analysis",
            "reason": "compare request evidence",
        },
        "call-load-technique",
    )
    _, second = version("2.0.0")
    catalog.versions = [second]
    reloaded = await control(
        SCOPE,
        "reload_technique",
        {
            "technique_id": "web.request-analysis",
            "reason": "refresh the selected technique",
        },
        "call-reload-technique",
    )
    unloaded = await control(
        SCOPE,
        "unload_technique",
        {"technique_id": "web.request-analysis"},
        "call-unload-technique",
    )

    assert listed[0]["id"] == "web.request-analysis"
    assert loaded["manifest"]["version"] == "1.0.0"
    assert reloaded["manifest"]["version"] == "2.0.0"
    assert unloaded == {
        "technique_id": "web.request-analysis",
        "active": False,
    }
    assert [row[1].structured_content["source_refs"] for row in transcript.rows] == [
        ["runtime-tool://list_techniques"],
        ["technique://web.request-analysis"],
        ["technique://web.request-analysis"],
        ["technique://web.request-analysis"],
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


async def test_approved_worktree_is_primary_only_and_transcripted() -> None:
    git = FakeGit()
    tracker = FakeControlIntents()
    control, _, transcript, _ = service(git=git, control_intents=tracker)

    result = await control(
        SCOPE,
        "create_worktree",
        {"name": "fix", "start_point": "HEAD"},
        "worktree-call",
    )

    assert result["path"] == ".riftx-wt-owner-fix"
    assert git.calls == [
        (
            "create_worktree",
            "run-1",
            {"name": "fix", "start_point": "HEAD"},
        )
    ]
    assert tracker.calls == [
        ("begin", "worktree-call"),
        ("success", "worktree-call"),
    ]
    assert transcript.rows[-1][1].structured_content["source_refs"] == [
        "worktree://.riftx-wt-owner-fix",
        "git-commit://" + "1" * 40,
    ]

    subagent_scope = RuntimeToolScope(
        run_id="run-1",
        session_id="session-subagent",
        agent_id="subagent",
        model_profile="test-profile",
    )
    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            subagent_scope,
            "create_worktree",
            {"name": "other"},
            "subagent-worktree-call",
        )
    assert captured.value.code == "code_worktree_primary_required"
    assert len(git.calls) == 1


async def test_worktree_has_no_side_effect_without_approved_intent() -> None:
    git = FakeGit()
    control, _, transcript, _ = service(git=git)

    with pytest.raises(ApplicationConflictError) as captured:
        await control(
            SCOPE,
            "create_worktree",
            {"name": "fix"},
            "worktree-call",
        )

    assert captured.value.code == "control_tool_approval_missing"
    assert git.calls == []
    assert transcript.rows == []


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
