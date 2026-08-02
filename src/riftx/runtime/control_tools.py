"""Run-scoped inline control tools for the production Agent Runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from riftx.application.errors import ApplicationConflictError, ApplicationServiceError
from riftx.application.services import ArtifactApplicationService
from riftx.domain import (
    AgentMessage,
    Execution,
    MessageRole,
    MessageType,
    MessageVisibility,
    TranscriptMessageDraft,
)
from riftx.execution import ExecutionService
from riftx.runtime.engine.agent_factory import RuntimeToolScope
from riftx.tools import ToolContextManager, ToolSearchRequest

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
    ) -> None:
        self._tools = tools
        self._executions = executions
        self._artifacts = artifacts
        self._events = events
        self._transcript = transcript

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
        try:
            result = await self._invoke(scope, tool_name, arguments)
            result = _bounded_result(result)
        except Exception as exc:
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

        content = {
            "type": "tool_result",
            "tool": tool_name,
            "tool_call_id": call_id,
            "status": "completed",
            "content": result,
            "source_refs": _source_refs(tool_name, arguments),
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
                artifact_ids=(
                    [artifact_id]
                    if (artifact_id := _string_argument(arguments, "artifact_id")) is not None
                    else []
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
    ) -> object:
        if tool_name == "search_tools":
            arguments = _SearchArguments.model_validate(raw_arguments)
            results = self._tools.search_tools(
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                request=ToolSearchRequest(
                    query=arguments.query,
                    capability=arguments.capability,
                    max_results=arguments.max_results,
                    include_unavailable=arguments.include_unavailable,
                ),
            )
            return [item.model_dump(mode="json") for item in results]
        if tool_name == "list_tools":
            arguments = _ListArguments.model_validate(raw_arguments)
            entries = self._tools.list_tools_for_scope(
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                include_unavailable=arguments.include_unavailable,
                max_results=arguments.max_results,
            )
            return [item.model_dump(mode="json") for item in entries]
        if tool_name == "get_tool":
            arguments = _ToolArguments.model_validate(raw_arguments)
            return self._tools.get_tool(
                arguments.tool_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            ).model_dump(mode="json")
        if tool_name == "get_execution":
            arguments = _ExecutionArguments.model_validate(raw_arguments)
            execution = await self._execution_for_scope(scope, arguments.execution_id)
            return _execution_payload(execution)
        if tool_name == "wait_execution":
            arguments = _WaitExecutionArguments.model_validate(raw_arguments)
            await self._execution_for_scope(scope, arguments.execution_id)
            result = await self._executions.wait(
                arguments.execution_id,
                timeout_seconds=arguments.timeout_seconds,
                stdout_cursor=arguments.stdout_cursor,
                stderr_cursor=arguments.stderr_cursor,
                max_bytes=arguments.max_bytes,
                next_poll_after_seconds=arguments.next_poll_after_seconds,
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
            arguments = _CancelExecutionArguments.model_validate(raw_arguments)
            await self._execution_for_scope(scope, arguments.execution_id)
            return _execution_payload(
                await self._executions.cancel(arguments.execution_id, arguments.reason)
            )
        if tool_name == "read_artifact":
            arguments = _ReadArtifactArguments.model_validate(raw_arguments)
            artifact = await self._artifacts.get(arguments.artifact_id)
            if artifact.run_id != scope.run_id:
                raise ApplicationConflictError(
                    "artifact_run_mismatch",
                    "Artifact is not available to this Run",
                )
            artifact, path = await self._artifacts.content_path(arguments.artifact_id)
            data, eof = await asyncio.to_thread(
                _read_slice,
                path,
                arguments.offset,
                arguments.max_bytes,
            )
            encoding, content = _artifact_content(artifact.mime_type, data)
            return {
                "artifact_id": artifact.id,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "offset": arguments.offset,
                "next_offset": arguments.offset + len(data),
                "eof": eof,
                "encoding": encoding,
                "content": content,
            }
        if tool_name == "complete_run":
            arguments = _CompleteRunArguments.model_validate(raw_arguments)
            await self._events.append(
                scope.run_id,
                "agent.completion_requested",
                {
                    "session_id": scope.session_id,
                    "agent_id": scope.agent_id,
                    "run_summary": arguments.run_summary,
                },
            )
            return {
                "completion_requested": True,
                "run_summary": arguments.run_summary,
            }
        raise RuntimeError(f"Unclassified Runtime control tool: {tool_name!r}")

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


def _read_slice(path: Path, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(max_bytes + 1)
    return data[:max_bytes], len(data) <= max_bytes


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


def _source_refs(tool_name: str, arguments: dict[str, object]) -> list[str]:
    if execution_id := _string_argument(arguments, "execution_id"):
        return [f"execution://{execution_id}"]
    if artifact_id := _string_argument(arguments, "artifact_id"):
        return [f"artifact://{artifact_id}"]
    if tool_id := _string_argument(arguments, "tool_id"):
        return [f"tool://{tool_id}"]
    return [f"runtime-tool://{tool_name}"]


def _string_argument(arguments: dict[str, object], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None
