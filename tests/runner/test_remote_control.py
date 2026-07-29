from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from riftx.domain import (
    ExecutionStatus,
    ExecutorType,
    Node,
    NodeStatus,
    RunnerCommandKind,
)
from riftx.runner.control_client import LeasedRunnerCommand
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig
from riftx.runner.models import ExecutionLaunchRequest
from riftx.runner.paths import RunnerPaths
from riftx.runner.remote import RemoteExecutionSupervisor
from riftx.runner.state import FileExecutionRepository
from riftx.runner.supervisor import ProcessSupervisor


class AlwaysMatchesInspector:
    async def matches(self, _: object) -> bool:
        return True


class FakeControlService:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[object, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        return object(), True


class FakeNodeService:
    async def get(self, node_id: str) -> Node:
        return Node(
            id=node_id,
            name=node_id,
            platform="linux",
            architecture="x86_64",
            status=NodeStatus.ONLINE,
        )


class FakeRunnerClient:
    def __init__(self) -> None:
        self.finished: list[tuple[str, bool, dict[str, object], str]] = []
        self.statuses: dict[str, list[ExecutionStatus]] = {}
        self.output: dict[tuple[str, str], bytearray] = {}

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        self.finished.append((command.id, succeeded, result or {}, error))

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **_: object,
    ) -> None:
        self.statuses.setdefault(execution_id, []).append(status)

    async def report_output(
        self,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        target = self.output.setdefault((execution_id, stream), bytearray())
        assert len(target) == offset
        target.extend(data)
        return len(target)

    async def close(self) -> None:
        return None


class FakeTerminalHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[RunnerCommandKind, dict[str, object]]] = []

    async def handle(self, kind: RunnerCommandKind, payload: dict[str, object]) -> object:
        self.calls.append((kind, payload))
        return {"resized": True}


@pytest.mark.asyncio
async def test_file_execution_repository_is_durable_and_idempotent(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    request = _request(tmp_path, key="durable-key")
    supervisor = ProcessSupervisor(repository, RunnerPaths(tmp_path / "runner"))

    first = await supervisor.start(request)
    duplicate = await supervisor.start(request)
    assert duplicate.id == first.id

    completed = await supervisor.wait(first.id)
    assert completed.status is ExecutionStatus.EXITED
    assert completed.exit_code == 0

    reopened = FileExecutionRepository(tmp_path / "executions.json")
    restored = await reopened.get_by_key("durable-key")
    assert restored is not None
    assert restored.id == first.id
    assert restored.status is ExecutionStatus.EXITED
    await supervisor.close()


@pytest.mark.asyncio
async def test_remote_supervisor_dispatches_idempotently_and_cancels(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "central-executions.json")
    control = FakeControlService()
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "central-runner"),
        control,  # type: ignore[arg-type]
        FakeNodeService(),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )
    request = _request(tmp_path, key="remote-key", node_id="runner-a")

    execution = await remote.start(request)
    duplicate = await remote.start(request)
    assert duplicate.id == execution.id
    assert len(control.enqueued) == 1
    node_id, kind, idempotency_key, payload = control.enqueued[0]
    assert (node_id, kind, idempotency_key) == (
        "runner-a",
        RunnerCommandKind.EXECUTE,
        "execute:remote-key",
    )
    assert payload["execution_id"] == execution.id
    assert payload["request"]["execution_id"] == execution.id  # type: ignore[index]

    await remote.cancel(execution.id)
    assert control.enqueued[-1][1] is RunnerCommandKind.CANCEL
    assert control.enqueued[-1][3]["execution_key"] == "remote-key"


@pytest.mark.asyncio
async def test_runner_daemon_executes_once_streams_output_and_handles_cancel(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "runner-executions.json")
    supervisor = ProcessSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    client = FakeRunnerClient()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            output_poll_seconds=0.001,
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,
        executions=repository,
    )

    execute = _command(
        "execute-1",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": "server-execution-1",
            "request": _request(
                tmp_path,
                key="daemon-key",
                node_id="runner-a",
                script="print('remote hello')",
            ).model_dump(mode="json"),
        },
    )
    await daemon.handle_command(execute)
    await _wait_for_status(client, "server-execution-1", ExecutionStatus.EXITED)
    assert client.finished[0][1] is True
    assert bytes(client.output[("server-execution-1", "stdout")]) == b"remote hello\n"

    await daemon.handle_command(_command("execute-duplicate", execute.kind, execute.payload))
    local = await repository.get_by_key("daemon-key")
    assert local is not None
    assert local.status is ExecutionStatus.EXITED

    long_running = _command(
        "execute-2",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": "server-execution-2",
            "request": _request(
                tmp_path,
                key="cancel-key",
                node_id="runner-a",
                script="import time; print('started', flush=True); time.sleep(30)",
            ).model_dump(mode="json"),
        },
    )
    await daemon.handle_command(long_running)
    await _wait_for_status(client, "server-execution-2", ExecutionStatus.RUNNING)
    await daemon.handle_command(
        _command(
            "cancel-2",
            RunnerCommandKind.CANCEL,
            {
                "execution_id": "server-execution-2",
                "execution_key": "cancel-key",
            },
        )
    )
    await _wait_for_status(client, "server-execution-2", ExecutionStatus.CANCELLED)
    cancelled = await repository.get_by_key("cancel-key")
    assert cancelled is not None
    assert cancelled.status is ExecutionStatus.CANCELLED
    await daemon.close()


def _request(
    tmp_path: Path,
    *,
    key: str,
    node_id: str = "local",
    script: str = "print('ok')",
) -> ExecutionLaunchRequest:
    return ExecutionLaunchRequest(
        execution_key=key,
        run_id="run-1",
        node_id=node_id,
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", script],
    )


def _command(
    command_id: str,
    kind: RunnerCommandKind,
    payload: dict[str, object],
) -> LeasedRunnerCommand:
    return LeasedRunnerCommand(
        id=command_id,
        kind=kind,
        payload=payload,
        lease_id=f"lease-{command_id}",
        attempts=1,
    )


async def _wait_for_status(
    client: FakeRunnerClient,
    execution_id: str,
    status: ExecutionStatus,
) -> None:
    for _ in range(1000):
        if status in client.statuses.get(execution_id, []):
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"execution {execution_id} did not report {status.value}")


@pytest.mark.asyncio
async def test_control_client_registers_persists_and_encodes_output(tmp_path: Path) -> None:
    import httpx

    from riftx.application.services import NodeRegistration
    from riftx.runner.control_client import RunnerControlClient, RunnerCredentialStore

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/nodes/register":
            return httpx.Response(
                200,
                json={"node": {"id": "runner-a"}, "created": True, "runner_token": "scoped"},
            )
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"id": "runner-a"})
        if request.url.path.endswith("/output"):
            assert request.read().decode().find('"data":"aGk="') >= 0
            return httpx.Response(200, json={"next_offset": 2})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    http = httpx.AsyncClient(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    store = RunnerCredentialStore(tmp_path / "credentials.json")
    client = RunnerControlClient(
        server_url="http://control.test",
        node_id="runner-a",
        credentials=store,
        registration_token="bootstrap",
        client=http,
    )
    await client.connect(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )
    assert store.load("runner-a") == "scoped"
    assert requests[0].headers["Authorization"] == "Bearer bootstrap"

    next_offset = await client.report_output("execution-1", stream="stdout", offset=0, data=b"hi")
    assert next_offset == 2
    assert requests[-1].headers["Authorization"] == "Bearer scoped"
    assert requests[-1].headers["X-RiftX-Node-ID"] == "runner-a"
    await http.aclose()


@pytest.mark.asyncio
async def test_runner_daemon_forwards_terminal_resize_commands(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    supervisor = ProcessSupervisor(repository, RunnerPaths(tmp_path / "runner"))
    client = FakeRunnerClient()
    terminal = FakeTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,
        executions=repository,
        terminal_handler=terminal,
    )
    command = _command(
        "resize-1",
        RunnerCommandKind.TERMINAL_RESIZE,
        {"session_id": "terminal-1", "cols": 160, "rows": 50},
    )
    await daemon.handle_command(command)
    assert terminal.calls == [(RunnerCommandKind.TERMINAL_RESIZE, command.payload)]
    assert client.finished[-1][1] is True
    await daemon.close()


@pytest.mark.asyncio
async def test_runner_daemon_reconnects_to_durable_active_execution(tmp_path: Path) -> None:
    state_file = tmp_path / "executions.json"
    repository = FileExecutionRepository(state_file)
    first_supervisor = ProcessSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    first_client = FakeRunnerClient()
    config = RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="runner-a",
        name="Runner A",
        state_path=tmp_path / "runner",
        output_poll_seconds=0.001,
    )
    first_daemon = RunnerDaemon(
        config=config,
        client=first_client,  # type: ignore[arg-type]
        supervisor=first_supervisor,
        executions=repository,
    )
    command = _command(
        "execute-reconnect",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": "server-reconnect",
            "request": _request(
                tmp_path,
                key="reconnect-key",
                node_id="runner-a",
                script="import time; print('alive', flush=True); time.sleep(30)",
            ).model_dump(mode="json"),
        },
    )
    await first_daemon.handle_command(command)
    await _wait_for_status(first_client, "server-reconnect", ExecutionStatus.RUNNING)
    local = await repository.get_by_key("reconnect-key")
    assert local is not None
    assert local.id == "server-reconnect"
    await first_daemon.close()

    reopened_repository = FileExecutionRepository(state_file)
    reopened_supervisor = ProcessSupervisor(
        reopened_repository,
        RunnerPaths(tmp_path / "runner"),
        inspector=AlwaysMatchesInspector(),  # type: ignore[arg-type]
        termination_grace_seconds=0.01,
    )
    recovered = await reopened_supervisor.recover()
    assert recovered[0].status is ExecutionStatus.RUNNING
    second_client = FakeRunnerClient()
    second_daemon = RunnerDaemon(
        config=config,
        client=second_client,  # type: ignore[arg-type]
        supervisor=reopened_supervisor,
        executions=reopened_repository,
    )
    await second_daemon.resume_active()
    await second_daemon.handle_command(
        _command(
            "cancel-reconnect",
            RunnerCommandKind.CANCEL,
            {
                "execution_id": "server-reconnect",
                "execution_key": "reconnect-key",
            },
        )
    )
    await _wait_for_status(second_client, "server-reconnect", ExecutionStatus.CANCELLED)
    await second_daemon.close()
