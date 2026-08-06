"""Run-scoped inline control tools for the production Agent Runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Collection
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from riftx.application.errors import (
    ApplicationConflictError,
    ApplicationServiceError,
    EntityNotFoundError,
)
from riftx.application.services import (
    ArtifactApplicationService,
    QueryReasoningGraph,
    ReasoningGraphQueryResult,
    TrafficMetadataApplicationService,
)
from riftx.application.services.runs import require_interactive_run_operation
from riftx.application.traffic import TrafficStatusClass
from riftx.browser.service import ActBrowser, BrowserApplicationService, BrowserView, OpenBrowser
from riftx.capabilities import TechniqueContextManager
from riftx.code import CodeWorkspaceService, GitWorkspaceService
from riftx.context import (
    AttemptRecord,
    AttemptStatus,
    CurrentFocus,
    NextAction,
    PlanItemUpdate,
    PlanUpdateProposal,
    WorkingMemory,
)
from riftx.domain import (
    AgentMessage,
    ArtifactAccessClass,
    BrowserActionType,
    Execution,
    MessageRole,
    MessageType,
    MessageVisibility,
    Run,
    TranscriptMessageDraft,
)
from riftx.domain.base import new_id
from riftx.execution import ExecutionService, build_execution_key
from riftx.mcp import MCPApplicationService
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
)
from riftx.runtime.engine.agent_factory import RuntimeToolScope
from riftx.skills import ProgressiveSkillContextManager
from riftx.target_http import TargetHttpRequest, TargetHttpResult, TargetHttpSubmission
from riftx.target_http.redaction import safe_redirect_metadata, safe_url_metadata
from riftx.target_http.service import TargetHttpApplicationService
from riftx.tasks import (
    AddTaskCommand,
    BlockTaskCommand,
    CancelTaskCommand,
    ClaimReadyTaskCommand,
    CompleteTaskCommand,
    FailTaskAttemptCommand,
    LinkTasksCommand,
    ReopenTaskCommand,
    Task,
    TaskBudgetInput,
    TaskEvidenceRequirementInput,
    TaskMutationResult,
    UpdateTaskCommand,
)
from riftx.tools import ToolContextManager, ToolSearchRequest
from riftx.tools.policy import AGENT_TOOL_POLICIES
from riftx.web import (
    CachePolicy,
    FetchRequest,
    FetchResult,
    PublicWebFetcher,
    RedirectPolicy,
    ResearchRequest,
    SearchFreshness,
    SearchRequest,
    SearchResponse,
    SearchType,
    WebResearchApplicationService,
    WebResearchPacket,
)

_MAX_CONTROL_RESULT_BYTES = 256 * 1024


class RunEventWriter(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> object: ...


class TranscriptWriter(Protocol):
    async def append(
        self,
        session_id: str,
        draft: TranscriptMessageDraft,
    ) -> AgentMessage: ...


class ControlIntentTracker(Protocol):
    async def begin_control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        attempt_group: str = "control",
        target_interaction_tool_ids: Collection[str] | None = None,
    ) -> ClaimedControlIntent | None: ...

    async def finish_control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
        succeeded: bool,
    ) -> None: ...


class ClaimedControlIntent(Protocol):
    id: str


class RunReader(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class TaskPlanner(Protocol):
    async def add_task(self, command: AddTaskCommand) -> TaskMutationResult: ...

    async def update_task(self, command: UpdateTaskCommand) -> TaskMutationResult: ...

    async def link_tasks(self, command: LinkTasksCommand) -> TaskMutationResult: ...

    async def block_task(self, command: BlockTaskCommand) -> TaskMutationResult: ...

    async def complete_task(self, command: CompleteTaskCommand) -> TaskMutationResult: ...

    async def fail_task_attempt(
        self,
        command: FailTaskAttemptCommand,
    ) -> TaskMutationResult: ...

    async def reopen_task(self, command: ReopenTaskCommand) -> TaskMutationResult: ...

    async def cancel_task(self, command: CancelTaskCommand) -> TaskMutationResult: ...

    async def list_ready(self, run_id: str, *, limit: int = 100) -> tuple[Task, ...]: ...

    async def claim_ready_task(
        self,
        command: ClaimReadyTaskCommand,
    ) -> TaskMutationResult | None: ...


class WorkingMemoryProposalService(Protocol):
    async def propose_plan_update(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        proposal: PlanUpdateProposal,
    ) -> WorkingMemory: ...

    async def record_attempt(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        attempt: AttemptRecord,
    ) -> WorkingMemory: ...


class ReasoningProposalService(Protocol):
    async def create_node(
        self,
        node: ReasoningNode,
        *,
        expected_graph_version: int,
    ) -> ReasoningGraph: ...

    async def record_negative_result(
        self,
        negative_result: ReasoningNode,
        *,
        invalidated_node_id: str,
        expected_graph_version: int,
        edge_id: str | None = None,
    ) -> ReasoningGraph: ...

    async def query(self, command: QueryReasoningGraph) -> ReasoningGraphQueryResult: ...


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SearchArguments(_Arguments):
    query: str = ""
    capability: str | None = None
    max_results: int = Field(default=8, ge=1, le=20)
    include_unavailable: bool = True


class _ListArguments(_Arguments):
    include_unavailable: bool = True
    max_results: int = Field(default=100, ge=1, le=100)


class _ToolArguments(_Arguments):
    tool_id: str = Field(min_length=1)


class _SearchMCPToolsArguments(_Arguments):
    query: str = Field(default="", max_length=2000)
    max_results: int = Field(default=20, ge=1, le=20)


class _MCPToolArguments(_Arguments):
    tool_id: str = Field(min_length=1, max_length=64)


class _CallMCPToolArguments(_MCPToolArguments):
    arguments: dict[str, object] = Field(max_length=1024)


class _SkillSearchArguments(_Arguments):
    query: str = ""
    capability: str | None = None
    max_results: int = Field(default=8, ge=1, le=20)


class _SkillListArguments(_Arguments):
    max_results: int = Field(default=100, ge=1, le=100)


class _SkillArguments(_Arguments):
    skill_id: str = Field(min_length=1)


class _LoadSkillArguments(_SkillArguments):
    reason: str = Field(min_length=1, max_length=1000)


class _TechniqueArguments(_Arguments):
    technique_id: str = Field(min_length=1)


class _LoadTechniqueArguments(_TechniqueArguments):
    reason: str = Field(min_length=1, max_length=1000)


class _ExecutionArguments(_Arguments):
    execution_id: str = Field(min_length=1)


class _WaitExecutionArguments(_ExecutionArguments):
    timeout_seconds: float = Field(default=30, gt=0, le=30)
    stdout_cursor: int = Field(default=0, ge=0)
    stderr_cursor: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)
    next_poll_after_seconds: int = Field(default=10, ge=1, le=3600)


class _CancelExecutionArguments(_ExecutionArguments):
    reason: str | None = Field(default=None, max_length=1000)


class _ReadArtifactArguments(_Arguments):
    artifact_id: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)


class _CompleteRunArguments(_Arguments):
    run_summary: str = Field(min_length=1, max_length=16_384)


class _ListReadyTasksArguments(_Arguments):
    limit: int = Field(default=100, ge=1, le=100)


class _AddTaskArguments(_Arguments):
    expected_graph_version: int = Field(ge=0)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=64)
    sequence: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    description: str = ""
    input_scope: dict[str, JsonValue] = Field(default_factory=dict)
    expected_output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    required_capability_ids: list[str] = Field(default_factory=list)
    workspace_owner: str | None = None
    session_owner_id: str | None = None
    stop_condition: str | None = None
    budget: TaskBudgetInput | None = None
    evidence_requirements: list[TaskEvidenceRequirementInput] = Field(default_factory=list)


class _UpdateTaskArguments(_Arguments):
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    input_scope: dict[str, JsonValue] | None = None
    expected_output_schema: dict[str, JsonValue] | None = None
    required_capability_ids: list[str] | None = None
    workspace_owner: str | None = None
    session_owner_id: str | None = None
    stop_condition: str | None = None


class _LinkTasksArguments(_Arguments):
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    depends_on_task_id: str = Field(min_length=1, max_length=64)


class _TaskReasonArguments(_Arguments):
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1)


class _ClaimReadyTaskArguments(_Arguments):
    preferred_task_id: str | None = Field(default=None, min_length=1, max_length=64)


class _CompleteTaskArguments(_Arguments):
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=64)
    completion_summary: str = Field(min_length=1)
    evidence_refs_by_requirement: dict[str, list[str]] = Field(default_factory=dict)


class _FailTaskAttemptArguments(_Arguments):
    expected_graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=64)
    failure_summary: str = Field(min_length=1)


class _ProposePlanUpdateArguments(_Arguments):
    expected_memory_version: int = Field(ge=0)
    item_updates: list[PlanItemUpdate] = Field(default_factory=list, max_length=100)
    current_focus: CurrentFocus | None = None
    next_action: NextAction | None = None


class _RecordAttemptArguments(_Arguments):
    expected_memory_version: int = Field(ge=0)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=64)
    action_signature: str = Field(min_length=1, max_length=1_000)
    target: str = Field(min_length=1, max_length=8_192)
    tool_id: str = Field(min_length=1, max_length=256)
    normalized_arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    result_status: AttemptStatus
    result_summary: str = Field(min_length=1, max_length=16_384)
    retryable: bool = False
    retry_of_attempt_id: str | None = Field(default=None, min_length=1, max_length=64)
    retry_reason: str | None = Field(default=None, min_length=1, max_length=2_000)


class _ReasoningProposalArguments(_Arguments):
    expected_graph_version: int = Field(ge=0)
    node_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    claim: str = Field(min_length=1, max_length=20_000)
    structured_data: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=1_000)


class _EvidenceReasoningProposalArguments(_ReasoningProposalArguments):
    evidence_ids: list[str] = Field(min_length=1, max_length=1_000)


class _RecordNegativeResultArguments(_EvidenceReasoningProposalArguments):
    invalidated_node_id: str = Field(min_length=1, max_length=64)


class _QueryReasoningGraphArguments(_Arguments):
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    kinds: list[ReasoningNodeKind] = Field(default_factory=list, max_length=8)
    statuses: list[ReasoningNodeStatus] = Field(default_factory=list, max_length=16)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    query: str = Field(default="", max_length=2_000)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    edge_limit: int = Field(default=100, ge=0, le=200)


class _ListFilesArguments(_Arguments):
    path: str = ""
    recursive: bool = False
    max_entries: int = Field(default=200, ge=1, le=1000)


class _ReadFileArguments(_Arguments):
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)


class _ReadManyFilesArguments(_Arguments):
    paths: list[str] = Field(min_length=1, max_length=20)
    max_bytes_per_file: int = Field(default=32 * 1024, ge=1, le=64 * 1024)
    max_total_bytes: int = Field(default=128 * 1024, ge=1, le=128 * 1024)


class _GlobArguments(_Arguments):
    pattern: str = Field(min_length=1, max_length=4096)
    path: str = ""
    max_results: int = Field(default=200, ge=1, le=1000)


class _GrepArguments(_Arguments):
    query: str = Field(min_length=1, max_length=4096)
    path: str = ""
    file_glob: str | None = Field(default=None, min_length=1, max_length=4096)
    case_sensitive: bool = True
    max_matches: int = Field(default=100, ge=1, le=200)


class _SymbolSearchArguments(_Arguments):
    query: str = Field(min_length=1, max_length=1024)
    path: str = Field(default="", max_length=4096)
    file_glob: str | None = Field(default=None, min_length=1, max_length=4096)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=200)


class _FindReferencesArguments(_Arguments):
    symbol: str = Field(min_length=1, max_length=512)
    path: str = Field(default="", max_length=4096)
    file_glob: str | None = Field(default=None, min_length=1, max_length=4096)
    include_declarations: bool = True
    max_results: int = Field(default=100, ge=1, le=200)


class _CallHierarchyArguments(_Arguments):
    symbol: str = Field(min_length=1, max_length=512)
    direction: Literal["incoming", "outgoing", "both"] = "both"
    path: str = Field(default="", max_length=4096)
    file_glob: str | None = Field(default=None, min_length=1, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=200)


class _DiagnosticsArguments(_Arguments):
    path: str = Field(default="", max_length=4096)
    file_glob: str | None = Field(default=None, min_length=1, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=200)


class _ApplyPatchArguments(_Arguments):
    patch: str = Field(min_length=1, max_length=262_144)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _RevertPatchArguments(_Arguments):
    receipt_artifact_id: str = Field(min_length=1)


class _OpenBrowserArguments(_Arguments):
    url: str = Field(min_length=1, max_length=8192)
    include_screenshot: bool = True


class _ObserveBrowserArguments(_Arguments):
    browser_session_id: str = Field(min_length=1)
    page_id: str | None = Field(default=None, min_length=1)
    include_screenshot: bool = False
    include_network: bool = True


class _ActBrowserArguments(_Arguments):
    browser_session_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    observation_version: int = Field(ge=1)
    action: Literal[
        "navigate",
        "click",
        "fill",
        "type",
        "select",
        "press",
        "scroll",
        "download",
        "wait",
        "go_back",
        "reload",
    ]
    element_ref: str | None = Field(default=None, min_length=1, max_length=255)
    value: str | None = Field(default=None, max_length=16_384)
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    delay_ms: int = Field(default=0, ge=0, le=1000)
    scroll_delta_x: int = Field(default=0, ge=-10_000, le=10_000)
    scroll_delta_y: int = Field(default=700, ge=-10_000, le=10_000)
    wait_ms: int = Field(default=500, ge=0, le=10_000)
    include_screenshot: bool = True


class _CloseBrowserArguments(_Arguments):
    browser_session_id: str = Field(min_length=1)


class _WebFetchArguments(_Arguments):
    url: str = Field(min_length=1, max_length=8192)
    cache_policy: CachePolicy = CachePolicy.DEFAULT
    redirect_policy: RedirectPolicy = RedirectPolicy.SAME_ORIGIN_AUTO
    max_response_bytes: int = Field(default=2_000_000, ge=1, le=10_000_000)
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    save_raw: bool = True
    use_browser_fallback: bool = True


class _WebSearchArguments(_Arguments):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: int = Field(default=10, ge=1, le=20)
    freshness: SearchFreshness | None = None
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    blocked_domains: list[str] = Field(default_factory=list, max_length=20)
    language: str | None = Field(default=None, max_length=32)
    region: str | None = Field(default=None, max_length=32)
    search_type: SearchType = SearchType.GENERAL


class _WebResearchArguments(_Arguments):
    question: str = Field(min_length=1, max_length=4_000)
    search_type: SearchType = SearchType.GENERAL
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    blocked_domains: list[str] = Field(default_factory=list, max_length=20)
    max_queries: int = Field(default=4, ge=1, le=4)
    max_results_per_query: int = Field(default=10, ge=1, le=20)
    max_total_results: int = Field(default=30, ge=1, le=50)
    max_sources: int = Field(default=6, ge=1, le=6)


class _QueryHttpTrafficArguments(_Arguments):
    method: str | None = Field(default=None, min_length=1, max_length=32)
    status_class: TrafficStatusClass | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)


class _ReadHttpExchangeArguments(_Arguments):
    exchange_id: str = Field(min_length=1, max_length=256)


class _TargetHttpRequestArguments(_Arguments):
    method: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=8192)
    headers: dict[str, str] = Field(default_factory=dict, max_length=100)
    query: dict[str, str] = Field(default_factory=dict, max_length=100)
    body: str | None = Field(default=None, max_length=1_000_000)
    json_body: JsonValue | None = None
    cookies: dict[str, str] = Field(default_factory=dict, max_length=100)
    verify_tls: bool = True
    follow_redirects: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    max_response_bytes: int = Field(default=2_000_000, ge=1, le=10_000_000)

    @field_validator("headers", "query", "cookies")
    @classmethod
    def validate_string_map(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key
            or len(key) > 256
            or len(item) > 16_384
            for key, item in value.items()
        ):
            raise ValueError("Target HTTP string map is invalid or too large")
        return value

    @model_validator(mode="after")
    def validate_body_size(self) -> _TargetHttpRequestArguments:
        if self.body is not None and self.json_body is not None:
            raise ValueError("Target HTTP accepts body or json_body, not both")
        if self.json_body is not None:
            encoded = json.dumps(
                self.json_body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(encoded) > 1_000_000:
                raise ValueError("Target HTTP json_body exceeds 1000000 bytes")
        return self


class _GitStatusArguments(_Arguments):
    max_entries: int = Field(default=200, ge=1, le=1000)


class _GitDiffArguments(_Arguments):
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    staged: bool = False
    context_lines: int = Field(default=3, ge=0, le=20)
    max_bytes: int = Field(default=64 * 1024, ge=1, le=64 * 1024)


class _GitLogArguments(_Arguments):
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    max_entries: int = Field(default=20, ge=1, le=100)


class _CreateWorktreeArguments(_Arguments):
    name: str = Field(min_length=1, max_length=64)
    start_point: str = Field(default="HEAD", min_length=1, max_length=64)


class RuntimeControlToolService:
    """Execute resident control tools without crossing the Runner command path.

    Execution reads and mutations are private to the exact Agent Session that
    created them. Artifacts are deliberately shared between Agent Sessions in
    the same Run so Subagent result packets can hand immutable evidence back to
    the Primary; artifacts from another Run remain inaccessible.
    """

    def __init__(
        self,
        *,
        tools: ToolContextManager,
        executions: ExecutionService,
        artifacts: ArtifactApplicationService,
        events: RunEventWriter,
        transcript: TranscriptWriter,
        skills: ProgressiveSkillContextManager | None = None,
        techniques: TechniqueContextManager | None = None,
        code: CodeWorkspaceService | None = None,
        git: GitWorkspaceService | None = None,
        browser: BrowserApplicationService | None = None,
        web_fetcher: PublicWebFetcher | None = None,
        web_research: WebResearchApplicationService | None = None,
        runs: RunReader | None = None,
        traffic: TrafficMetadataApplicationService | None = None,
        target_http: TargetHttpApplicationService | None = None,
        mcp: MCPApplicationService | None = None,
        control_intents: ControlIntentTracker | None = None,
        task_planner: TaskPlanner | None = None,
        working_memory_proposals: WorkingMemoryProposalService | None = None,
        reasoning_proposals: ReasoningProposalService | None = None,
        worker_id: str = "runtime",
    ) -> None:
        self._tools = tools
        self._executions = executions
        self._artifacts = artifacts
        self._events = events
        self._transcript = transcript
        self._skills = skills
        self._techniques = techniques
        self._code = code
        self._git = git
        self._browser = browser
        self._web_fetcher = web_fetcher
        self._web_research = web_research
        self._runs = runs
        self._traffic = traffic
        self._target_http = target_http
        self._mcp = mcp
        self._control_intents = control_intents
        self._task_planner = task_planner
        self._working_memory_proposals = working_memory_proposals
        self._reasoning_proposals = reasoning_proposals
        self._worker_id = worker_id

    async def __call__(
        self,
        scope: RuntimeToolScope,
        tool_name: str,
        arguments: dict[str, object],
        call_id: str,
    ) -> object:
        await self._events.append(
            scope.run_id,
            "runtime.control_tool_started",
            {
                "session_id": scope.session_id,
                "agent_id": scope.agent_id,
                "tool": tool_name,
                "tool_call_id": call_id,
            },
        )
        approval_claimed = False
        claimed_intent: ClaimedControlIntent | None = None
        try:
            policy = AGENT_TOOL_POLICIES.get(tool_name)
            if policy is not None and policy.approval_required:
                claimed_intent = (
                    await self._control_intents.begin_control_intent(
                        run_id=scope.run_id,
                        session_id=scope.session_id,
                        engine_call_id=call_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        attempt_group=(
                            "mcp"
                            if tool_name == "call_mcp_tool"
                            else "initial"
                            if tool_name == "target_http_request"
                            else "control"
                        ),
                        target_interaction_tool_ids=(
                            ("target_http_request",)
                            if tool_name == "target_http_request"
                            else None
                        ),
                    )
                    if self._control_intents is not None
                    else None
                )
                if not claimed_intent:
                    raise ApplicationConflictError(
                        "control_tool_approval_missing",
                        "Control Tool mutation lacks an approved durable intent",
                    )
                approval_claimed = True
            result = await self._invoke(
                scope,
                tool_name,
                arguments,
                call_id=call_id,
                claimed_intent=claimed_intent,
            )
            result = _bounded_result(result)
        except Exception as exc:
            if approval_claimed:
                assert self._control_intents is not None
                await self._control_intents.finish_control_intent(
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    engine_call_id=call_id,
                    succeeded=False,
                )
            error_code = exc.code if isinstance(exc, ApplicationServiceError) else "internal_error"
            await self._events.append(
                scope.run_id,
                "runtime.control_tool_failed",
                {
                    "session_id": scope.session_id,
                    "agent_id": scope.agent_id,
                    "tool": tool_name,
                    "tool_call_id": call_id,
                    "error_type": type(exc).__name__,
                    "error_code": error_code,
                },
            )
            raise

        if approval_claimed:
            assert self._control_intents is not None
            await self._control_intents.finish_control_intent(
                run_id=scope.run_id,
                session_id=scope.session_id,
                engine_call_id=call_id,
                succeeded=True,
            )

        argument_artifact_id = _string_argument(arguments, "artifact_id")
        content = {
            "type": "tool_result",
            "tool": tool_name,
            "tool_call_id": call_id,
            "status": "completed",
            "content": result,
            "source_refs": _source_refs(tool_name, arguments, result=result),
        }
        await self._transcript.append(
            scope.session_id,
            TranscriptMessageDraft(
                agent_id=scope.agent_id,
                role=MessageRole.TOOL,
                message_type=MessageType.TOOL_RESULT_REFERENCE,
                structured_content=content,
                tool_call_id=call_id,
                execution_id=_string_argument(arguments, "execution_id"),
                artifact_ids=list(
                    dict.fromkeys(
                        artifact_id
                        for artifact_id in (
                            argument_artifact_id,
                            *_result_artifact_ids(result),
                        )
                        if artifact_id is not None
                    )
                ),
                visibility=MessageVisibility.AGENT_ONLY,
            ),
        )
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        await self._events.append(
            scope.run_id,
            "runtime.control_tool_completed",
            {
                "session_id": scope.session_id,
                "agent_id": scope.agent_id,
                "tool": tool_name,
                "tool_call_id": call_id,
                "result_bytes": len(encoded),
                "result_sha256": hashlib.sha256(encoded).hexdigest(),
            },
        )
        return result

    async def _invoke(
        self,
        scope: RuntimeToolScope,
        tool_name: str,
        raw_arguments: dict[str, object],
        *,
        call_id: str,
        claimed_intent: ClaimedControlIntent | None,
    ) -> object:
        if tool_name == "search_tools":
            search_arguments = _SearchArguments.model_validate(raw_arguments)
            results = await self._tools.search_tools(
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                request=ToolSearchRequest(
                    query=search_arguments.query,
                    capability=search_arguments.capability,
                    max_results=search_arguments.max_results,
                    include_unavailable=search_arguments.include_unavailable,
                ),
            )
            return [item.model_dump(mode="json") for item in results]
        if tool_name == "list_tools":
            list_arguments = _ListArguments.model_validate(raw_arguments)
            entries = await self._tools.list_tools_for_scope(
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                include_unavailable=list_arguments.include_unavailable,
                max_results=list_arguments.max_results,
            )
            return [item.model_dump(mode="json") for item in entries]
        if tool_name == "get_tool":
            tool_arguments = _ToolArguments.model_validate(raw_arguments)
            return (
                await self._tools.get_tool(
                    tool_arguments.tool_id,
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    agent_id=scope.agent_id,
                )
            ).model_dump(mode="json")
        if tool_name == "reload_tool":
            tool_arguments = _ToolArguments.model_validate(raw_arguments)
            return (
                await self._tools.reload_tool(
                    tool_arguments.tool_id,
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    agent_id=scope.agent_id,
                )
            ).model_dump(mode="json")
        if tool_name == "unload_tool":
            tool_arguments = _ToolArguments.model_validate(raw_arguments)
            await self._tools.unload_tool(
                tool_arguments.tool_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            )
            return {"tool_id": tool_arguments.tool_id, "unloaded": True}
        if tool_name == "search_mcp_tools":
            mcp = self._require_mcp()
            search_mcp_arguments = _SearchMCPToolsArguments.model_validate(raw_arguments)
            return mcp.search_tools(
                search_mcp_arguments.query,
                max_results=search_mcp_arguments.max_results,
            )
        if tool_name == "get_mcp_tool":
            mcp = self._require_mcp()
            get_mcp_arguments = _MCPToolArguments.model_validate(raw_arguments)
            return mcp.get_tool(get_mcp_arguments.tool_id)
        if tool_name == "call_mcp_tool":
            if claimed_intent is None:
                raise ApplicationConflictError(
                    "control_tool_approval_missing",
                    "MCP Tool call lacks an approved durable intent",
            )
            mcp = self._require_mcp()
            call_mcp_arguments = _CallMCPToolArguments.model_validate(raw_arguments)
            return (
                await mcp.invoke(
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    tool_call_id=claimed_intent.id,
                    tool_id=call_mcp_arguments.tool_id,
                    arguments=call_mcp_arguments.arguments,
                )
            ).model_dump(mode="json")
        if tool_name == "search_skills":
            skills = self._require_skills()
            skill_arguments = _SkillSearchArguments.model_validate(raw_arguments)
            results = await skills.search_skills(
                skill_arguments.query,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                capability=skill_arguments.capability,
                max_results=skill_arguments.max_results,
            )
            return [item.model_dump(mode="json") for item in results]
        if tool_name == "list_skills":
            skills = self._require_skills()
            skill_arguments = _SkillListArguments.model_validate(raw_arguments)
            entries = await skills.list_skills(session_id=scope.session_id)
            return [
                item.model_dump(mode="json")
                for item in entries[: skill_arguments.max_results]
            ]
        if tool_name == "load_skill":
            skills = self._require_skills()
            skill_arguments = _LoadSkillArguments.model_validate(raw_arguments)
            document = await skills.select_skill(
                skill_arguments.skill_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                reason=skill_arguments.reason,
            )
            return document.model_dump(mode="json")
        if tool_name == "load_skill_references":
            skills = self._require_skills()
            skill_arguments = _SkillArguments.model_validate(raw_arguments)
            reference = await skills.load_references(
                skill_arguments.skill_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            )
            return reference.model_dump(mode="json")
        if tool_name == "reload_skill":
            skills = self._require_skills()
            skill_arguments = _LoadSkillArguments.model_validate(raw_arguments)
            document = await skills.reload_skill(
                skill_arguments.skill_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                reason=skill_arguments.reason,
            )
            return document.model_dump(mode="json")
        if tool_name == "unload_skill":
            skills = self._require_skills()
            skill_arguments = _SkillArguments.model_validate(raw_arguments)
            await skills.unload_skill(
                skill_arguments.skill_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            )
            return {"skill_id": skill_arguments.skill_id, "active": False}
        if tool_name == "list_techniques":
            techniques = self._require_techniques()
            list_arguments = _SkillListArguments.model_validate(raw_arguments)
            entries = await techniques.list_techniques(session_id=scope.session_id)
            return [
                item.model_dump(mode="json")
                for item in entries[: list_arguments.max_results]
            ]
        if tool_name == "load_technique":
            techniques = self._require_techniques()
            technique_arguments = _LoadTechniqueArguments.model_validate(raw_arguments)
            version = await techniques.select_technique(
                technique_arguments.technique_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                reason=technique_arguments.reason,
            )
            return version.model_dump(mode="json")
        if tool_name == "reload_technique":
            techniques = self._require_techniques()
            technique_arguments = _LoadTechniqueArguments.model_validate(raw_arguments)
            version = await techniques.reload_technique(
                technique_arguments.technique_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                reason=technique_arguments.reason,
            )
            return version.model_dump(mode="json")
        if tool_name == "unload_technique":
            techniques = self._require_techniques()
            technique_arguments = _TechniqueArguments.model_validate(raw_arguments)
            await techniques.unload_technique(
                technique_arguments.technique_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            )
            return {"technique_id": technique_arguments.technique_id, "active": False}
        if tool_name == "list_files":
            code = self._require_code()
            code_arguments = _ListFilesArguments.model_validate(raw_arguments)
            return (
                await code.list_files(
                    scope.run_id,
                    path=code_arguments.path,
                    recursive=code_arguments.recursive,
                    max_entries=code_arguments.max_entries,
                )
            ).model_dump(mode="json")
        if tool_name == "read_file":
            code = self._require_code()
            code_arguments = _ReadFileArguments.model_validate(raw_arguments)
            return (
                await code.read_file(
                    scope.run_id,
                    path=code_arguments.path,
                    offset=code_arguments.offset,
                    max_bytes=code_arguments.max_bytes,
                )
            ).model_dump(mode="json")
        if tool_name == "read_many_files":
            code = self._require_code()
            code_arguments = _ReadManyFilesArguments.model_validate(raw_arguments)
            return (
                await code.read_many_files(
                    scope.run_id,
                    paths=code_arguments.paths,
                    max_bytes_per_file=code_arguments.max_bytes_per_file,
                    max_total_bytes=code_arguments.max_total_bytes,
                )
            ).model_dump(mode="json")
        if tool_name == "glob":
            code = self._require_code()
            code_arguments = _GlobArguments.model_validate(raw_arguments)
            return (
                await code.glob(
                    scope.run_id,
                    pattern=code_arguments.pattern,
                    path=code_arguments.path,
                    max_results=code_arguments.max_results,
                )
            ).model_dump(mode="json")
        if tool_name == "grep":
            code = self._require_code()
            code_arguments = _GrepArguments.model_validate(raw_arguments)
            return (
                await code.grep(
                    scope.run_id,
                    query=code_arguments.query,
                    path=code_arguments.path,
                    file_glob=code_arguments.file_glob,
                    case_sensitive=code_arguments.case_sensitive,
                    max_matches=code_arguments.max_matches,
                )
            ).model_dump(mode="json")
        if tool_name == "symbol_search":
            code = self._require_code()
            symbol_arguments = _SymbolSearchArguments.model_validate(raw_arguments)
            return (
                await code.symbol_search(
                    scope.run_id,
                    query=symbol_arguments.query,
                    path=symbol_arguments.path,
                    file_glob=symbol_arguments.file_glob,
                    case_sensitive=symbol_arguments.case_sensitive,
                    max_results=symbol_arguments.max_results,
                )
            ).model_dump(mode="json")
        if tool_name == "find_references":
            code = self._require_code()
            reference_arguments = _FindReferencesArguments.model_validate(raw_arguments)
            return (
                await code.find_references(
                    scope.run_id,
                    symbol=reference_arguments.symbol,
                    path=reference_arguments.path,
                    file_glob=reference_arguments.file_glob,
                    include_declarations=reference_arguments.include_declarations,
                    max_results=reference_arguments.max_results,
                )
            ).model_dump(mode="json")
        if tool_name == "call_hierarchy":
            code = self._require_code()
            call_arguments = _CallHierarchyArguments.model_validate(raw_arguments)
            return (
                await code.call_hierarchy(
                    scope.run_id,
                    symbol=call_arguments.symbol,
                    direction=call_arguments.direction,
                    path=call_arguments.path,
                    file_glob=call_arguments.file_glob,
                    max_results=call_arguments.max_results,
                )
            ).model_dump(mode="json")
        if tool_name == "diagnostics":
            code = self._require_code()
            diagnostic_arguments = _DiagnosticsArguments.model_validate(raw_arguments)
            return (
                await code.diagnostics(
                    scope.run_id,
                    path=diagnostic_arguments.path,
                    file_glob=diagnostic_arguments.file_glob,
                    max_results=diagnostic_arguments.max_results,
                )
            ).model_dump(mode="json")
        if tool_name == "apply_patch":
            code = self._require_code()
            patch_arguments = _ApplyPatchArguments.model_validate(raw_arguments)
            return (
                await code.apply_patch(
                    scope.run_id,
                    patch=patch_arguments.patch,
                    expected_sha256=patch_arguments.expected_sha256,
                )
            ).model_dump(mode="json")
        if tool_name == "revert_patch":
            code = self._require_code()
            revert_arguments = _RevertPatchArguments.model_validate(raw_arguments)
            return (
                await code.revert_patch(
                    scope.run_id,
                    receipt_artifact_id=revert_arguments.receipt_artifact_id,
                )
            ).model_dump(mode="json")
        if tool_name == "open_browser":
            browser = self._require_browser()
            browser_arguments = _OpenBrowserArguments.model_validate(raw_arguments)
            return _browser_payload(
                await browser.open(
                    OpenBrowser(
                        run_id=scope.run_id,
                        agent_session_id=scope.session_id,
                        url=browser_arguments.url,
                        headless=True,
                        include_screenshot=browser_arguments.include_screenshot,
                    )
                )
            )
        if tool_name == "observe_browser":
            browser = self._require_browser()
            browser_arguments = _ObserveBrowserArguments.model_validate(raw_arguments)
            await self._require_browser_scope(
                scope,
                browser_arguments.browser_session_id,
            )
            return _browser_payload(
                await browser.observe(
                    browser_arguments.browser_session_id,
                    page_id=browser_arguments.page_id,
                    include_screenshot=browser_arguments.include_screenshot,
                    include_network=browser_arguments.include_network,
                )
            )
        if tool_name == "act_browser":
            browser = self._require_browser()
            browser_arguments = _ActBrowserArguments.model_validate(raw_arguments)
            await self._require_browser_scope(
                scope,
                browser_arguments.browser_session_id,
            )
            action = BrowserActionType(browser_arguments.action)
            options: dict[str, JsonValue] = {}
            if action is BrowserActionType.TYPE:
                options["delay_ms"] = browser_arguments.delay_ms
            elif action is BrowserActionType.SCROLL:
                options.update(
                    {
                        "delta_x": browser_arguments.scroll_delta_x,
                        "delta_y": browser_arguments.scroll_delta_y,
                    }
                )
            elif action is BrowserActionType.WAIT:
                options["milliseconds"] = browser_arguments.wait_ms
            return _browser_payload(
                await browser.act(
                    browser_arguments.browser_session_id,
                    ActBrowser(
                        page_id=browser_arguments.page_id,
                        observation_version=browser_arguments.observation_version,
                        action=action,
                        action_key=call_id,
                        element_ref=browser_arguments.element_ref,
                        value=browser_arguments.value,
                        url=browser_arguments.url,
                        options=options,
                        include_screenshot=browser_arguments.include_screenshot,
                    ),
                )
            )
        if tool_name == "close_browser":
            browser = self._require_browser()
            browser_arguments = _CloseBrowserArguments.model_validate(raw_arguments)
            await self._require_browser_scope(
                scope,
                browser_arguments.browser_session_id,
            )
            return _browser_payload(
                await browser.close(browser_arguments.browser_session_id)
            )
        if tool_name == "web_fetch":
            fetcher = self._require_web_fetcher()
            fetch_arguments = _WebFetchArguments.model_validate(raw_arguments)
            return _web_fetch_payload(
                await fetcher.fetch(
                    scope.run_id,
                    FetchRequest(
                        url=fetch_arguments.url,
                        cache_policy=fetch_arguments.cache_policy,
                        redirect_policy=fetch_arguments.redirect_policy,
                        max_response_bytes=fetch_arguments.max_response_bytes,
                        timeout_seconds=fetch_arguments.timeout_seconds,
                        save_raw=fetch_arguments.save_raw,
                        use_browser_fallback=fetch_arguments.use_browser_fallback,
                    ),
                )
            )
        if tool_name == "web_search":
            research = self._require_web_research()
            web_search_arguments = _WebSearchArguments.model_validate(raw_arguments)
            return _web_search_payload(
                await research.search(
                    scope.run_id,
                    scope.session_id,
                    scope.model_profile,
                    SearchRequest(
                        query=web_search_arguments.query,
                        max_results=web_search_arguments.max_results,
                        freshness=web_search_arguments.freshness,
                        allowed_domains=web_search_arguments.allowed_domains,
                        blocked_domains=web_search_arguments.blocked_domains,
                        language=web_search_arguments.language,
                        region=web_search_arguments.region,
                        search_type=web_search_arguments.search_type,
                    ),
                )
            )
        if tool_name == "web_research":
            research = self._require_web_research()
            research_arguments = _WebResearchArguments.model_validate(raw_arguments)
            return _web_research_payload(
                await research.research(
                    ResearchRequest(
                        run_id=scope.run_id,
                        session_id=scope.session_id,
                        question=research_arguments.question,
                        search_type=research_arguments.search_type,
                        allowed_domains=research_arguments.allowed_domains,
                        blocked_domains=research_arguments.blocked_domains,
                        max_queries=research_arguments.max_queries,
                        max_results_per_query=research_arguments.max_results_per_query,
                        max_total_results=research_arguments.max_total_results,
                        max_sources=research_arguments.max_sources,
                    ),
                    model_profile=scope.model_profile,
                )
            )
        if tool_name == "query_http_traffic":
            await self._interactive_run(scope.run_id)
            traffic = self._require_traffic()
            traffic_arguments = _QueryHttpTrafficArguments.model_validate(raw_arguments)
            page = await traffic.list_for_runtime(
                scope.run_id,
                method=traffic_arguments.method,
                status_class=traffic_arguments.status_class,
                limit=traffic_arguments.limit,
                cursor=traffic_arguments.cursor,
            )
            return {
                **page.model_dump(mode="json"),
                "content_trust": "REDACTED_SENSITIVE_METADATA",
            }
        if tool_name == "read_http_exchange":
            await self._interactive_run(scope.run_id)
            traffic = self._require_traffic()
            target_http = self._require_target_http()
            exchange_arguments = _ReadHttpExchangeArguments.model_validate(raw_arguments)
            detail = await traffic.get_for_runtime(
                scope.run_id,
                exchange_arguments.exchange_id,
            )
            target_result = await target_http.get_result(
                scope.run_id,
                exchange_arguments.exchange_id,
            )
            await self._require_target_http_artifacts(scope.run_id, target_result)
            return _http_exchange_payload(detail.model_dump(mode="json"), target_result)
        if tool_name == "target_http_request":
            if claimed_intent is None:
                raise ApplicationConflictError(
                    "control_tool_approval_missing",
                    "Target HTTP request lacks an approved durable intent",
                )
            run = await self._interactive_run(scope.run_id)
            target_http = self._require_target_http()
            request_arguments = _TargetHttpRequestArguments.model_validate(raw_arguments)
            request = TargetHttpRequest(
                execution_key=build_execution_key(
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    tool_call_id=claimed_intent.id,
                    attempt_group="initial",
                ),
                method=request_arguments.method,
                url=request_arguments.url,
                headers=request_arguments.headers,
                query=request_arguments.query,
                body=request_arguments.body,
                json_body=request_arguments.json_body,
                cookies=request_arguments.cookies,
                verify_tls=request_arguments.verify_tls,
                follow_redirects=request_arguments.follow_redirects,
                timeout_seconds=request_arguments.timeout_seconds,
                max_response_bytes=request_arguments.max_response_bytes,
                save_request=True,
                save_response=True,
            )
            target_result = await target_http.execute(
                TargetHttpSubmission(
                    run_id=scope.run_id,
                    session_id=scope.session_id,
                    tool_call_id=claimed_intent.id,
                    node_id=run.node_id,
                    request=request,
                )
            )
            await self._require_target_http_artifacts(scope.run_id, target_result)
            return _target_http_result_payload(target_result)
        if tool_name == "git_status":
            git = self._require_git()
            git_arguments = _GitStatusArguments.model_validate(raw_arguments)
            return (
                await git.status(
                    scope.run_id,
                    max_entries=git_arguments.max_entries,
                )
            ).model_dump(mode="json")
        if tool_name == "git_diff":
            git = self._require_git()
            git_arguments = _GitDiffArguments.model_validate(raw_arguments)
            return (
                await git.diff(
                    scope.run_id,
                    path=git_arguments.path,
                    staged=git_arguments.staged,
                    context_lines=git_arguments.context_lines,
                    max_bytes=git_arguments.max_bytes,
                )
            ).model_dump(mode="json")
        if tool_name == "git_log":
            git = self._require_git()
            git_arguments = _GitLogArguments.model_validate(raw_arguments)
            return (
                await git.log(
                    scope.run_id,
                    path=git_arguments.path,
                    max_entries=git_arguments.max_entries,
                )
            ).model_dump(mode="json")
        if tool_name == "create_worktree":
            self._require_primary_worktree(scope)
            git = self._require_git()
            worktree_arguments = _CreateWorktreeArguments.model_validate(raw_arguments)
            return (
                await git.create_worktree(
                    scope.run_id,
                    name=worktree_arguments.name,
                    start_point=worktree_arguments.start_point,
                )
            ).model_dump(mode="json")
        if tool_name == "get_execution":
            execution_arguments = _ExecutionArguments.model_validate(raw_arguments)
            execution = await self._execution_for_scope(
                scope,
                execution_arguments.execution_id,
            )
            return _execution_payload(execution)
        if tool_name == "wait_execution":
            wait_arguments = _WaitExecutionArguments.model_validate(raw_arguments)
            await self._execution_for_scope(scope, wait_arguments.execution_id)
            result = await self._executions.wait(
                wait_arguments.execution_id,
                timeout_seconds=wait_arguments.timeout_seconds,
                stdout_cursor=wait_arguments.stdout_cursor,
                stderr_cursor=wait_arguments.stderr_cursor,
                max_bytes=wait_arguments.max_bytes,
                next_poll_after_seconds=wait_arguments.next_poll_after_seconds,
            )
            return {
                "wait_status": result.wait_status.value,
                "execution": _execution_payload(result.execution),
                "partial_output": result.partial_output,
                "next_poll_after_seconds": result.next_poll_after_seconds,
                "stdout_cursor": result.stdout_cursor,
                "stderr_cursor": result.stderr_cursor,
            }
        if tool_name == "cancel_execution":
            cancel_arguments = _CancelExecutionArguments.model_validate(raw_arguments)
            await self._execution_for_scope(scope, cancel_arguments.execution_id)
            return _execution_payload(
                await self._executions.cancel(
                    cancel_arguments.execution_id,
                    cancel_arguments.reason,
                )
            )
        if tool_name == "read_artifact":
            artifact_arguments = _ReadArtifactArguments.model_validate(raw_arguments)
            artifact = await self._artifacts.get(artifact_arguments.artifact_id)
            if artifact.run_id != scope.run_id:
                raise ApplicationConflictError(
                    "artifact_run_mismatch",
                    "Artifact is not available to this Run",
                )
            if (
                artifact.audit_id is not None
                and artifact.access_class is not ArtifactAccessClass.AUDIT_INTERNAL
            ):
                raise ApplicationConflictError(
                    "artifact_access_denied",
                    "Artifact is not available to the Agent Runtime",
                )
            artifact_slice = (
                await self._artifacts.read_audit_content_slice(
                    artifact_arguments.artifact_id,
                    audit_id=artifact.audit_id,
                    run_id=scope.run_id,
                    offset=artifact_arguments.offset,
                    max_bytes=artifact_arguments.max_bytes,
                )
                if artifact.audit_id is not None
                else await self._artifacts.read_content_slice(
                    artifact_arguments.artifact_id,
                    expected_run_id=scope.run_id,
                    offset=artifact_arguments.offset,
                    max_bytes=artifact_arguments.max_bytes,
                )
            )
            artifact = artifact_slice.artifact
            encoding, content = _artifact_content(
                artifact.mime_type,
                artifact_slice.data,
            )
            return {
                "artifact_id": artifact.id,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "offset": artifact_arguments.offset,
                "next_offset": artifact_slice.next_offset,
                "eof": artifact_slice.eof,
                "encoding": encoding,
                "content": content,
            }
        if tool_name == "list_ready_tasks":
            planner = self._require_task_planner()
            list_ready_arguments = _ListReadyTasksArguments.model_validate(raw_arguments)
            return [
                task.model_dump(mode="json")
                for task in await planner.list_ready(
                    scope.run_id,
                    limit=list_ready_arguments.limit,
                )
            ]
        if tool_name == "add_task":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            add_task_arguments = _AddTaskArguments.model_validate(raw_arguments)
            return (
                await planner.add_task(
                    AddTaskCommand(
                        run_id=scope.run_id,
                        **add_task_arguments.model_dump(exclude_none=True),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "update_task":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            update_task_arguments = _UpdateTaskArguments.model_validate(raw_arguments)
            return (
                await planner.update_task(
                    UpdateTaskCommand(
                        run_id=scope.run_id,
                        **update_task_arguments.model_dump(exclude_unset=True),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "link_tasks":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            link_tasks_arguments = _LinkTasksArguments.model_validate(raw_arguments)
            return (
                await planner.link_tasks(
                    LinkTasksCommand(
                        run_id=scope.run_id,
                        **link_tasks_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "block_task":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            block_task_arguments = _TaskReasonArguments.model_validate(raw_arguments)
            return (
                await planner.block_task(
                    BlockTaskCommand(
                        run_id=scope.run_id,
                        **block_task_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "claim_ready_task":
            planner = self._require_task_planner()
            claim_task_arguments = _ClaimReadyTaskArguments.model_validate(raw_arguments)
            claim_result = await planner.claim_ready_task(
                ClaimReadyTaskCommand(
                    run_id=scope.run_id,
                    worker_id=self._worker_id,
                    session_id=scope.session_id,
                    preferred_task_id=claim_task_arguments.preferred_task_id,
                )
            )
            return claim_result.model_dump(mode="json") if claim_result is not None else None
        if tool_name == "complete_task":
            planner = self._require_task_planner()
            complete_task_arguments = _CompleteTaskArguments.model_validate(raw_arguments)
            return (
                await planner.complete_task(
                    CompleteTaskCommand(
                        run_id=scope.run_id,
                        actor_session_id=scope.session_id,
                        **complete_task_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "fail_task_attempt":
            planner = self._require_task_planner()
            fail_task_arguments = _FailTaskAttemptArguments.model_validate(raw_arguments)
            return (
                await planner.fail_task_attempt(
                    FailTaskAttemptCommand(
                        run_id=scope.run_id,
                        actor_session_id=scope.session_id,
                        **fail_task_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "reopen_task":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            reopen_task_arguments = _TaskReasonArguments.model_validate(raw_arguments)
            return (
                await planner.reopen_task(
                    ReopenTaskCommand(
                        run_id=scope.run_id,
                        **reopen_task_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "cancel_task":
            self._require_primary_planner(scope)
            planner = self._require_task_planner()
            cancel_task_arguments = _TaskReasonArguments.model_validate(raw_arguments)
            return (
                await planner.cancel_task(
                    CancelTaskCommand(
                        run_id=scope.run_id,
                        actor_session_id=scope.session_id,
                        **cancel_task_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "propose_plan_update":
            self._require_primary_cognition(scope)
            proposals = self._require_working_memory_proposals()
            plan_arguments = _ProposePlanUpdateArguments.model_validate(raw_arguments)
            memory = await proposals.propose_plan_update(
                run_id=scope.run_id,
                expected_memory_version=plan_arguments.expected_memory_version,
                proposal=PlanUpdateProposal(
                    item_updates=plan_arguments.item_updates,
                    current_focus=plan_arguments.current_focus,
                    next_action=plan_arguments.next_action,
                ),
            )
            return {
                "working_memory_id": memory.id,
                "working_memory_version": memory.version,
                "accepted": True,
            }
        if tool_name == "record_attempt":
            self._require_primary_cognition(scope)
            proposals = self._require_working_memory_proposals()
            attempt_arguments = _RecordAttemptArguments.model_validate(raw_arguments)
            memory = await proposals.record_attempt(
                run_id=scope.run_id,
                expected_memory_version=attempt_arguments.expected_memory_version,
                attempt=AttemptRecord(
                    id=attempt_arguments.attempt_id or new_id(),
                    action_signature=attempt_arguments.action_signature,
                    target=attempt_arguments.target,
                    tool_id=attempt_arguments.tool_id,
                    normalized_arguments=attempt_arguments.normalized_arguments,
                    result_status=attempt_arguments.result_status,
                    result_summary=attempt_arguments.result_summary,
                    retryable=attempt_arguments.retryable,
                    retry_of_attempt_id=attempt_arguments.retry_of_attempt_id,
                    retry_reason=attempt_arguments.retry_reason,
                ),
            )
            return {
                "working_memory_id": memory.id,
                "working_memory_version": memory.version,
                "attempt_id": memory.attempts[-1].id,
                "accepted": True,
            }
        if tool_name in {
            "record_observation",
            "propose_fact",
            "propose_hypothesis",
            "propose_finding",
        }:
            self._require_primary_cognition(scope)
            reasoning = self._require_reasoning_proposals()
            argument_type = (
                _ReasoningProposalArguments
                if tool_name == "propose_hypothesis"
                else _EvidenceReasoningProposalArguments
            )
            reasoning_arguments = argument_type.model_validate(raw_arguments)
            kind, status = {
                "record_observation": (
                    ReasoningNodeKind.OBSERVATION,
                    ReasoningNodeStatus.RECORDED,
                ),
                "propose_fact": (
                    ReasoningNodeKind.FACT_CANDIDATE,
                    ReasoningNodeStatus.CANDIDATE,
                ),
                "propose_hypothesis": (
                    ReasoningNodeKind.HYPOTHESIS,
                    ReasoningNodeStatus.UNVERIFIED,
                ),
                "propose_finding": (
                    ReasoningNodeKind.VULNERABILITY_CANDIDATE,
                    ReasoningNodeStatus.CANDIDATE,
                ),
            }[tool_name]
            node = _reasoning_node(scope, reasoning_arguments, kind=kind, status=status)
            graph = await reasoning.create_node(
                node,
                expected_graph_version=reasoning_arguments.expected_graph_version,
            )
            return _reasoning_mutation_payload(graph, node.id)
        if tool_name == "record_negative_result":
            self._require_primary_cognition(scope)
            reasoning = self._require_reasoning_proposals()
            negative_arguments = _RecordNegativeResultArguments.model_validate(raw_arguments)
            node = _reasoning_node(
                scope,
                negative_arguments,
                kind=ReasoningNodeKind.NEGATIVE_RESULT,
                status=ReasoningNodeStatus.RECORDED,
            )
            graph = await reasoning.record_negative_result(
                node,
                invalidated_node_id=negative_arguments.invalidated_node_id,
                expected_graph_version=negative_arguments.expected_graph_version,
            )
            return _reasoning_mutation_payload(graph, node.id)
        if tool_name == "query_reasoning_graph":
            self._require_primary_cognition(scope)
            reasoning = self._require_reasoning_proposals()
            query_arguments = _QueryReasoningGraphArguments.model_validate(raw_arguments)
            return (
                await reasoning.query(
                    QueryReasoningGraph(
                        run_id=scope.run_id,
                        **query_arguments.model_dump(),
                    )
                )
            ).model_dump(mode="json")
        if tool_name == "complete_run":
            complete_arguments = _CompleteRunArguments.model_validate(raw_arguments)
            await self._events.append(
                scope.run_id,
                "agent.completion_requested",
                {
                    "session_id": scope.session_id,
                    "agent_id": scope.agent_id,
                    "run_summary": complete_arguments.run_summary,
                },
            )
            return {
                "completion_requested": True,
                "run_summary": complete_arguments.run_summary,
            }
        raise RuntimeError(f"Unclassified Runtime control tool: {tool_name!r}")

    def _require_skills(self) -> ProgressiveSkillContextManager:
        if self._skills is None:
            raise RuntimeError("Progressive Skill context is not configured")
        return self._skills

    def _require_techniques(self) -> TechniqueContextManager:
        if self._techniques is None:
            raise RuntimeError("Technique context is not configured")
        return self._techniques

    def _require_code(self) -> CodeWorkspaceService:
        if self._code is None:
            raise RuntimeError("Native code workspace is not configured")
        return self._code

    def _require_git(self) -> GitWorkspaceService:
        if self._git is None:
            raise RuntimeError("Native Git workspace is not configured")
        return self._git

    def _require_browser(self) -> BrowserApplicationService:
        if self._browser is None:
            raise RuntimeError("Managed browser service is not configured")
        return self._browser

    def _require_web_fetcher(self) -> PublicWebFetcher:
        if self._web_fetcher is None:
            raise RuntimeError("Public Web Fetch service is not configured")
        return self._web_fetcher

    def _require_web_research(self) -> WebResearchApplicationService:
        if self._web_research is None:
            raise RuntimeError("Web Search and Research service is not configured")
        return self._web_research

    def _require_traffic(self) -> TrafficMetadataApplicationService:
        if self._traffic is None:
            raise RuntimeError("Target HTTP Traffic metadata service is not configured")
        return self._traffic

    def _require_target_http(self) -> TargetHttpApplicationService:
        if self._target_http is None:
            raise RuntimeError("Target HTTP service is not configured")
        return self._target_http

    def _require_mcp(self) -> MCPApplicationService:
        if self._mcp is None:
            raise RuntimeError("MCP application service is not configured")
        return self._mcp

    def _require_task_planner(self) -> TaskPlanner:
        if self._task_planner is None:
            raise RuntimeError("Task Planner is not configured")
        return self._task_planner

    def _require_working_memory_proposals(self) -> WorkingMemoryProposalService:
        if self._working_memory_proposals is None:
            raise RuntimeError("Working Memory Proposal service is not configured")
        return self._working_memory_proposals

    def _require_reasoning_proposals(self) -> ReasoningProposalService:
        if self._reasoning_proposals is None:
            raise RuntimeError("Reasoning Proposal service is not configured")
        return self._reasoning_proposals

    @staticmethod
    def _require_primary_planner(scope: RuntimeToolScope) -> None:
        if scope.agent_id != "primary":
            raise ApplicationConflictError(
                "task_planner_primary_required",
                "Only the Primary Agent may change Task Graph topology",
            )

    @staticmethod
    def _require_primary_cognition(scope: RuntimeToolScope) -> None:
        if scope.agent_id != "primary":
            raise ApplicationConflictError(
                "cognitive_tools_primary_required",
                "Only the Primary Agent may propose authoritative cognitive state",
            )

    @staticmethod
    def _require_primary_worktree(scope: RuntimeToolScope) -> None:
        if scope.agent_id != "primary":
            raise ApplicationConflictError(
                "code_worktree_primary_required",
                "Worktree creation is available only to the Primary Agent",
            )

    async def _interactive_run(self, run_id: str) -> Run:
        if self._runs is None:
            raise RuntimeError("Run repository is not configured for Target HTTP tools")
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return require_interactive_run_operation(run)

    async def _require_target_http_artifacts(
        self,
        run_id: str,
        result: TargetHttpResult,
    ) -> None:
        for artifact_id in (
            result.request_artifact_id,
            result.response_artifact_id,
        ):
            if artifact_id is None:
                continue
            artifact = await self._artifacts.get(artifact_id)
            if artifact.run_id != run_id:
                raise ApplicationConflictError(
                    "target_http_artifact_run_mismatch",
                    "Target HTTP Artifact is not available to this Run",
                )

    async def _require_browser_scope(
        self,
        scope: RuntimeToolScope,
        browser_session_id: str,
    ) -> None:
        view = await self._require_browser().get(
            browser_session_id,
            expected_run_id=scope.run_id,
        )
        if view.session.agent_session_id != scope.session_id:
            raise ApplicationConflictError(
                "browser_scope_mismatch",
                "Browser session is not available to this Agent Session",
            )

    async def _execution_for_scope(
        self,
        scope: RuntimeToolScope,
        execution_id: str,
    ) -> Execution:
        execution = await self._executions.get(execution_id)
        # Execution.session_id is the durable ownership anchor. The trusted
        # agent_id is retained in transcript/audit records, while one Session
        # must never inspect or cancel another Session's live process.
        if execution.run_id != scope.run_id or execution.session_id != scope.session_id:
            raise ApplicationConflictError(
                "execution_scope_mismatch",
                "Execution is not available to this Agent Session",
            )
        return execution


def _reasoning_node(
    scope: RuntimeToolScope,
    arguments: _ReasoningProposalArguments,
    *,
    kind: ReasoningNodeKind,
    status: ReasoningNodeStatus,
) -> ReasoningNode:
    return ReasoningNode(
        id=arguments.node_id or new_id(),
        run_id=scope.run_id,
        session_id=scope.session_id,
        task_id=arguments.task_id,
        kind=kind,
        status=status,
        claim=arguments.claim,
        structured_data=arguments.structured_data,
        evidence_ids=tuple(arguments.evidence_ids),
        creator_type=ReasoningCreatorType.AGENT,
        created_by=scope.agent_id,
    )


def _reasoning_mutation_payload(graph: ReasoningGraph, node_id: str) -> dict[str, object]:
    node = next(item for item in graph.nodes if item.id == node_id)
    return {
        "run_id": graph.run_id,
        "graph_version": graph.version,
        "node": node.model_dump(mode="json"),
        "edges": [
            edge.model_dump(mode="json")
            for edge in graph.edges
            if edge.source_node_id == node_id or edge.target_node_id == node_id
        ],
    }


def _execution_payload(execution: Execution) -> dict[str, object]:
    return {
        "id": execution.id,
        "run_id": execution.run_id,
        "session_id": execution.session_id,
        "tool_call_id": execution.tool_call_id,
        "node_id": execution.node_id,
        "executor_type": execution.executor_type.value,
        "tool_id": execution.tool_id,
        "tool_version": execution.tool_version,
        "status": execution.status.value,
        "exit_code": execution.exit_code,
        "started_at": (
            execution.started_at.isoformat() if execution.started_at is not None else None
        ),
        "finished_at": (
            execution.finished_at.isoformat() if execution.finished_at is not None else None
        ),
    }


def _bounded_result(result: object) -> object:
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    if len(encoded) > _MAX_CONTROL_RESULT_BYTES:
        raise ApplicationConflictError(
            "control_tool_result_too_large",
            "Runtime control tool result exceeded the bounded model context limit",
            details={"result_bytes": len(encoded), "max_bytes": _MAX_CONTROL_RESULT_BYTES},
        )
    return json.loads(encoded)


def _artifact_content(mime_type: str, data: bytes) -> tuple[str, str]:
    textual = mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/javascript",
        "application/xml",
    }
    if textual:
        return "utf-8", data.decode("utf-8", errors="replace")
    try:
        return "utf-8", data.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(data).decode("ascii")


def _browser_payload(view: BrowserView) -> dict[str, object]:
    pages = [
        {
            "id": page.id,
            "url": _truncated(page.url, 2048),
            "title": _truncated(page.title, 500),
            "status": page.status.value,
            "last_observation_version": page.last_observation_version,
        }
        for page in list(view.pages)[:10]
    ]
    observation = view.observation
    action = view.action
    return {
        "session": {
            "id": view.session.id,
            "status": view.session.status.value,
            "owner": view.session.owner.value,
            "current_page_id": view.session.current_page_id,
            "page_ids": view.session.page_ids[:20],
        },
        "pages": pages,
        "pages_truncated": len(view.pages) > len(pages),
        "observation": (
            {
                "id": observation.id,
                "page_id": observation.page_id,
                "url": _truncated(observation.url, 2048),
                "title": _truncated(observation.title, 500),
                "visible_text_excerpt": observation.visible_text_excerpt[:20_000],
                "headings": [
                    _truncated(item, 500) for item in observation.headings[:30]
                ],
                "interactive_elements": [
                    {
                        "ref": item.ref,
                        "role": _truncated(item.role, 128),
                        "name": _truncated(item.name, 300),
                        "text": _truncated(item.text, 300),
                        "input_type": _truncated(item.input_type, 128),
                        "disabled": item.disabled,
                        "href": _truncated(item.href, 1000),
                        "frame_id": _truncated(item.frame_id, 255),
                    }
                    for item in observation.interactive_elements[:30]
                ],
                "forms": [
                    {
                        "ref": form.ref,
                        "action": _truncated(form.action, 1000),
                        "method": _truncated(form.method, 32),
                        "fields": [
                            {
                                "ref": _truncated(field.ref, 128),
                                "name": _truncated(field.name, 128),
                                "label": _truncated(field.label, 200),
                                "input_type": _truncated(field.input_type, 64),
                                "required": field.required,
                            }
                            for field in form.fields[:10]
                        ],
                        "fields_truncated": len(form.fields) > 10,
                    }
                    for form in observation.forms[:5]
                ],
                "alerts": [_truncated(item, 500) for item in observation.alerts[:10]],
                "console_errors": [
                    _truncated(item, 500) for item in observation.console_errors[:10]
                ],
                "recent_network_summary": [
                    {
                        "sequence": item.sequence,
                        "method": item.method,
                        "url": _truncated(item.url, 1500),
                        "resource_type": _truncated(item.resource_type, 128),
                        "status_code": item.status_code,
                        "failed": item.failed,
                        "failure_text": _truncated(item.failure_text, 500),
                    }
                    for item in observation.recent_network_summary[:15]
                ],
                "screenshot_artifact_id": observation.screenshot_artifact_id,
                "network_artifact_id": observation.network_artifact_id,
                "dom_artifact_id": observation.dom_artifact_id,
                "observation_version": observation.observation_version,
                "content_trust": observation.content_trust,
                "truncated": {
                    "headings": len(observation.headings) > 30,
                    "interactive_elements": len(observation.interactive_elements) > 30,
                    "forms": len(observation.forms) > 5,
                    "alerts": len(observation.alerts) > 10,
                    "console_errors": len(observation.console_errors) > 10,
                    "network": len(observation.recent_network_summary) > 15,
                },
            }
            if observation is not None
            else None
        ),
        "action": (
            {
                "id": action.id,
                "action_key": action.action_key,
                "page_id": action.page_id,
                "observation_version": action.observation_version,
                "action": action.action.value,
                "status": action.status.value,
                "result_observation_id": action.result_observation_id,
                "download_artifact_id": action.download_artifact_id,
                "error": _truncated(action.error, 2000),
            }
            if action is not None
            else None
        ),
    }


def _web_fetch_payload(result: FetchResult) -> dict[str, object]:
    document = result.document
    source = result.source
    chunks = [
        {
            "id": chunk.id,
            "sequence": chunk.sequence,
            "heading_path": [_truncated(item, 300) for item in chunk.heading_path[:8]],
            "heading_path_truncated": len(chunk.heading_path) > 8,
            "content_excerpt": chunk.content[:6_000],
            "content_truncated": len(chunk.content) > 6_000,
            "token_count": chunk.token_count,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
        }
        for chunk in result.chunks[:6]
    ]
    return {
        "status": result.status.value,
        "requested_url": _truncated(result.requested_url, 8192),
        "final_url": _truncated(result.final_url, 8192),
        "redirect_url": _truncated(result.redirect_url, 8192),
        "redirect_chain": [_truncated(item, 8192) for item in result.redirect_chain[:10]],
        "redirect_chain_truncated": len(result.redirect_chain) > 10,
        "reason": _truncated(result.reason, 2_000),
        "raw_artifact_id": result.raw_artifact_id,
        "cache_hit": result.cache_hit,
        "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
        "document": (
            {
                "id": document.id,
                "requested_url": _truncated(document.requested_url, 8192),
                "final_url": _truncated(document.final_url, 8192),
                "canonical_url": _truncated(document.canonical_url, 8192),
                "title": _truncated(document.title, 1_000),
                "author": _truncated(document.author, 500),
                "site_name": _truncated(document.site_name, 500),
                "published_at": (
                    document.published_at.isoformat()
                    if document.published_at is not None
                    else None
                ),
                "fetched_at": document.fetched_at.isoformat(),
                "mime_type": document.mime_type,
                "language": _truncated(document.language, 32),
                "raw_artifact_id": document.raw_artifact_id,
                "normalized_artifact_id": document.normalized_artifact_id,
                "content_hash": document.content_hash,
                "text_length": document.text_length,
                "extraction_status": document.extraction_status.value,
                "truncated": document.truncated,
            }
            if document is not None
            else None
        ),
        "source": (
            {
                "id": source.id,
                "document_id": source.document_id,
                "url": _truncated(source.url, 8192),
                "title": _truncated(source.title, 1_000),
                "domain": _truncated(source.domain, 253),
                "author": _truncated(source.author, 500),
                "published_at": (
                    source.published_at.isoformat()
                    if source.published_at is not None
                    else None
                ),
                "fetched_at": source.fetched_at.isoformat(),
                "source_type": source.source_type.value,
                "content_hash": source.content_hash,
            }
            if source is not None
            else None
        ),
        "chunks": chunks,
        "chunks_truncated": len(result.chunks) > len(chunks),
    }


def _web_search_payload(response: SearchResponse) -> dict[str, object]:
    return {
        "query_id": response.query_id,
        "provider": response.provider,
        "query": response.request.query,
        "search_type": response.request.search_type.value,
        "artifact_id": response.artifact_id,
        "warnings": [_truncated(item, 500) for item in response.warnings[:16]],
        "content_trust": response.content_trust,
        "candidate_status": "DISCOVERY_ONLY_NOT_A_CANONICAL_SOURCE",
        "results": [
            {
                "id": result.id,
                "title": _truncated(result.title, 1_000),
                "url": _truncated(str(result.url), 8_192),
                "normalized_url": _truncated(result.normalized_url, 8_192),
                "snippet": _truncated(result.snippet, 6_000),
                "domain": result.domain,
                "published_at": (
                    result.published_at.isoformat() if result.published_at is not None else None
                ),
                "provider": result.provider,
                "provider_rank": result.provider_rank,
            }
            for result in response.results[:20]
        ],
        "results_truncated": len(response.results) > 20,
    }


def _web_research_payload(packet: WebResearchPacket) -> dict[str, object]:
    return {
        "packet_id": packet.id,
        "question": packet.question,
        "summary": packet.summary[:4_000],
        "key_claims": [claim.model_dump(mode="json") for claim in packet.key_claims[:6]],
        "sources": [
            {
                "id": source.id,
                "document_id": source.document_id,
                "url": _truncated(source.url, 8_192),
                "title": _truncated(source.title, 1_000),
                "domain": source.domain,
                "published_at": (
                    source.published_at.isoformat() if source.published_at is not None else None
                ),
                "fetched_at": source.fetched_at.isoformat(),
                "content_hash": source.content_hash,
            }
            for source in packet.sources[:6]
        ],
        "unresolved_questions": [
            _truncated(item, 1_000) for item in packet.unresolved_questions[:16]
        ],
        "search_query_ids": packet.search_query_ids,
        "document_ids": packet.document_ids,
        "artifact_ids": packet.artifact_ids,
        "canonical_sources_only": True,
        "content_trust": packet.content_trust,
    }


def _target_http_result_payload(result: TargetHttpResult) -> dict[str, object]:
    return {
        "exchange_id": result.request_id,
        "execution_key": result.execution_key,
        "request_hash": result.request_hash,
        "status_code": result.status_code,
        "reason_phrase": _truncated(result.reason_phrase, 256),
        "elapsed_ms": result.elapsed_ms,
        "content_type": _truncated(result.content_type, 192),
        "content_length": result.content_length,
        "response_excerpt": _truncated(result.body_excerpt, 8_192),
        "request_artifact_id": result.request_artifact_id,
        "response_artifact_id": result.response_artifact_id,
        "final_url_summary": safe_url_metadata(result.final_url),
        "redirect_summary": safe_redirect_metadata(result.redirect_chain),
        "tls_summary": result.tls_summary,
        "truncated": result.truncated,
        "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
    }


def _http_exchange_payload(
    metadata: dict[str, object],
    result: TargetHttpResult,
) -> dict[str, object]:
    return {
        "exchange_id": result.request_id,
        "metadata": metadata,
        "response": _target_http_result_payload(result),
        "request_artifact_id": result.request_artifact_id,
        "response_artifact_id": result.response_artifact_id,
        "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
    }


def _result_artifact_ids(result: object) -> list[str]:
    artifact_ids: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("artifact_id") and isinstance(item, str) and item:
                    artifact_ids.append(item)
                elif key.endswith("artifact_ids") and isinstance(item, list):
                    artifact_ids.extend(
                        value for value in item if isinstance(value, str) and value
                    )
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(result)
    return list(dict.fromkeys(artifact_ids))


def _truncated(value: str | None, maximum: int) -> str | None:
    return value[:maximum] if value is not None else None


def _source_refs(
    tool_name: str,
    arguments: dict[str, object],
    *,
    result: object | None = None,
) -> list[str]:
    if tool_name == "call_mcp_tool" and isinstance(result, dict):
        mcp_refs: list[str] = []
        if tool_id := _string_argument(result, "tool_id"):
            mcp_refs.append(f"mcp-tool://{tool_id}")
        if execution_key := _string_argument(result, "execution_key"):
            mcp_refs.append(f"mcp-execution://{execution_key}")
        mcp_refs.extend(f"artifact://{item}" for item in _result_artifact_ids(result))
        return list(dict.fromkeys(mcp_refs))
    if tool_name in {
        "query_http_traffic",
        "read_http_exchange",
        "target_http_request",
    } and isinstance(result, dict):
        traffic_refs: list[str] = []
        if isinstance(result.get("exchange_id"), str):
            traffic_refs.append(f"target-http://{result['exchange_id']}")
        items = result.get("items")
        if isinstance(items, list):
            traffic_refs.extend(
                f"target-http://{item['exchange_id']}"
                for item in items
                if isinstance(item, dict) and isinstance(item.get("exchange_id"), str)
            )
        traffic_refs.extend(f"artifact://{item}" for item in _result_artifact_ids(result))
        if traffic_refs:
            return list(dict.fromkeys(traffic_refs))
    if tool_name in {"web_fetch", "web_search", "web_research"} and isinstance(
        result, dict
    ):
        refs: list[str] = []
        if tool_name == "web_search" and isinstance(result.get("query_id"), str):
            refs.append(f"web-search://{result['query_id']}")
        if tool_name == "web_research" and isinstance(result.get("packet_id"), str):
            refs.append(f"web-research://{result['packet_id']}")
        document = result.get("document")
        if isinstance(document, dict) and isinstance(document.get("id"), str):
            refs.append(f"web-document://{document['id']}")
        source = result.get("source")
        if isinstance(source, dict) and isinstance(source.get("id"), str):
            refs.append(f"web-source://{source['id']}")
        sources = result.get("sources")
        if isinstance(sources, list):
            refs.extend(
                f"web-source://{item['id']}"
                for item in sources
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
        refs.extend(f"artifact://{item}" for item in _result_artifact_ids(result))
        if refs:
            return list(dict.fromkeys(refs))
    if tool_name in {
        "open_browser",
        "observe_browser",
        "act_browser",
        "close_browser",
    } and isinstance(result, dict):
        refs: list[str] = []
        session = result.get("session")
        if isinstance(session, dict) and isinstance(session.get("id"), str):
            refs.append(f"browser://{session['id']}")
        observation = result.get("observation")
        if isinstance(observation, dict) and isinstance(observation.get("page_id"), str):
            refs.append(f"browser-page://{observation['page_id']}")
        refs.extend(f"artifact://{item}" for item in _result_artifact_ids(result))
        if refs:
            return list(dict.fromkeys(refs))
    if tool_name in {"apply_patch", "revert_patch"} and isinstance(result, dict):
        refs: list[str] = []
        if path := _string_argument(result, "path"):
            refs.append(f"code://{path}")
        if receipt_id := _string_argument(result, "receipt_artifact_id"):
            refs.append(f"artifact://{receipt_id}")
        if refs:
            return refs
    if tool_name == "create_worktree" and isinstance(result, dict):
        worktree_refs: list[str] = []
        if path := _string_argument(result, "path"):
            worktree_refs.append(f"worktree://{path}")
        if commit := _string_argument(result, "head_commit"):
            worktree_refs.append(f"git-commit://{commit}")
        if worktree_refs:
            return worktree_refs
    if execution_id := _string_argument(arguments, "execution_id"):
        return [f"execution://{execution_id}"]
    if artifact_id := _string_argument(arguments, "artifact_id"):
        return [f"artifact://{artifact_id}"]
    if tool_id := _string_argument(arguments, "tool_id"):
        return [f"tool://{tool_id}"]
    if skill_id := _string_argument(arguments, "skill_id"):
        return [f"skill://{skill_id}"]
    if technique_id := _string_argument(arguments, "technique_id"):
        return [f"technique://{technique_id}"]
    if path := _string_argument(arguments, "path"):
        return [f"code://{path}"]
    paths = arguments.get("paths")
    if isinstance(paths, list):
        return [f"code://{path}" for path in paths if isinstance(path, str) and path]
    return [f"runtime-tool://{tool_name}"]


def _string_argument(arguments: dict[str, object], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None
