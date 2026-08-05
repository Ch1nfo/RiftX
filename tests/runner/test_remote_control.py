from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.run_safety import RunSafetyStopService
from riftx.domain import (
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Node,
    NodeStatus,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandStatus,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_payload_digest,
)
from riftx.runner.control_client import LeasedRunnerCommand
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig
from riftx.runner.models import ExecutionLaunchRequest
from riftx.runner.paths import RunnerPaths
from riftx.runner.remote import RemoteExecutionSupervisor
from riftx.runner.state import FileExecutionRepository
from riftx.runner.supervisor import ProcessSupervisor
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpRequest,
    TargetHttpResult,
    TargetHttpRunnerRequest,
)

_OWNER = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)


class FakeControlService:
    def __init__(
        self,
        *,
        cancel_ack_result: dict[str, object] | None = None,
        cancel_ack_succeeds: bool = True,
    ) -> None:
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []
        self.commands: dict[str, RunnerCommand] = {}
        self.waited: list[str] = []
        self.cancel_ack_result = cancel_ack_result
        self.cancel_ack_succeeds = cancel_ack_succeeds

    async def current_principal(self, node_id: str) -> RunnerPrincipal:
        assert node_id == "runner-a"
        return _OWNER

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        target: RunnerPrincipal | None = None,
        **_: object,
    ) -> tuple[RunnerCommand, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        command = RunnerCommand(
            id=f"command-{len(self.enqueued)}",
            node_id=node_id,
            target=target or _OWNER,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        self.commands[command.id] = command
        return command, True

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand:
        del timeout_seconds, poll_interval_seconds
        self.waited.append(command_id)
        command = self.commands[command_id]
        result = self.cancel_ack_result
        if result is None:
            result = {
                "execution_id": command.payload.get("execution_id"),
                "local_execution_id": command.payload.get("execution_id"),
                "execution_key": command.payload.get("execution_key"),
                "owner": command.target.model_dump(mode="json") if command.target else None,
                "status": ExecutionStatus.CANCELLED.value,
                "physical_stop_confirmed": True,
            }
        return command.model_copy(
            update={
                "status": (
                    RunnerCommandStatus.COMPLETED
                    if self.cancel_ack_succeeds
                    else RunnerCommandStatus.FAILED
                ),
                "result": result,
                "error": "simulated cancel failure" if not self.cancel_ack_succeeds else "",
            }
        )


class FakeNodeService:
    async def get(self, node_id: str) -> Node:
        return Node(
            id=node_id,
            name=node_id,
            platform="linux",
            architecture="x86_64",
            status=NodeStatus.ONLINE,
        )


class BlockingLostNodeService:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, node_id: str) -> Node:
        self.entered.set()
        await self.release.wait()
        return Node(
            id=node_id,
            name=node_id,
            platform="linux",
            architecture="x86_64",
            status=NodeStatus.LOST,
        )


class FakeRunnerClient:
    def __init__(self) -> None:
        self.finished: list[tuple[str, bool, dict[str, object], str]] = []
        self.statuses: dict[str, list[ExecutionStatus]] = {}
        self.output: dict[tuple[str, str], bytearray] = {}

    @property
    def principal(self) -> RunnerPrincipal:
        return _OWNER

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
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
        **_: object,
    ) -> None:
        _assert_callback_binding(
            runner_command_id=runner_command_id,
            runner_effect_binding_id=runner_effect_binding_id,
            runner_envelope_digest=runner_envelope_digest,
            runner_binding_digest=runner_binding_digest,
        )
        self.statuses.setdefault(execution_id, []).append(status)

    async def report_output(
        self,
        execution_id: str,
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        _assert_callback_binding(
            runner_command_id=runner_command_id,
            runner_effect_binding_id=runner_effect_binding_id,
            runner_envelope_digest=runner_envelope_digest,
            runner_binding_digest=runner_binding_digest,
        )
        target = self.output.setdefault((execution_id, stream), bytearray())
        assert len(target) == offset
        target.extend(data)
        return len(target)

    async def report_command_output(
        self,
        command: LeasedRunnerCommand,
        *,
        offset: int,
        data: bytes,
    ) -> int:
        target = self.output.setdefault((command.id, "command"), bytearray())
        assert len(target) == offset
        target.extend(data)
        return len(target)

    async def close(self) -> None:
        return None


class FakeTerminalHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[RunnerCommandKind, dict[str, object]]] = []

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        **_: object,
    ) -> object:
        self.calls.append((kind, payload))
        return {"resized": True}


class FakeTargetHttpHandler:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.launches: list[TargetHttpRunnerRequest] = []

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard=None,
    ) -> TargetHttpExchange:
        self.launches.append(launch)
        if effect_guard is not None:
            await effect_guard()
        return TargetHttpExchange(
            result=TargetHttpResult(
                execution_key=launch.request.execution_key,
                request_hash=launch.request.fingerprint,
                status_code=200,
                final_url=launch.request.url,
                elapsed_ms=1,
                content_length=len(self.body),
            ),
            response_body=self.body,
        )


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
    persisted = await repository.get(execution.id)
    assert duplicate.id == execution.id
    assert execution.created_at is not None
    assert persisted is not None and persisted.created_at == execution.created_at
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
async def test_remote_execution_preserves_explicit_id_and_rejects_legacy_rebinding(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "remote-explicit-executions.json")
    control = FakeControlService()
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "remote-explicit-runner"),
        control,  # type: ignore[arg-type]
        FakeNodeService(),  # type: ignore[arg-type]
    )
    explicit = _request(
        tmp_path,
        key="remote-explicit-key",
        node_id="runner-a",
    ).model_copy(update={"execution_id": "remote-explicit-execution"})

    started = await remote.start(explicit)
    duplicate = await remote.start(explicit)

    assert started.id == duplicate.id == "remote-explicit-execution"
    assert len(control.enqueued) == 1
    assert control.enqueued[0][3]["execution_id"] == "remote-explicit-execution"
    assert control.enqueued[0][3]["request"]["execution_id"] == (  # type: ignore[index]
        "remote-explicit-execution"
    )

    legacy_request = _request(
        tmp_path,
        key="remote-legacy-explicit-key",
        node_id="runner-a",
    )
    legacy = Execution(
        id="remote-legacy-original",
        execution_key=legacy_request.execution_key,
        launch_fingerprint=None,
        run_id=legacy_request.run_id,
        node_id=legacy_request.node_id,
        owner=_OWNER,
        executor_type=legacy_request.executor_type,
        argv=legacy_request.argv,
        cwd=str(legacy_request.cwd),
        env_diff=legacy_request.env,
        status=ExecutionStatus.STARTING,
        stdout_path=str(tmp_path / "remote-legacy.stdout"),
        stderr_path=str(tmp_path / "remote-legacy.stderr"),
    )
    assert (await repository.create_if_absent(legacy))[1] is True
    enqueued_before = len(control.enqueued)

    with pytest.raises(ApplicationConflictError) as captured:
        await remote.start(
            legacy_request.model_copy(update={"execution_id": "remote-legacy-foreign"})
        )

    assert captured.value.code == "execution_idempotency_conflict"
    assert len(control.enqueued) == enqueued_before


@pytest.mark.parametrize("operation", ["wait", "recover"])
async def test_late_remote_node_loss_does_not_overwrite_cancelled_execution(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = FileExecutionRepository(tmp_path / f"remote-{operation}-race.json")
    nodes = BlockingLostNodeService()
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / f"remote-{operation}-runner"),
        FakeControlService(),  # type: ignore[arg-type]
        nodes,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )
    execution = await remote.start(
        _request(tmp_path, key=f"remote-{operation}-key", node_id="runner-a")
    )

    if operation == "wait":
        pending = asyncio.create_task(remote.wait(execution.id))
    else:
        pending = asyncio.create_task(remote.recover())
    await nodes.entered.wait()

    current = await repository.get(execution.id)
    assert current is not None
    current.transition_to(ExecutionStatus.CANCELLED)
    current, saved = await repository.save_if_status(
        current,
        expected={ExecutionStatus.STARTING},
    )
    assert saved is True
    nodes.release.set()

    result = await pending
    reconciled = result[0] if operation == "recover" else result
    assert reconciled.status is ExecutionStatus.CANCELLED
    persisted = await repository.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.CANCELLED


@pytest.mark.asyncio
async def test_run_safety_reaudits_remote_terminal_execution_with_command_ack(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "terminal-reaudit-executions.json")
    control = FakeControlService()
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "terminal-reaudit-runner"),
        control,  # type: ignore[arg-type]
        FakeNodeService(),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )
    execution = await remote.start(
        _request(tmp_path, key="terminal-reaudit-key", node_id="runner-a")
    )
    execution.pid = 7123
    execution.process_group_id = 7123
    execution.transition_to(ExecutionStatus.CANCELLED)
    await repository.save(execution)
    safety = RunSafetyStopService(
        execution_repository=repository,
        execution_runner=remote,
        require_all_resource_stoppers=False,
    )

    result = await safety.stop_run("run-1")

    disposition = result.resources["executions"]
    assert disposition.succeeded is True
    assert disposition.attempted_ids == (execution.id,)
    assert disposition.confirmed_statuses == {execution.id: ExecutionStatus.CANCELLED.value}
    persisted = await repository.get(execution.id)
    assert persisted is not None
    assert persisted.physical_stop_confirmed_at is not None
    assert control.enqueued[-1][1] is RunnerCommandKind.CANCEL
    assert len(control.waited) == 1
    assert control.commands[control.waited[0]].kind is RunnerCommandKind.CANCEL


@pytest.mark.asyncio
async def test_run_safety_rejects_remote_terminal_ack_without_physical_stop(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "invalid-terminal-ack.json")
    control = FakeControlService(
        cancel_ack_result={
            "execution_id": "placeholder",
            "physical_stop_confirmed": False,
        }
    )
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "invalid-terminal-ack-runner"),
        control,  # type: ignore[arg-type]
        FakeNodeService(),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )
    execution = await remote.start(
        _request(tmp_path, key="invalid-terminal-ack-key", node_id="runner-a")
    )
    execution.pid = 7124
    execution.process_group_id = 7124
    execution.transition_to(ExecutionStatus.CANCELLED)
    await repository.save(execution)
    control.cancel_ack_result = {
        "execution_id": execution.id,
        "local_execution_id": execution.id,
        "execution_key": execution.execution_key,
        "owner": _OWNER.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": False,
    }
    safety = RunSafetyStopService(
        execution_repository=repository,
        execution_runner=remote,
        require_all_resource_stoppers=False,
        execution_cancel_max_passes=1,
    )

    result = await safety.stop_run("run-1", drain=False)

    disposition = result.resources["executions"]
    assert disposition.succeeded is False
    assert "did not confirm physical process stop" in disposition.failures[execution.id]


@pytest.mark.asyncio
async def test_remote_start_guard_blocks_before_dispatch_or_enqueues_cancel_after_dispatch(
    tmp_path: Path,
) -> None:
    repository = FileExecutionRepository(tmp_path / "guarded-central-executions.json")
    control = FakeControlService()
    remote = RemoteExecutionSupervisor(
        repository,
        RunnerPaths(tmp_path / "guarded-central-runner"),
        control,  # type: ignore[arg-type]
        FakeNodeService(),  # type: ignore[arg-type]
    )

    async def blocked_before_dispatch() -> None:
        raise ApplicationConflictError("run_execution_blocked", "Run is pausing")

    with pytest.raises(ApplicationConflictError):
        await remote.start(
            _request(tmp_path, key="blocked-before-dispatch", node_id="runner-a"),
            effect_guard=blocked_before_dispatch,
        )
    blocked = await repository.get_by_key("blocked-before-dispatch")
    assert blocked is not None and blocked.status is ExecutionStatus.CANCELLED
    assert control.enqueued == []

    guard_calls = 0

    async def blocked_after_dispatch() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise ApplicationConflictError("run_execution_blocked", "Run is cancelling")

    with pytest.raises(ApplicationConflictError):
        await remote.start(
            _request(tmp_path, key="blocked-after-dispatch", node_id="runner-a"),
            effect_guard=blocked_after_dispatch,
        )
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.EXECUTE,
        RunnerCommandKind.CANCEL,
    ]
    dispatched = await repository.get_by_key("blocked-after-dispatch")
    assert dispatched is not None and dispatched.status is ExecutionStatus.CANCELLED
    assert dispatched.physical_stop_confirmed_at is not None


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

    await daemon.handle_command(execute)
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
    cancel_finish = next(item for item in client.finished if item[0] == "cancel-2")
    assert cancel_finish[1] is True
    assert cancel_finish[2] == {
        "execution_id": "server-execution-2",
        "local_execution_id": cancelled.id,
        "execution_key": "cancel-key",
        "owner": _OWNER.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }

    suppressed_marker = tmp_path / "cancelled-before-start"
    await daemon.handle_command(
        _command(
            "cancel-before-start",
            RunnerCommandKind.CANCEL,
            {
                "execution_id": "server-execution-suppressed",
                "execution_key": "suppressed-key",
            },
        )
    )
    await daemon.handle_command(
        _command(
            "execute-after-cancel",
            RunnerCommandKind.EXECUTE,
            {
                "execution_id": "server-execution-suppressed",
                "request": _request(
                    tmp_path,
                    key="suppressed-key",
                    node_id="runner-a",
                    script=(
                        "from pathlib import Path; "
                        f"Path({str(suppressed_marker)!r}).write_text('unsafe')"
                    ),
                ).model_dump(mode="json"),
            },
        )
    )

    # The no-local CANCEL is deliberately unconfirmed.  The later EXECUTE is
    # suppressed by its tombstone without claiming that a split-brain owner
    # physically stopped its process.
    assert "server-execution-suppressed" not in client.statuses
    no_local_cancel = next(item for item in client.finished if item[0] == "cancel-before-start")
    assert no_local_cancel[1] is False
    assert "physical termination could not be confirmed" in no_local_cancel[3]
    delayed_execute = next(item for item in client.finished if item[0] == "execute-after-cancel")
    assert delayed_execute[0:2] == ("execute-after-cancel", True)
    assert delayed_execute[2] == {
        "execution_id": "server-execution-suppressed",
        "status": "suppressed",
        "suppressed_by_cancellation": True,
        "physical_stop_confirmed": False,
    }
    assert await repository.get_by_key("suppressed-key") is None
    assert not suppressed_marker.exists()
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
    *,
    owner: RunnerPrincipal = _OWNER,
) -> LeasedRunnerCommand:
    if kind in {RunnerCommandKind.EXECUTE, RunnerCommandKind.TERMINAL_START}:
        raw_request = payload.get("request")
        if isinstance(raw_request, dict):
            payload = {
                **payload,
                "request": {
                    **raw_request,
                    "runner_principal": owner.model_dump(mode="json"),
                },
            }
    if kind in {RunnerCommandKind.EXECUTE, RunnerCommandKind.CANCEL}:
        execution_id = payload.get("execution_id")
        assert isinstance(execution_id, str) and execution_id
        operation_family = (
            RunnerOperationFamily.EXECUTION
            if kind is RunnerCommandKind.EXECUTE
            else RunnerOperationFamily.SAFETY_STOP
        )
        output_contract = (
            RunnerOutputContract(
                max_output_bytes=100_000_000,
                allowed_streams=("stderr", "stdout"),
                result_schema="riftx.runner-result/execution-start/v1",
            )
            if kind is RunnerCommandKind.EXECUTE
            else RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1",
                stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            )
        )
        binding = RunnerEffectBinding(
            id=f"binding-{command_id}",
            run_id="run-1",
            run_kind=RunKind.GENERAL,
            node_id="runner-a",
            target=owner,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=operation_family,
            execution_id=execution_id,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution_id,
        )
    elif kind in {
        RunnerCommandKind.TERMINAL_START,
        RunnerCommandKind.TERMINAL_WRITE,
        RunnerCommandKind.TERMINAL_RESIZE,
        RunnerCommandKind.TERMINAL_INTERRUPT,
    }:
        session_id = payload.get("session_id")
        execution_id = payload.get("execution_id")
        assert isinstance(session_id, str) and session_id
        assert isinstance(execution_id, str) and execution_id
        operation_family = RunnerOperationFamily.TERMINAL
        output_contract = RunnerOutputContract(
            max_output_bytes=(
                100_000_000
                if kind is RunnerCommandKind.TERMINAL_START
                else 0
            ),
            allowed_streams=(
                ("stderr", "stdout")
                if kind is RunnerCommandKind.TERMINAL_START
                else ()
            ),
            result_schema=(
                "riftx.runner-result/terminal-start/v1"
                if kind is RunnerCommandKind.TERMINAL_START
                else "riftx.runner-result/terminal-operation/v1"
            ),
        )
        binding = RunnerEffectBinding(
            id=f"binding-{command_id}",
            run_id="run-1",
            run_kind=RunKind.GENERAL,
            node_id="runner-a",
            target=owner,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=operation_family,
            execution_id=execution_id,
            resource_kind=RunnerResourceKind.TERMINAL_SESSION,
            resource_id=session_id,
        )
    elif kind is RunnerCommandKind.TARGET_HTTP:
        launch = payload.get("launch")
        assert isinstance(launch, dict)
        resource_id = launch.get("tool_call_id")
        assert isinstance(resource_id, str) and resource_id
        operation_family = RunnerOperationFamily.TARGET_HTTP
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("command",),
            result_schema="riftx.runner-result/target-http/v1",
        )
        binding = RunnerEffectBinding(
            id=f"binding-{command_id}",
            run_id="run-1",
            run_kind=RunKind.GENERAL,
            node_id="runner-a",
            target=owner,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=operation_family,
            resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
            resource_id=resource_id,
        )
    elif kind is RunnerCommandKind.BROWSER:
        raw_command = payload.get("command")
        assert isinstance(raw_command, dict)
        resource_id = raw_command.get("session_id")
        assert isinstance(resource_id, str) and resource_id
        operation_family = RunnerOperationFamily.BROWSER
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("command",),
            result_schema="riftx.runner-result/browser/v1",
        )
        binding = RunnerEffectBinding(
            id=f"binding-{command_id}",
            run_id="run-1",
            run_kind=RunKind.GENERAL,
            node_id="runner-a",
            target=owner,
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=operation_family,
            resource_kind=RunnerResourceKind.BROWSER_SESSION,
            resource_id=resource_id,
        )
    else:
        raise AssertionError(f"Unsupported test command kind: {kind.value}")

    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=kind,
        operation_family=operation_family,
        payload_digest=runner_payload_digest(payload),
        output_contract=output_contract,
    )
    return LeasedRunnerCommand(
        id=command_id,
        kind=kind,
        payload=payload,
        lease_id=f"lease-{command_id}",
        attempts=1,
        target=owner,
        ownership=ownership,
        effect_binding_id=binding.id,
        binding_digest=binding.binding_digest,
        envelope_digest=ownership.envelope_digest,
        operation_family=operation_family,
        output_contract=output_contract,
    )


def _assert_callback_binding(
    *,
    runner_command_id: str,
    runner_effect_binding_id: str,
    runner_envelope_digest: str,
    runner_binding_digest: str,
) -> None:
    assert runner_command_id
    assert runner_effect_binding_id
    assert len(runner_envelope_digest) == 64
    assert len(runner_binding_digest) == 64


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
                json={
                    "node": {"id": "runner-a"},
                    "created": True,
                    "runner_token": "scoped",
                    "principal": {"instance_id": "instance-a", "epoch": 7},
                },
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
    stored = store.load("runner-a")
    assert stored is not None
    assert stored.token == "scoped"
    assert stored.principal.instance_id == "instance-a"
    assert stored.principal.epoch == 7
    assert requests[0].headers["Authorization"] == "Bearer bootstrap"

    callback_command = _command(
        "output-command-1",
        RunnerCommandKind.EXECUTE,
        {"execution_id": "execution-1"},
        owner=stored.principal,
    )
    assert callback_command.ownership is not None
    next_offset = await client.report_output(
        "execution-1",
        runner_command_id=callback_command.id,
        runner_effect_binding_id=callback_command.effect_binding_id,
        runner_envelope_digest=callback_command.envelope_digest,
        runner_binding_digest=callback_command.binding_digest,
        stream="stdout",
        offset=0,
        data=b"hi",
    )
    assert next_offset == 2
    assert requests[-1].headers["Authorization"] == "Bearer scoped"
    assert requests[-1].headers["X-RiftX-Node-ID"] == "runner-a"
    assert requests[-1].headers["X-RiftX-Runner-Instance-ID"] == "instance-a"
    assert requests[-1].headers["X-RiftX-Runner-Epoch"] == "7"
    await http.aclose()


@pytest.mark.asyncio
async def test_runner_daemon_forwards_terminal_resize_commands(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    local_execution = Execution(
        id="terminal-execution-1",
        execution_key="terminal:terminal-1",
        run_id="run-1",
        node_id="runner-a",
        owner=_OWNER,
        executor_type=ExecutorType.PTY,
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "terminal.log"),
        stderr_path=str(tmp_path / "terminal.log"),
        status=ExecutionStatus.RUNNING,
    )
    await repository.create_if_absent(local_execution)
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
            {
                "session_id": "terminal-1",
                "execution_id": local_execution.id,
                "operation_id": "resize-1",
                "cols": 160,
                "rows": 50,
            },
    )
    await daemon.handle_command(command)
    assert terminal.calls == [(RunnerCommandKind.TERMINAL_RESIZE, command.payload)]
    assert client.finished[-1][1] is True
    await daemon.close()


@pytest.mark.asyncio
async def test_runner_daemon_executes_target_http_and_chunks_response(tmp_path: Path) -> None:
    repository = FileExecutionRepository(tmp_path / "executions.json")
    supervisor = ProcessSupervisor(repository, RunnerPaths(tmp_path / "runner"))
    client = FakeRunnerClient()
    response_body = b"x" * (256 * 1024 + 17)
    target_http = FakeTargetHttpHandler(response_body)
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
        target_http_handler=target_http,
    )
    request = TargetHttpRequest(
        execution_key="target-http-key",
        method="GET",
        url="https://target.internal/status",
    )
    command = _command(
        "target-http-1",
        RunnerCommandKind.TARGET_HTTP,
        {
            "launch": {
                "run_id": "run-1",
                "session_id": "session-1",
                "tool_call_id": "tool-call-1",
                "node_id": "runner-a",
                "scope": {"domains": ["target.internal"]},
                "request": request.runner_payload(),
            },
            "max_response_bytes": request.max_response_bytes,
        },
    )

    await daemon.handle_command(command)

    assert target_http.launches[0].request == request
    assert bytes(client.output[(command.id, "command")]) == response_body
    _, succeeded, result, error = client.finished[-1]
    assert succeeded is True
    assert error == ""
    assert result["result"]["request_hash"] == request.fingerprint  # type: ignore[index]
    await daemon.close()


@pytest.mark.asyncio
async def test_runner_daemon_close_cancels_durable_active_execution(tmp_path: Path) -> None:
    state_file = tmp_path / "executions.json"
    repository = FileExecutionRepository(state_file)
    supervisor = ProcessSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    client = FakeRunnerClient()
    config = RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="runner-a",
        name="Runner A",
        state_path=tmp_path / "runner",
        output_poll_seconds=0.001,
    )
    daemon = RunnerDaemon(
        config=config,
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,
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
    await daemon.handle_command(command)
    await _wait_for_status(client, "server-reconnect", ExecutionStatus.RUNNING)
    local = await repository.get_by_key("reconnect-key")
    assert local is not None
    assert local.id == "server-reconnect"
    await daemon.close()

    reopened_repository = FileExecutionRepository(state_file)
    stopped = await reopened_repository.get_by_key("reconnect-key")
    assert stopped is not None
    assert stopped.status is ExecutionStatus.CANCELLED
    reopened_supervisor = ProcessSupervisor(
        reopened_repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    recovered = await reopened_supervisor.recover()
    assert recovered == []
    await reopened_supervisor.close(cancel_running=True)


@pytest.mark.asyncio
async def test_runner_daemon_recovers_execution_after_abrupt_restart(tmp_path: Path) -> None:
    state_file = tmp_path / "executions.json"
    repository = FileExecutionRepository(state_file)
    first_supervisor = ProcessSupervisor(
        repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    launch_command = _command(
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
    raw_request = launch_command.payload.get("request")
    assert isinstance(raw_request, dict)
    request = ExecutionLaunchRequest.model_validate(raw_request).model_copy(
        update={
            "execution_id": "server-reconnect",
            "runner_command_id": launch_command.id,
            "runner_effect_binding_id": launch_command.effect_binding_id,
            "runner_binding_digest": launch_command.binding_digest,
            "runner_envelope_digest": launch_command.envelope_digest,
        }
    )
    execution = await first_supervisor.start(request)
    assert execution.status is ExecutionStatus.RUNNING
    execution.owner = _OWNER
    await repository.save(execution)

    # Simulate an abrupt daemon restart: abandon local monitoring without running the
    # graceful RunnerDaemon.close() path, which intentionally cancels active work.
    await first_supervisor.close(cancel_running=False)

    reopened_repository = FileExecutionRepository(state_file)
    reopened_supervisor = ProcessSupervisor(
        reopened_repository,
        RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.01,
    )
    recovered = await reopened_supervisor.recover()
    assert len(recovered) == 1
    assert recovered[0].status is ExecutionStatus.RUNNING
    second_client = FakeRunnerClient()
    config = RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="runner-a",
        name="Runner A",
        state_path=tmp_path / "runner",
        output_poll_seconds=0.001,
    )
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


@pytest.mark.asyncio
async def test_runner_daemon_executes_browser_command_and_uploads_attachment(
    tmp_path: Path,
) -> None:
    from riftx.browser import BrowserAttachment, BrowserRuntimeExchange, BrowserRuntimeResult
    from riftx.domain import (
        BrowserMode,
        BrowserObservation,
        BrowserPage,
        BrowserSession,
        BrowserSessionStatus,
    )

    class FakeBrowserHandler:
        async def open(self, request):
            session = BrowserSession(
                id=request.session_id,
                run_id=request.run_id,
                agent_session_id=request.agent_session_id,
                node_id=request.node_id,
                mode=BrowserMode.MANAGED_EPHEMERAL,
                status=BrowserSessionStatus.ACTIVE,
                current_page_id="page-1",
                page_ids=["page-1"],
            )
            page = BrowserPage(
                id="page-1",
                browser_session_id=session.id,
                url=request.url,
                title="Example",
                last_observation_version=1,
            )
            observation = BrowserObservation(
                browser_session_id=session.id,
                page_id=page.id,
                url=request.url,
                title="Example",
                visible_text_excerpt="bounded",
                observation_version=1,
            )
            return BrowserRuntimeExchange(
                result=BrowserRuntimeResult(
                    session=session,
                    pages=[page],
                    observation=observation,
                    attachment=BrowserAttachment(
                        kind="screenshot",
                        name="screen.png",
                        mime_type="image/png",
                    ),
                ),
                attachment_content=b"png-bytes",
            )

    repository = FileExecutionRepository(tmp_path / "executions.json")
    supervisor = ProcessSupervisor(repository, RunnerPaths(tmp_path / "runner"))
    client = FakeRunnerClient()
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
        browser_handler=FakeBrowserHandler(),  # type: ignore[arg-type]
    )
    command = _command(
        "browser-open-1",
        RunnerCommandKind.BROWSER,
        {
            "operation": "open",
            "command": {
                "session_id": "browser-1",
                "run_id": "run-1",
                "agent_session_id": "agent-session-1",
                "node_id": "runner-a",
                "mode": "managed_ephemeral",
                "url": "https://example.com/",
                "scope": {"domains": ["example.com"]},
                "headless": True,
                "include_screenshot": True,
            },
            "max_attachment_bytes": 100_000_000,
        },
    )

    await daemon.handle_command(command)

    assert bytes(client.output[(command.id, "command")]) == b"png-bytes"
    _, succeeded, result, error = client.finished[-1]
    assert succeeded is True
    assert error == ""
    assert result["result"]["observation"]["observation_version"] == 1  # type: ignore[index]
    await daemon.close()
