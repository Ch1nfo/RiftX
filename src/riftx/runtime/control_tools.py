"""Run-scoped inline control tools for the production Agent Runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from riftx.application.errors import ApplicationConflictError, ApplicationServiceError
from riftx.application.services import ArtifactApplicationService
from riftx.browser.service import ActBrowser, BrowserApplicationService, BrowserView, OpenBrowser
from riftx.code import CodeWorkspaceService, GitWorkspaceService
from riftx.domain import (
    AgentMessage,
    ArtifactAccessClass,
    BrowserActionType,
    Execution,
    MessageRole,
    MessageType,
    MessageVisibility,
    TranscriptMessageDraft,
)
from riftx.execution import ExecutionService
from riftx.runtime.engine.agent_factory import RuntimeToolScope
from riftx.skills import ProgressiveSkillContextManager
from riftx.tools import ToolContextManager, ToolSearchRequest
from riftx.tools.policy import AGENT_TOOL_POLICIES

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
    ) -> bool: ...

    async def finish_control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
        succeeded: bool,
    ) -> None: ...


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
        code: CodeWorkspaceService | None = None,
        git: GitWorkspaceService | None = None,
        browser: BrowserApplicationService | None = None,
        control_intents: ControlIntentTracker | None = None,
    ) -> None:
        self._tools = tools
        self._executions = executions
        self._artifacts = artifacts
        self._events = events
        self._transcript = transcript
        self._skills = skills
        self._code = code
        self._git = git
        self._browser = browser
        self._control_intents = control_intents

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
        try:
            policy = AGENT_TOOL_POLICIES.get(tool_name)
            if policy is not None and policy.approval_required:
                approved = self._control_intents is not None and await (
                    self._control_intents.begin_control_intent(
                        run_id=scope.run_id,
                        session_id=scope.session_id,
                        engine_call_id=call_id,
                    )
                )
                if not approved:
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
    ) -> object:
        if tool_name == "search_tools":
            search_arguments = _SearchArguments.model_validate(raw_arguments)
            results = self._tools.search_tools(
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
            entries = self._tools.list_tools_for_scope(
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                include_unavailable=list_arguments.include_unavailable,
                max_results=list_arguments.max_results,
            )
            return [item.model_dump(mode="json") for item in entries]
        if tool_name == "get_tool":
            tool_arguments = _ToolArguments.model_validate(raw_arguments)
            return self._tools.get_tool(
                tool_arguments.tool_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
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


def _result_artifact_ids(result: object) -> list[str]:
    artifact_ids: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("artifact_id") and isinstance(item, str) and item:
                    artifact_ids.append(item)
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
    if execution_id := _string_argument(arguments, "execution_id"):
        return [f"execution://{execution_id}"]
    if artifact_id := _string_argument(arguments, "artifact_id"):
        return [f"artifact://{artifact_id}"]
    if tool_id := _string_argument(arguments, "tool_id"):
        return [f"tool://{tool_id}"]
    if skill_id := _string_argument(arguments, "skill_id"):
        return [f"skill://{skill_id}"]
    if path := _string_argument(arguments, "path"):
        return [f"code://{path}"]
    paths = arguments.get("paths")
    if isinstance(paths, list):
        return [f"code://{path}" for path in paths if isinstance(path, str) and path]
    return [f"runtime-tool://{tool_name}"]


def _string_argument(arguments: dict[str, object], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None
