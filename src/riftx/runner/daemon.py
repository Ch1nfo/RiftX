"""Standalone outbound RiftX Runner daemon."""

from __future__ import annotations

import asyncio
import logging
import platform as platform_module
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import typer

from riftx import __version__
from riftx.application.ports import ExecutionRepository
from riftx.application.services import NodeRegistration
from riftx.domain import Execution, ExecutionStatus, RunnerCommandKind

from .control_client import (
    LeasedRunnerCommand,
    OutputOffsetMismatch,
    RunnerControlClient,
    RunnerControlClientError,
    RunnerCredentialStore,
)
from .models import ExecutionLaunchRequest
from .paths import RunnerPaths
from .protocols import ExecutionRunner
from .state import FileExecutionRepository
from .supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)
app = typer.Typer(help="Run an outbound RiftX execution node.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Manage the standalone outbound Runner daemon."""


_TERMINAL_STATUSES = {
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.LOST,
}


class TerminalCommandHandler(Protocol):
    async def handle(self, kind: RunnerCommandKind, payload: dict[str, object]) -> object: ...


@dataclass(frozen=True, slots=True)
class RunnerDaemonConfig:
    server_url: str
    node_id: str
    name: str
    state_path: Path
    registration_token: str | None = None
    platform: str = platform_module.system().lower()
    architecture: str = platform_module.machine().lower()
    runner_version: str = __version__
    capabilities: tuple[str, ...] = ("process", "shell")
    labels: dict[str, str] | None = None
    poll_wait_seconds: float = 30.0
    reconnect_initial_seconds: float = 0.25
    reconnect_max_seconds: float = 10.0
    output_poll_seconds: float = 0.1

    @property
    def registration(self) -> NodeRegistration:
        return NodeRegistration(
            node_id=self.node_id,
            name=self.name,
            platform=self.platform,
            architecture=self.architecture,
            runner_version=self.runner_version,
            capabilities=self.capabilities,
            labels=self.labels,
        )


class RunnerDaemon:
    """Long-polls commands and bridges them to a local ProcessSupervisor."""

    def __init__(
        self,
        *,
        config: RunnerDaemonConfig,
        client: RunnerControlClient,
        supervisor: ExecutionRunner,
        executions: ExecutionRepository,
        terminal_handler: TerminalCommandHandler | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._supervisor = supervisor
        self._executions = executions
        self._terminal_handler = terminal_handler
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def run_forever(self) -> None:
        delay = self.config.reconnect_initial_seconds
        while not self._closed:
            try:
                await self._client.connect(self.config.registration)
                await self.resume_active()
                delay = self.config.reconnect_initial_seconds
                command = await self._client.poll(wait_seconds=self.config.poll_wait_seconds)
                if command is not None:
                    await self.handle_command(command)
            except asyncio.CancelledError:
                raise
            except RunnerControlClientError as exc:
                if exc.status_code == 401:
                    self._client.invalidate_credentials()
                logger.warning("Runner control connection failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)
            except Exception:
                logger.exception("Runner command processing failed")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)

    async def handle_command(self, command: LeasedRunnerCommand) -> None:
        try:
            if command.kind is RunnerCommandKind.EXECUTE:
                result = await self._handle_execute(command.payload)
            elif command.kind is RunnerCommandKind.CANCEL:
                result = await self._handle_cancel(command.payload)
            elif command.kind in {
                RunnerCommandKind.TERMINAL_START,
                RunnerCommandKind.TERMINAL_WRITE,
                RunnerCommandKind.TERMINAL_RESIZE,
                RunnerCommandKind.TERMINAL_INTERRUPT,
                RunnerCommandKind.TERMINAL_CLOSE,
            }:
                if self._terminal_handler is None:
                    raise RuntimeError(f"Runner does not support command {command.kind.value!r}")
                terminal_result = await self._terminal_handler.handle(command.kind, command.payload)
                result = {"result": terminal_result}
            else:
                raise RuntimeError(f"Unsupported Runner command {command.kind.value!r}")
        except Exception as exc:
            logger.exception("Runner command %s failed", command.id)
            await self._client.finish(command, succeeded=False, error=str(exc))
            return
        await self._client.finish(command, succeeded=True, result=result)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self._supervisor, "close", None)
        if close is not None:
            await close()
        await self._client.close()

    async def _handle_execute(self, payload: dict[str, object]) -> dict[str, object]:
        server_execution_id = _required_string(payload, "execution_id")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("execute command is missing request")
        request = ExecutionLaunchRequest.model_validate(raw_request)
        if request.execution_id not in {None, server_execution_id}:
            raise ValueError("execute command execution IDs do not match")
        request = request.model_copy(update={"execution_id": server_execution_id})
        if request.node_id != self.config.node_id:
            raise ValueError("execute command targets a different Runner node")
        execution = await self._supervisor.start(request)
        if execution.status in _TERMINAL_STATUSES:
            await self._client.report_status(
                server_execution_id,
                ExecutionStatus.RUNNING,
                pid=execution.pid,
                process_group_id=execution.process_group_id,
            )
        await self._report_execution(server_execution_id, execution)
        self._start_monitor(server_execution_id, execution.id)
        return {
            "execution_id": server_execution_id,
            "local_execution_id": execution.id,
            "status": execution.status.value,
        }

    async def _handle_cancel(self, payload: dict[str, object]) -> dict[str, object]:
        server_execution_id = _required_string(payload, "execution_id")
        execution_key = _required_string(payload, "execution_key")
        local = await self._executions.get_by_key(execution_key)
        if local is None:
            await self._client.report_status(server_execution_id, ExecutionStatus.LOST)
            return {"execution_id": server_execution_id, "status": "lost"}
        execution = await self._supervisor.cancel(local.id)
        await self._report_execution(server_execution_id, execution)
        return {"execution_id": server_execution_id, "status": execution.status.value}

    def _start_monitor(self, server_execution_id: str, local_execution_id: str) -> None:
        current = self._monitors.get(server_execution_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._monitor_execution(server_execution_id, local_execution_id),
            name=f"riftx-remote-monitor-{server_execution_id}",
        )
        self._monitors[server_execution_id] = task
        task.add_done_callback(lambda _: self._monitors.pop(server_execution_id, None))

    async def _monitor_execution(
        self,
        server_execution_id: str,
        local_execution_id: str,
    ) -> None:
        cursors = {"stdout": 0, "stderr": 0}
        while not self._closed:
            try:
                cursors = await self._forward_output(
                    server_execution_id,
                    local_execution_id,
                    cursors,
                )
                execution = await self._refresh_local_execution(local_execution_id)
                if execution.status in _TERMINAL_STATUSES:
                    cursors = await self._forward_output(
                        server_execution_id,
                        local_execution_id,
                        cursors,
                    )
                    await self._report_with_retry(server_execution_id, execution)
                    return
            except RunnerControlClientError:
                logger.warning(
                    "Output forwarding failed for execution %s; retrying",
                    server_execution_id,
                )
            await asyncio.sleep(self.config.output_poll_seconds)

    async def resume_active(self) -> None:
        for execution in await self._executions.list_active():
            self._start_monitor(execution.id, execution.id)

    async def _refresh_local_execution(self, execution_id: str) -> Execution:
        reconcile = getattr(self._supervisor, "reconcile", None)
        if reconcile is not None:
            return await reconcile(execution_id)
        return await self._supervisor.get(execution_id)

    async def _forward_output(
        self,
        server_execution_id: str,
        local_execution_id: str,
        cursors: dict[str, int],
    ) -> dict[str, int]:
        output = await self._supervisor.read_output(
            local_execution_id,
            stdout_cursor=cursors["stdout"],
            stderr_cursor=cursors["stderr"],
            max_bytes=256 * 1024,
        )
        for stream, item in (("stdout", output.stdout), ("stderr", output.stderr)):
            if not item.data:
                continue
            try:
                cursors[stream] = await self._client.report_output(
                    server_execution_id,
                    stream=stream,
                    offset=item.cursor,
                    data=item.data,
                )
            except OutputOffsetMismatch as exc:
                cursors[stream] = exc.expected_offset
        return cursors

    async def _report_with_retry(
        self,
        server_execution_id: str,
        execution: Execution,
    ) -> None:
        while not self._closed:
            try:
                await self._report_execution(server_execution_id, execution)
                return
            except RunnerControlClientError:
                await asyncio.sleep(self.config.reconnect_initial_seconds)

    async def _report_execution(
        self,
        server_execution_id: str,
        execution: Execution,
    ) -> None:
        await self._client.report_status(
            server_execution_id,
            execution.status,
            pid=execution.pid if execution.status is ExecutionStatus.RUNNING else None,
            process_group_id=(
                execution.process_group_id if execution.status is ExecutionStatus.RUNNING else None
            ),
            exit_code=(execution.exit_code if execution.status in _TERMINAL_STATUSES else None),
        )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Runner command is missing {key}")
    return value


async def _run(config: RunnerDaemonConfig) -> None:
    config.state_path.mkdir(parents=True, exist_ok=True)
    executions = FileExecutionRepository(config.state_path / "executions.json")
    supervisor = ProcessSupervisor(executions, RunnerPaths(config.state_path))
    client = RunnerControlClient(
        server_url=config.server_url,
        node_id=config.node_id,
        credentials=RunnerCredentialStore(config.state_path / "credentials.json"),
        registration_token=config.registration_token,
    )
    daemon = RunnerDaemon(
        config=config,
        client=client,
        supervisor=supervisor,
        executions=executions,
    )
    try:
        await supervisor.recover()
        await daemon.run_forever()
    finally:
        await daemon.close()


@app.command()
def serve(
    server_url: Annotated[
        str, typer.Option(envvar="RIFTX_SERVER_URL", help="RiftX Control Plane URL.")
    ] = "http://127.0.0.1:8787",
    node_id: Annotated[
        str, typer.Option(envvar="RIFTX_NODE_ID", help="Stable Runner node ID.")
    ] = platform_module.node() or "runner",
    name: Annotated[
        str, typer.Option(envvar="RIFTX_NODE_NAME", help="Display name.")
    ] = platform_module.node() or "RiftX Runner",
    state_path: Annotated[
        Path, typer.Option(envvar="RIFTX_RUNNER_STATE", help="Runner state directory.")
    ] = Path(".riftx/remote-runner"),
    registration_token: Annotated[
        str | None,
        typer.Option(
            envvar="RIFTX_RUNNER_REGISTRATION_TOKEN",
            help="Bootstrap registration token.",
        ),
    ] = None,
) -> None:
    """Connect to a Control Plane and execute commands on this host."""

    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        _run(
            RunnerDaemonConfig(
                server_url=server_url,
                node_id=node_id,
                name=name,
                state_path=state_path,
                registration_token=registration_token,
            )
        )
    )


if __name__ == "__main__":
    app()
