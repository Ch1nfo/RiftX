from __future__ import annotations

from riftx.domain import RunnerCommand, RunnerCommandKind, RunnerCommandStatus, Scope
from riftx.runner.target_http import NodeTargetHttpRouter, RemoteTargetHttpClient
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


class RecordingRunner:
    def __init__(self, name: str) -> None:
        self.name = name
        self.launches: list[TargetHttpRunnerRequest] = []

    async def execute(self, launch: TargetHttpRunnerRequest) -> TargetHttpExchange:
        self.launches.append(launch)
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
