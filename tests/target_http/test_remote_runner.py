from __future__ import annotations

import asyncio

import pytest

from riftx.domain import RunnerCommand, RunnerCommandKind, RunnerCommandStatus, Scope
from riftx.runner.target_http import NodeTargetHttpRouter, RemoteTargetHttpClient
from riftx.target_http.errors import (
    TargetHttpRunnerExecutionCancelledError,
    TargetHttpRunnerExecutionUncertainError,
)
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpRequest,
    TargetHttpResult,
    TargetHttpRunnerRequest,
)


class CompletedTargetControl:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []
        self.completed: RunnerCommand | None = None

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        raw_launch = payload["launch"]
        assert isinstance(raw_launch, dict)
        raw_request = raw_launch["request"]
        assert isinstance(raw_request, dict)
        request = TargetHttpRequest.from_runner_payload(raw_request)
        result = TargetHttpResult(
            execution_key=request.execution_key,
            request_hash=request.fingerprint,
            status_code=200,
            final_url=request.url,
            elapsed_ms=3,
            content_length=len(self.body),
        )
        self.completed = RunnerCommand(
            id="command-1",
            node_id=node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
            status=RunnerCommandStatus.COMPLETED,
            result={"result": result.model_dump(mode="json")},
        )
        return self.completed, True

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand:
        assert command_id == "command-1"
        assert timeout_seconds == 35
        assert poll_interval_seconds == 0.1
        assert self.completed is not None
        return self.completed

    async def read_command_output(self, command_id: str) -> bytes:
        assert command_id == "command-1"
        return self.body


class StopTargetControl:
    def __init__(self, *, acknowledge: bool = True) -> None:
        self.acknowledge = acknowledge
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []
        self.commands: dict[str, RunnerCommand] = {}

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        intent_id = str(payload["tool_call_ids"][0])  # type: ignore[index]
        command = RunnerCommand(
            id=f"stop-{intent_id}",
            node_id=node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
            status=RunnerCommandStatus.COMPLETED,
            result={
                "outcomes": [
                    {
                        "tool_call_id": intent_id,
                        "confirmed": True,
                        "reason": "durable tombstone acknowledged",
                    }
                ]
            },
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
        assert timeout_seconds == 0.01
        assert poll_interval_seconds == 0.1
        if not self.acknowledge:
            raise TimeoutError("Runner is offline")
        return self.commands[command_id]

    async def read_command_output(self, command_id: str) -> bytes:
        return b""


class InterruptedTargetControl:
    def __init__(self, *, cancel_acknowledged: bool, block_execute_wait: bool = False) -> None:
        self.cancel_acknowledged = cancel_acknowledged
        self.block_execute_wait = block_execute_wait
        self.execute_wait_started = asyncio.Event()
        self.enqueued: list[tuple[str, RunnerCommandKind, str, dict[str, object]]] = []
        self.commands: dict[str, RunnerCommand] = {}

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]:
        self.enqueued.append((node_id, kind, idempotency_key, payload))
        if kind is RunnerCommandKind.TARGET_HTTP:
            command = RunnerCommand(
                id="execute-command",
                node_id=node_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
                status=RunnerCommandStatus.LEASED,
            )
        else:
            intent_id = str(payload["tool_call_ids"][0])  # type: ignore[index]
            command = RunnerCommand(
                id="cancel-command",
                node_id=node_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
                status=RunnerCommandStatus.COMPLETED,
                result={
                    "outcomes": [
                        {
                            "tool_call_id": intent_id,
                            "confirmed": self.cancel_acknowledged,
                            "reason": (
                                "target_http_local_task_terminated"
                                if self.cancel_acknowledged
                                else "target_http_local_task_not_registered"
                            ),
                        }
                    ]
                },
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
        assert poll_interval_seconds == 0.1
        if command_id == "execute-command":
            assert timeout_seconds == 35
            self.execute_wait_started.set()
            if self.block_execute_wait:
                await asyncio.Event().wait()
            raise TimeoutError("Runner command wait timed out")
        assert timeout_seconds == 0.01
        if not self.cancel_acknowledged:
            raise TimeoutError("Runner cancellation acknowledgement timed out")
        return self.commands[command_id]

    async def read_command_output(self, command_id: str) -> bytes:
        return b""


class FailedTargetControl(InterruptedTargetControl):
    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]:
        command, created = await super().enqueue(
            node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if kind is RunnerCommandKind.TARGET_HTTP:
            command = command.model_copy(
                update={
                    "status": RunnerCommandStatus.FAILED,
                    "error": "delivery claim replay suppressed after a possible send",
                }
            )
            self.commands[command.id] = command
        return command, created

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand:
        if command_id == "execute-command":
            assert timeout_seconds == 35
            assert poll_interval_seconds == 0.1
            return self.commands[command_id]
        return await super().wait_command(
            command_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


class RecordingRunner:
    def __init__(self, name: str) -> None:
        self.name = name
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
                status_code=204,
                final_url=launch.request.url,
                elapsed_ms=0,
            ),
            response_body=self.name.encode(),
        )

    async def stop_run(self, run_id, *, node_id, tool_call_ids):
        raise NotImplementedError


def _launch(node_id: str = "runner-a") -> TargetHttpRunnerRequest:
    return TargetHttpRunnerRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id=node_id,
        scope=Scope(domains=["target.internal"]),
        request=TargetHttpRequest(
            execution_key="target-key",
            method="POST",
            url="https://target.internal/api",
            body=b"binary-request",
            timeout_seconds=5,
        ),
    )


async def test_remote_target_http_dispatches_request_and_reassembles_response() -> None:
    control = CompletedTargetControl(b"remote-response")
    client = RemoteTargetHttpClient(control)

    exchange = await client.execute(_launch())

    node_id, kind, key, payload = control.enqueued[0]
    assert (node_id, kind, key) == (
        "runner-a",
        RunnerCommandKind.TARGET_HTTP,
        "target-http:target-key",
    )
    raw_launch = payload["launch"]
    assert isinstance(raw_launch, dict)
    raw_request = raw_launch["request"]
    assert isinstance(raw_request, dict)
    assert TargetHttpRequest.from_runner_payload(raw_request) == _launch().request
    assert exchange.response_body == b"remote-response"
    assert exchange.result.request_hash == _launch().request.fingerprint


async def test_remote_stop_requires_and_returns_per_intent_runner_ack() -> None:
    control = StopTargetControl()
    client = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)

    outcomes = await client.stop_run(
        "run-1",
        node_id="runner-a",
        tool_call_ids=["tool-call-1", "tool-call-2"],
    )

    assert [item.tool_call_id for item in outcomes] == ["tool-call-1", "tool-call-2"]
    assert all(item.confirmed is True for item in outcomes)
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
    ]
    assert [item[3]["tool_call_ids"] for item in control.enqueued] == [
        ["tool-call-1"],
        ["tool-call-2"],
    ]


async def test_remote_stop_stays_unconfirmed_when_runner_is_offline() -> None:
    control = StopTargetControl(acknowledge=False)
    client = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)

    outcomes = await client.stop_run(
        "run-1",
        node_id="runner-a",
        tool_call_ids=["tool-call-1"],
    )

    assert outcomes[0].confirmed is False
    assert "Runner is offline" in (outcomes[0].reason or "")
    assert control.enqueued[0][1] is RunnerCommandKind.TARGET_HTTP_CANCEL


@pytest.mark.parametrize("cancel_acknowledged", [True, False])
async def test_remote_wait_timeout_enqueues_durable_cancel_and_exposes_ack(
    cancel_acknowledged: bool,
) -> None:
    control = InterruptedTargetControl(cancel_acknowledged=cancel_acknowledged)
    client = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)

    with pytest.raises(TargetHttpRunnerExecutionUncertainError) as caught:
        await client.execute(_launch())

    assert caught.value.stop_outcome.confirmed is cancel_acknowledged
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.TARGET_HTTP,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
    ]


@pytest.mark.parametrize("cancel_acknowledged", [True, False])
async def test_remote_failed_delivery_claim_requires_separate_stop_ack(
    cancel_acknowledged: bool,
) -> None:
    control = FailedTargetControl(cancel_acknowledged=cancel_acknowledged)
    client = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)

    with pytest.raises(TargetHttpRunnerExecutionUncertainError) as caught:
        await client.execute(_launch())

    assert "delivery claim replay suppressed" in str(caught.value)
    assert caught.value.stop_outcome.confirmed is cancel_acknowledged
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.TARGET_HTTP,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
    ]


async def test_remote_control_coroutine_cancellation_waits_for_runner_stop_ack() -> None:
    control = InterruptedTargetControl(
        cancel_acknowledged=True,
        block_execute_wait=True,
    )
    client = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)
    execution = asyncio.create_task(client.execute(_launch()))
    await asyncio.wait_for(control.execute_wait_started.wait(), timeout=1)

    execution.cancel()
    with pytest.raises(TargetHttpRunnerExecutionCancelledError) as caught:
        await execution

    assert caught.value.stop_outcome.confirmed is True
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.TARGET_HTTP,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
    ]


async def test_remote_effect_guard_blocks_before_durable_runner_dispatch() -> None:
    control = CompletedTargetControl(b"unused")
    client = RemoteTargetHttpClient(control)

    async def blocked() -> None:
        raise RuntimeError("run is pausing")

    with pytest.raises(RuntimeError, match="run is pausing"):
        await client.execute(_launch(), effect_guard=blocked)

    assert control.enqueued == []


async def test_node_router_keeps_local_and_remote_host_networks_separate() -> None:
    local = RecordingRunner("local")
    remote = RecordingRunner("remote")
    router = NodeTargetHttpRouter(
        local_node_id="local",
        local=local,
        remote=remote,
    )

    local_exchange = await router.execute(_launch("local"))
    remote_exchange = await router.execute(_launch("runner-a"))

    assert local_exchange.response_body == b"local"
    assert remote_exchange.response_body == b"remote"
    assert [item.node_id for item in local.launches] == ["local"]
    assert [item.node_id for item in remote.launches] == ["runner-a"]
