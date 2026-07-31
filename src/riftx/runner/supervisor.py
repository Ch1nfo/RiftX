"""Persistent process supervision for local RiftX executions."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository
from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.domain.base import new_id
from riftx.executors import (
    DirectProcessExecutor,
    ProcessExecutionRequest,
    ProcessHandle,
    ProcessResult,
    ProcessStartError,
    ShellExecutionRequest,
    ShellExecutor,
    merge_environment,
)

from .models import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from .paths import RunnerPaths
from .process_inspector import ProcessInspector


@dataclass(slots=True)
class _ManagedExecution:
    handle: ProcessHandle
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False


class ProcessTerminationError(RuntimeError):
    """Raised when a detached process cannot be confirmed terminated."""


class ProcessSupervisor:
    def __init__(
        self,
        repository: ExecutionRepository,
        paths: RunnerPaths,
        *,
        process_executor: DirectProcessExecutor | None = None,
        shell_executor: ShellExecutor | None = None,
        inspector: ProcessInspector | None = None,
        termination_grace_seconds: float = 2.0,
        on_completed: Callable[[Execution], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._paths = paths
        self._process_executor = process_executor or DirectProcessExecutor()
        self._shell_executor = shell_executor or ShellExecutor(self._process_executor)
        self._inspector = inspector or ProcessInspector()
        self._termination_grace_seconds = termination_grace_seconds
        self._on_completed = on_completed
        self._managed: dict[str, _ManagedExecution] = {}

    async def start(self, request: ExecutionLaunchRequest) -> Execution:
        execution_id = request.execution_id or new_id()
        self._paths.ensure_run_layout(request.run_id)
        output_paths = self._paths.execution(request.run_id, execution_id)
        execution = Execution(
            id=execution_id,
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
            platform_system=platform.system().lower() or os.name,
            platform_release=platform.release(),
            platform_architecture=platform.machine() or "unknown",
            stdout_path=str(output_paths.stdout),
            stderr_path=str(output_paths.stderr),
            status=(
                ExecutionStatus.QUEUED
                if request.session_id is not None
                else ExecutionStatus.CREATED
            ),
        )

        execution, created = await self._repository.create_if_absent(execution)
        if not created:
            return execution

        effective_environment = merge_environment(request.env, mode=request.environment_mode)
        execution.executable_path = _resolve_executable(
            request.argv[0] if request.argv else request.shell_path,
            effective_environment,
        )

        execution.transition_to(ExecutionStatus.STARTING)
        await self._repository.save(execution)
        try:
            handle = await self._start_handle(request, execution, effective_environment)
        except ProcessStartError:
            execution.transition_to(ExecutionStatus.FAILED)
            await self._repository.save(execution)
            return execution

        execution.argv = handle.request.argv
        execution.executable_path = _resolve_executable(
            handle.request.argv[0], effective_environment
        )
        execution.pid = handle.pid
        execution.process_group_id = handle.process_group_id
        execution.process_created_at = handle.started_at
        execution.transition_to(ExecutionStatus.RUNNING, at=handle.started_at)
        await self._repository.save(execution)

        managed = _ManagedExecution(handle=handle)
        self._managed[execution.id] = managed
        task = asyncio.create_task(
            self._monitor(execution.id, managed),
            name=f"riftx-execution-{execution.id}",
        )
        managed.task = task
        return execution

    async def get(self, execution_id: str) -> Execution:
        execution = await self._repository.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def wait(self, execution_id: str) -> Execution:
        managed = self._managed.get(execution_id)
        if managed is not None:
            await asyncio.shield(_managed_task(managed))
        return await self.get(execution_id)

    async def cancel(self, execution_id: str) -> Execution:
        managed = self._managed.get(execution_id)
        if managed is not None:
            if not _managed_task(managed).done():
                managed.cancel_requested = True
                await managed.handle.cancel(
                    termination_grace_seconds=self._termination_grace_seconds
                )
                await asyncio.shield(_managed_task(managed))
                return await self.get(execution_id)

        execution = await self.get(execution_id)
        if execution.status in {ExecutionStatus.CREATED, ExecutionStatus.QUEUED}:
            execution.transition_to(ExecutionStatus.CANCELLED)
            return await self._repository.save(execution)
        if execution.status not in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.LOST,
        }:
            return execution
        if await self._matches_detached_process(execution, phase="before termination"):
            process_group_id = execution.process_group_id or execution.pid
            try:
                await _terminate_detached_process(
                    process_group_id,
                    grace_seconds=self._termination_grace_seconds,
                )
            except Exception as exc:
                raise ProcessTerminationError(
                    f"Failed to terminate detached execution {execution.id!r} "
                    f"using process group {process_group_id!r}"
                ) from exc
            await self._confirm_detached_process_stopped(execution)
        # Either the matching process group was terminated and verified above,
        # or the inspector proved that no matching process exists. Both are
        # affirmative stop confirmations, including reconciliation from LOST.
        execution.transition_to(ExecutionStatus.CANCELLED)
        await self._repository.save(execution)
        return execution

    async def _matches_detached_process(self, execution: Execution, *, phase: str) -> bool:
        try:
            return await self._inspector.matches(execution)
        except Exception as exc:
            raise ProcessTerminationError(
                f"Could not verify detached execution {execution.id!r} {phase}"
            ) from exc

    async def _confirm_detached_process_stopped(self, execution: Execution) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._termination_grace_seconds, 0.5)
        while await self._matches_detached_process(execution, phase="after termination"):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ProcessTerminationError(
                    f"Detached execution {execution.id!r} still matches process "
                    f"{execution.pid!r} after termination"
                )
            await asyncio.sleep(min(0.05, remaining))

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        execution = await self.get(execution_id)
        stdout, stderr = await asyncio.gather(
            asyncio.to_thread(
                _read_output_slice,
                Path(execution.stdout_path),
                stdout_cursor,
                max_bytes,
            ),
            asyncio.to_thread(
                _read_output_slice,
                Path(execution.stderr_path),
                stderr_cursor,
                max_bytes,
            ),
        )
        return ExecutionOutput(stdout=stdout, stderr=stderr)

    async def recover(self) -> list[Execution]:
        recovered: list[Execution] = []
        for execution in await self._repository.list_active():
            if execution.executor_type is ExecutorType.PTY:
                continue
            if not await self._inspector.matches(execution):
                execution.transition_to(ExecutionStatus.LOST)
                await self._repository.save(execution)
            recovered.append(execution)
        return recovered

    async def reconcile(self, execution_id: str) -> Execution:
        """Refresh a recovered execution that is no longer a child of this process."""

        execution = await self.get(execution_id)
        if (
            execution.status in {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}
            and execution.id not in self._managed
            and not await self._inspector.matches(execution)
        ):
            execution.transition_to(ExecutionStatus.LOST)
            await self._repository.save(execution)
        return execution

    async def close(self, *, cancel_running: bool = False) -> None:
        managed = list(self._managed.items())
        if cancel_running:
            await asyncio.gather(*(self.cancel(execution_id) for execution_id, _ in managed))
        else:
            for _, item in managed:
                _managed_task(item).cancel()
            await asyncio.gather(
                *(_managed_task(item) for _, item in managed),
                return_exceptions=True,
            )
            self._managed.clear()

    async def _start_handle(
        self,
        request: ExecutionLaunchRequest,
        execution: Execution,
        effective_environment: dict[str, str],
    ) -> ProcessHandle:
        if request.executor_type is ExecutorType.PROCESS:
            return await self._process_executor.start(
                ProcessExecutionRequest(
                    execution_key=request.execution_key,
                    argv=request.argv,
                    cwd=request.cwd,
                    env=effective_environment,
                    timeout_seconds=request.timeout_seconds,
                    stdout_path=Path(execution.stdout_path),
                    stderr_path=Path(execution.stderr_path),
                )
            )
        if request.shell is None:
            raise ValueError("shell request is missing shell kind")
        return await self._shell_executor.start(
            ShellExecutionRequest(
                execution_key=request.execution_key,
                script=request.command_text or "",
                shell=request.shell,
                shell_path=request.shell_path,
                cwd=request.cwd,
                env=effective_environment,
                timeout_seconds=request.timeout_seconds,
                stdout_path=Path(execution.stdout_path),
                stderr_path=Path(execution.stderr_path),
            )
        )

    async def _monitor(self, execution_id: str, managed: _ManagedExecution) -> None:
        try:
            result = await managed.handle.wait(
                termination_grace_seconds=self._termination_grace_seconds
            )
            await self._finalize(execution_id, result, managed.cancel_requested)
        finally:
            self._managed.pop(execution_id, None)

    async def _finalize(
        self,
        execution_id: str,
        result: ProcessResult,
        cancel_requested: bool,
    ) -> None:
        execution = await self.get(execution_id)
        if execution.status not in {ExecutionStatus.STARTING, ExecutionStatus.RUNNING}:
            return
        if cancel_requested:
            target = ExecutionStatus.CANCELLED
        elif execution.session_id is not None and result.timed_out:
            target = ExecutionStatus.HARD_TIMEOUT
        elif execution.session_id is not None and result.status is ExecutionStatus.EXITED:
            target = ExecutionStatus.COMPLETED
        else:
            target = result.status
        execution.transition_to(target, exit_code=result.exit_code)
        await self._repository.save(execution)
        if self._on_completed is not None:
            await self._on_completed(execution)


def _managed_task(managed: _ManagedExecution) -> asyncio.Task[None]:
    if managed.task is None:
        raise RuntimeError("managed execution monitor has not been initialized")
    return managed.task


def _resolve_executable(
    executable: str | Path | None,
    environment: dict[str, str],
) -> str | None:
    if executable is None:
        return None
    path = Path(executable)
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return shutil.which(str(executable), path=environment.get("PATH"))


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("output cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"output cursor {cursor} is beyond file size {size}")
    with path.open("rb") as stream:
        stream.seek(cursor)
        data = stream.read(max_bytes)
    next_cursor = cursor + len(data)
    return OutputSlice(
        data=data,
        cursor=cursor,
        next_cursor=next_cursor,
        eof=next_cursor >= size,
    )


async def _terminate_detached_process(
    process_group_id: int | None, *, grace_seconds: float
) -> None:
    if process_group_id is None:
        return
    if os.name == "posix":
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        await asyncio.sleep(grace_seconds)
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        os.kill(process_group_id, signal.SIGTERM)
