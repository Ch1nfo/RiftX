"""Scope and approval gate for idempotent Runner Target HTTP requests."""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.ports import RunEventRepository
from riftx.application.services.artifacts import (
    ArtifactApplicationService,
    RegisterArtifactContent,
)
from riftx.execution import build_execution_key
from riftx.persistence.repositories import SQLAlchemyRunRepository
from riftx.persistence.runtime_repositories import SQLAlchemyToolCallIntentRepository
from riftx.runtime.types import ToolCallStatus
from riftx.scope import ScopeGuard, ScopeTargetKind

from .models import (
    TargetHttpExchange,
    TargetHttpResult,
    TargetHttpRunnerRequest,
    TargetHttpSubmission,
)


class TargetHttpRunner(Protocol):
    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange: ...


class TargetHttpRequestRepository(Protocol):
    async def get_by_execution_key(self, execution_key: str) -> TargetHttpResult | None: ...

    async def create(
        self,
        submission: TargetHttpSubmission,
        result: TargetHttpResult,
    ) -> TargetHttpResult: ...


class TargetHttpApplicationService:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        tool_calls: SQLAlchemyToolCallIntentRepository,
        requests: TargetHttpRequestRepository,
        runner: TargetHttpRunner,
        artifacts: ArtifactApplicationService,
        events: RunEventRepository | None = None,
    ) -> None:
        self._runs = runs
        self._tool_calls = tool_calls
        self._requests = requests
        self._runner = runner
        self._artifacts = artifacts
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    async def execute(self, submission: TargetHttpSubmission) -> TargetHttpResult:
        request = submission.request
        expected_key = build_execution_key(
            run_id=submission.run_id,
            session_id=submission.session_id,
            tool_call_id=submission.tool_call_id,
            attempt_group="initial",
        )
        if request.execution_key != expected_key:
            raise ApplicationConflictError(
                "target_http_execution_key_mismatch",
                "Target HTTP execution key does not match its Run/Session/Tool identity",
            )
        lock = self._locks.setdefault(request.execution_key, asyncio.Lock())
        self._lock_users[request.execution_key] = self._lock_users.get(request.execution_key, 0) + 1
        async with lock:
            try:
                existing = await self._requests.get_by_execution_key(request.execution_key)
                if existing is not None:
                    if existing.request_hash != request.fingerprint:
                        raise ApplicationConflictError(
                            "target_http_idempotency_conflict",
                            "Target HTTP execution key was already used for another request",
                        )
                    return existing
                run = await self._runs.get(submission.run_id)
                if run is None:
                    raise EntityNotFoundError("Run", submission.run_id)
                if run.node_id != submission.node_id:
                    raise ApplicationConflictError(
                        "target_http_node_mismatch",
                        "Target HTTP must execute on the Run's Runner node",
                    )
                ScopeGuard(run.scope).require(request.url, kind=ScopeTargetKind.URL)
                intent = await self._tool_calls.get(submission.tool_call_id)
                if (
                    intent is None
                    or intent.run_id != submission.run_id
                    or intent.session_id != submission.session_id
                ):
                    raise EntityNotFoundError("ToolCallIntent", submission.tool_call_id)
                if intent.status not in {
                    ToolCallStatus.READY,
                    ToolCallStatus.EXECUTING,
                }:
                    raise ApplicationConflictError(
                        "target_http_not_approved",
                        "Target HTTP Tool Call is not approved for execution",
                    )
                if intent.status is ToolCallStatus.READY:
                    intent.status = ToolCallStatus.EXECUTING
                    await self._tool_calls.save(intent)
                await self._event(
                    submission.run_id,
                    "target_http.request_started",
                    {"execution_key": request.execution_key, "url": request.url},
                )
                try:
                    exchange = await self._runner.execute(
                        TargetHttpRunnerRequest(
                            run_id=submission.run_id,
                            session_id=submission.session_id,
                            tool_call_id=submission.tool_call_id,
                            node_id=submission.node_id,
                            scope=run.scope,
                            request=request,
                        )
                    )
                    result = await self._save_artifacts(submission, exchange)
                    result = await self._requests.create(submission, result)
                except Exception as exc:
                    intent.status = ToolCallStatus.FAILED
                    await self._tool_calls.save(intent)
                    await self._event(
                        submission.run_id,
                        "target_http.request_failed",
                        {
                            "execution_key": request.execution_key,
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
                intent.status = ToolCallStatus.COMPLETED
                await self._tool_calls.save(intent)
                await self._event(
                    submission.run_id,
                    "target_http.response_received",
                    {
                        "execution_key": request.execution_key,
                        "request_id": result.request_id,
                        "status_code": result.status_code,
                    },
                )
                return result
            finally:
                self._lock_users[request.execution_key] -= 1
                if self._lock_users[request.execution_key] == 0:
                    self._lock_users.pop(request.execution_key, None)
                    self._locks.pop(request.execution_key, None)

    async def _save_artifacts(self, submission, exchange):
        request = submission.request
        result = exchange.result
        request_artifact_id = None
        response_artifact_id = None
        if request.save_request:
            artifact = await self._artifacts.register_content(
                submission.run_id,
                RegisterArtifactContent(
                    content=json.dumps(
                        request.runner_payload(), ensure_ascii=False, indent=2
                    ).encode(),
                    name=f"target-http-{result.request_id}-request.json",
                    mime_type="application/json",
                    description="Immutable Target HTTP request",
                ),
            )
            request_artifact_id = artifact.id
        if request.save_response:
            artifact = await self._artifacts.register_content(
                submission.run_id,
                RegisterArtifactContent(
                    content=exchange.response_body,
                    name=f"target-http-{result.request_id}-response.bin",
                    mime_type=result.content_type or "application/octet-stream",
                    description="Immutable Target HTTP response body",
                ),
            )
            response_artifact_id = artifact.id
        return result.model_copy(
            update={
                "request_artifact_id": request_artifact_id,
                "response_artifact_id": response_artifact_id,
            }
        )

    async def _event(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload)
