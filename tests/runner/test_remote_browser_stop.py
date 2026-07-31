from __future__ import annotations

from riftx.browser import BrowserRuntimeResult, BrowserSessionCommand
from riftx.domain import (
    BrowserMode,
    BrowserSession,
    BrowserSessionStatus,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
)
from riftx.runner.browser import RemoteBrowserClient


class _CompletedBrowserCloseControl:
    def __init__(self) -> None:
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
        raw_command = payload["command"]
        assert isinstance(raw_command, dict)
        close = BrowserSessionCommand.model_validate(raw_command)
        assert close.session is not None
        session = close.session.model_copy(deep=True)
        session.transition_to(BrowserSessionStatus.CLOSED)
        command = RunnerCommand(
            id=f"close-{len(self.enqueued)}",
            node_id=node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
            status=RunnerCommandStatus.COMPLETED,
            result={"result": BrowserRuntimeResult(session=session).model_dump(mode="json")},
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
        assert timeout_seconds == 330
        assert poll_interval_seconds == 0.1
        return self.commands[command_id]

    async def read_command_output(self, command_id: str) -> bytes:
        return b""


async def test_remote_browser_close_uses_priority_command_and_retryable_delivery() -> None:
    control = _CompletedBrowserCloseControl()
    client = RemoteBrowserClient(node_id="runner-a", control=control)
    session = BrowserSession(
        id="browser-1",
        run_id="run-1",
        agent_session_id="agent-session-1",
        node_id="runner-a",
        mode=BrowserMode.MANAGED_EPHEMERAL,
    )
    session.transition_to(BrowserSessionStatus.STARTING)
    session.transition_to(BrowserSessionStatus.ACTIVE)
    command = BrowserSessionCommand(session_id=session.id, session=session)

    first = await client.close(command)
    second = await client.close(command)

    assert first.result.session.status is BrowserSessionStatus.CLOSED
    assert second.result.session.status is BrowserSessionStatus.CLOSED
    assert [item[1] for item in control.enqueued] == [
        RunnerCommandKind.BROWSER_CLOSE,
        RunnerCommandKind.BROWSER_CLOSE,
    ]
    assert control.enqueued[0][2] != control.enqueued[1][2]
    assert all(item[2].startswith("browser:browser-1:close:") for item in control.enqueued)
