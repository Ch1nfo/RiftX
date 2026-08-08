"""Run-authorized MCP Tool invocation with durable result evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.run_kind_effects import (
    EffectMode,
    EffectOrigin,
    OperationEffect,
    RunEffectOperation,
)
from riftx.application.services.runs import require_run_kind_effect_operation
from riftx.domain import Run, RunStatus
from riftx.execution import build_execution_key
from riftx.runtime.types import ToolCallIntent, ToolCallStatus

from .models import MCPInvocationResult
from .registry import MCPServerRegistry, MCPToolInvocationError

_BLOCKED_RUN_STATUSES = frozenset(
    {
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }
)
_MAX_MODEL_BLOCKS = 20
_MAX_MODEL_TEXT_CHARS = 16_000
_MAX_MODEL_STRUCTURED_BYTES = 64 * 1024


class MCPRunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...


class MCPArtifactStore(Protocol):
    async def save(
        self,
        run_id: str,
        *,
        name: str,
        mime_type: str,
        content: bytes,
        description: str,
    ) -> str: ...


class MCPToolCallRepository(Protocol):
    async def get(self, intent_id: str) -> ToolCallIntent | None: ...

    async def execution_claim_is_current(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> bool: ...


class MCPApplicationService:
    def __init__(
        self,
        *,
        registry: MCPServerRegistry,
        runs: MCPRunRepository,
        tool_calls: MCPToolCallRepository,
        artifacts: MCPArtifactStore,
    ) -> None:
        self._registry = registry
        self._runs = runs
        self._tool_calls = tool_calls
        self._artifacts = artifacts

    def search_tools(self, query: str, *, max_results: int = 20) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in self._registry.index.search(query, max_results=max_results)
        ]

    def get_tool(self, tool_id: str) -> dict[str, object]:
        return {
            "entry": self._registry.index.get(tool_id).model_dump(mode="json"),
            "schema": self._registry.index.schema(tool_id).model_dump(mode="json"),
        }

    async def invoke(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        tool_id: str,
        arguments: dict[str, object],
    ) -> MCPInvocationResult:
        execution_key = build_execution_key(
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            attempt_group="mcp",
        )
        await self._require_authorized_effect(
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            arguments=arguments,
            execution_key=execution_key,
        )
        try:
            entry, result = await self._registry.invoke(
                tool_id,
                arguments,
                execution_key=execution_key,
            )
        except MCPToolInvocationError as exc:
            raise ApplicationConflictError(
                exc.code,
                "MCP Tool invocation was rejected or unavailable",
            ) from None

        await self._require_authorized_effect(
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            arguments=arguments,
            execution_key=execution_key,
        )
        encoded_result = _canonical_bytes(result)
        result_sha256 = hashlib.sha256(encoded_result).hexdigest()
        envelope = _canonical_bytes(
            {
                "schema_version": "riftx.mcp-result/v1",
                "run_id": run_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "execution_key": execution_key,
                "tool_id": entry.id,
                "server_id": entry.server_id,
                "tool_name": entry.name,
                "result_sha256": result_sha256,
                "result": result,
            }
        )
        artifact_id = await self._artifacts.save(
            run_id,
            name=f"mcp-{entry.id}-{execution_key[-12:]}.json",
            mime_type="application/json",
            content=envelope,
            description=f"Sanitized MCP Tool result for {entry.id}",
        )
        content, content_truncated = _content_preview(result.get("content"))
        structured, structured_truncated = _structured_preview(
            result.get("structuredContent")
        )
        return MCPInvocationResult(
            tool_call_id=tool_call_id,
            execution_key=execution_key,
            tool_id=entry.id,
            server_id=entry.server_id,
            tool_name=entry.name,
            status="error" if result.get("isError") is True else "completed",
            artifact_id=artifact_id,
            result_sha256=result_sha256,
            result_bytes=len(encoded_result),
            content=content,
            content_truncated=content_truncated,
            structured_content=structured,
            structured_content_truncated=structured_truncated,
        )

    async def _require_effects_allowed(self, run_id: str) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_run_kind_effect_operation(
            run,
            operation=RunEffectOperation.SERVICE_MCP_INVOKE,
            origin=EffectOrigin.APPLICATION_SERVICE,
            effect=OperationEffect.HOST_EXECUTION,
            mode=EffectMode.NORMAL,
        )
        if run.status in _BLOCKED_RUN_STATUSES:
            raise ApplicationConflictError(
                "run_mcp_invocation_blocked",
                f"Run {run.id!r} cannot invoke MCP Tools while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run

    async def _require_authorized_effect(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        tool_id: str,
        arguments: dict[str, object],
        execution_key: str,
    ) -> None:
        await self._require_effects_allowed(run_id)
        intent = await self._tool_calls.get(tool_call_id)
        expected_arguments = {"tool_id": tool_id, "arguments": arguments}
        if (
            intent is None
            or intent.run_id != run_id
            or intent.session_id != session_id
            or intent.tool_id != "call_mcp_tool"
            or intent.execution_spec is not None
            or intent.status is not ToolCallStatus.EXECUTING
            or _canonical_bytes(intent.arguments) != _canonical_bytes(expected_arguments)
            or not await self._tool_calls.execution_claim_is_current(
                tool_call_id,
                execution_key=execution_key,
                attempt_group="mcp",
            )
        ):
            raise ApplicationConflictError(
                "mcp_tool_call_not_authorized",
                "MCP Tool invocation lacks its exact approved durable execution claim",
            )


def _content_preview(value: object) -> tuple[list[dict[str, object]], bool]:
    if not isinstance(value, list):
        return [], False
    preview: list[dict[str, object]] = []
    for block in value[:_MAX_MODEL_BLOCKS]:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                preview.append(
                    {
                        "type": "text",
                        "text": text[:_MAX_MODEL_TEXT_CHARS],
                        "truncated": len(text) > _MAX_MODEL_TEXT_CHARS,
                    }
                )
        elif block_type in {"image", "audio"}:
            data = block.get("data")
            preview.append(
                {
                    "type": block_type,
                    "mime_type": block.get("mimeType"),
                    "encoded_characters": len(data) if isinstance(data, str) else 0,
                    "data_omitted": True,
                }
            )
        elif block_type == "resource_link":
            preview.append(
                {
                    "type": "resource_link",
                    "name": _truncated(block.get("name"), 500),
                    "title": _truncated(block.get("title"), 500),
                    "uri": _truncated(block.get("uri"), 4096),
                    "description": _truncated(block.get("description"), 2000),
                    "mime_type": _truncated(block.get("mimeType"), 256),
                    "size": block.get("size") if isinstance(block.get("size"), int) else None,
                }
            )
        elif block_type == "resource":
            preview.append(_resource_preview(block))
    return preview, len(value) > _MAX_MODEL_BLOCKS


def _resource_preview(block: dict[str, object]) -> dict[str, object]:
    resource = block.get("resource")
    if not isinstance(resource, dict):
        return {"type": "resource", "content_omitted": True}
    payload: dict[str, object] = {
        "type": "resource",
        "uri": _truncated(resource.get("uri"), 4096),
        "mime_type": _truncated(resource.get("mimeType"), 256),
    }
    text = resource.get("text")
    if isinstance(text, str):
        payload.update(
            text=text[:_MAX_MODEL_TEXT_CHARS],
            truncated=len(text) > _MAX_MODEL_TEXT_CHARS,
        )
    else:
        blob = resource.get("blob")
        payload.update(
            encoded_characters=len(blob) if isinstance(blob, str) else 0,
            data_omitted=True,
        )
    return payload


def _structured_preview(value: object) -> tuple[dict[str, object] | None, bool]:
    if not isinstance(value, dict):
        return None, False
    encoded = _canonical_bytes(value)
    if len(encoded) <= _MAX_MODEL_STRUCTURED_BYTES:
        return value, False
    return {
        "truncated": True,
        "result_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }, True


def _truncated(value: object, maximum: int) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
