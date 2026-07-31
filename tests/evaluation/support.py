from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from riftx.domain import (
    BrowserAction,
    BrowserObservation,
    BrowserPage,
    Execution,
    ExecutionStatus,
    InteractiveElement,
    NetworkEventSummary,
)
from riftx.evaluation import OneShotFaultInjector, RecoveryBoundary
from riftx.runner import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from riftx.runtime.engine import AgentEngineEvent, AgentEngineEventType, AgentEngineState


class EvaluationEngineRun:
    async def events(self) -> AsyncIterator[AgentEngineEvent]:
        yield AgentEngineEvent(sequence=1, event_type=AgentEngineEventType.RUN_STARTED)
        yield AgentEngineEvent(
            sequence=2,
            event_type=AgentEngineEventType.TOOL_CALL_READY,
            data={
                "call_id": "qa-recovery-tool-call",
                "tool_id": "qa-recovery-probe",
                "arguments": {},
                "approval_level": "never",
            },
        )
        yield AgentEngineEvent(sequence=3, event_type=AgentEngineEventType.RUN_COMPLETED)

    async def suspend(self) -> AgentEngineState:
        return AgentEngineState(
            engine_type="qa",
            engine_version="1",
            provider="qa",
            model="model-a",
            serialized_state={"recoverable": True},
        )

    async def cancel(self) -> None:
        return None


class EvaluationEngine:
    def __init__(self) -> None:
        self.model_calls = 0

    async def start(self, request: object) -> EvaluationEngineRun:
        self.model_calls += 1
        return EvaluationEngineRun()

    async def resume(self, request: object) -> EvaluationEngineRun:
        self.model_calls += 1
        return EvaluationEngineRun()


class DurableEvaluationRunner:
    """Fast Runner double whose authoritative state is the real Execution repository."""

    def __init__(self, repository: Any, launches: dict[str, int]) -> None:
        self._repository = repository
        self._launches = launches

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        if effect_guard is not None:
            await effect_guard()
        execution = Execution(
            execution_key=request.execution_key,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            status=ExecutionStatus.QUEUED,
            stdout_path=str(Path(request.cwd) / f"{request.tool_call_id}.stdout"),
            stderr_path=str(Path(request.cwd) / f"{request.tool_call_id}.stderr"),
        )
        execution, created = await self._repository.create_if_absent(execution)
        if not created:
            return execution
        tool_call_id = request.tool_call_id or "unknown"
        self._launches[tool_call_id] = self._launches.get(tool_call_id, 0) + 1
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        return await self._repository.save(execution)

    async def get(self, execution_id: str) -> Execution:
        execution = await self._repository.get(execution_id)
        assert execution is not None
        return execution

    async def wait(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            raw_index = (execution.tool_id or "qa-tool-999").rsplit("-", 1)[-1]
            failed = raw_index.isdigit() and int(raw_index) < 10
            execution.transition_to(
                ExecutionStatus.FAILED if failed else ExecutionStatus.COMPLETED,
                exit_code=1 if failed else 0,
            )
            await self._repository.save(execution)
        return execution

    async def cancel(self, execution_id: str) -> Execution:
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            execution.transition_to(ExecutionStatus.CANCELLED)
            await self._repository.save(execution)
        return execution

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.FAILED:
            stdout = b""
            stderr = f"expected failure for {execution.tool_id}\n".encode()
        else:
            stdout = f"completed {execution.tool_id}\n".encode()
            stderr = b""
        stdout = stdout[:max_bytes]
        stderr = stderr[: max(0, max_bytes - len(stdout))]
        return ExecutionOutput(
            stdout=OutputSlice(
                data=stdout,
                cursor=stdout_cursor,
                next_cursor=stdout_cursor + len(stdout),
                eof=True,
            ),
            stderr=OutputSlice(
                data=stderr,
                cursor=stderr_cursor,
                next_cursor=stderr_cursor + len(stderr),
                eof=True,
            ),
        )


class FaultingExecutionRunner:
    def __init__(
        self,
        delegate: DurableEvaluationRunner,
        injector: OneShotFaultInjector,
    ) -> None:
        self._delegate = delegate
        self._injector = injector

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        execution = await self._delegate.start(request, effect_guard=effect_guard)
        self._injector.trip(RecoveryBoundary.AFTER_EXECUTION_STARTED)
        return execution

    async def get(self, execution_id: str) -> Execution:
        return await self._delegate.get(execution_id)

    async def wait(self, execution_id: str) -> Execution:
        execution = await self._delegate.wait(execution_id)
        self._injector.trip(RecoveryBoundary.AFTER_EXECUTION_COMPLETED)
        return execution

    async def cancel(self, execution_id: str) -> Execution:
        return await self._delegate.cancel(execution_id)

    async def read_output(self, execution_id: str, **kwargs: object) -> ExecutionOutput:
        return await self._delegate.read_output(execution_id, **kwargs)


class RecordingWorkflow:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str, str]] = []

    async def approve(self, run_id: str, approval_id: str) -> None:
        self.signals.append(("approve", run_id, approval_id))

    async def reject(self, run_id: str, approval_id: str) -> None:
        self.signals.append(("reject", run_id, approval_id))


class EvaluationBrowserSession:
    profile_path = None

    def __init__(self, session_id: str, url: str, action_counts: dict[str, int]) -> None:
        self.session_id = session_id
        self.url = url
        self._action_counts = action_counts
        self._downloads: list[object] = []

    async def pages(self) -> list[BrowserPage]:
        return [
            BrowserPage(
                id="qa-page-1",
                browser_session_id=self.session_id,
                url=self.url,
                title="QA target",
            )
        ]

    async def observe(
        self,
        page_id: str,
        *,
        browser_session_id: str,
        version: int,
        include_screenshot: bool,
        include_network: bool,
    ) -> tuple[BrowserPage, BrowserObservation, bytes]:
        page = (await self.pages())[0]
        page.last_observation_version = version
        return (
            page,
            BrowserObservation(
                browser_session_id=browser_session_id,
                page_id=page_id,
                url=self.url,
                title="QA target",
                visible_text_excerpt="bounded observation",
                interactive_elements=[
                    InteractiveElement(ref="e-1", role="button", text="Continue")
                ],
                recent_network_summary=(
                    [
                        NetworkEventSummary(
                            sequence=version,
                            method="GET",
                            url=self.url,
                            status_code=200,
                        )
                    ]
                    if include_network
                    else []
                ),
                observation_version=version,
            ),
            b"qa-png" if include_screenshot else b"",
        )

    async def act(self, action: BrowserAction) -> tuple[None, bytes]:
        self._action_counts[action.action_key] = self._action_counts.get(action.action_key, 0) + 1
        self.url = "https://example.com/continued"
        return None, b""

    async def storage_digest(self) -> str:
        return "qa-storage"

    async def download_count(self) -> int:
        return len(self._downloads)

    async def downloads_since(self, index: int) -> list[object]:
        return self._downloads[index:]

    async def close(self) -> None:
        return None


class EvaluationBrowserEngine:
    def __init__(self, action_counts: dict[str, int]) -> None:
        self._action_counts = action_counts

    async def open(self, command: Any) -> EvaluationBrowserSession:
        return EvaluationBrowserSession(command.session_id, command.url, self._action_counts)


class FaultingBrowserRunner:
    def __init__(self, delegate: Any, injector: OneShotFaultInjector) -> None:
        self._delegate = delegate
        self._injector = injector

    async def open(self, command: Any) -> Any:
        return await self._delegate.open(command)

    async def observe(self, command: Any) -> Any:
        return await self._delegate.observe(command)

    async def act(self, command: Any) -> Any:
        result = await self._delegate.act(command)
        self._injector.trip(RecoveryBoundary.DURING_BROWSER_ACTION)
        return result

    async def takeover(self, command: Any) -> Any:
        return await self._delegate.takeover(command)

    async def release(self, command: Any) -> Any:
        return await self._delegate.release(command)

    async def close(self, command: Any) -> Any:
        return await self._delegate.close(command)


def digest_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
