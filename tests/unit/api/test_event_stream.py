from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import riftx.api.dependencies as api_dependencies
from riftx.api.dependencies import RunReadAuthorizationSnapshot, RunReadAuthorizer
from riftx.api.routes import events as event_routes
from riftx.application.errors import (
    AuthenticationError,
    ResourceNotAccessibleError,
    resource_not_accessible,
)
from riftx.domain import (
    LocalPrincipal,
    Objective,
    OperatorCapability,
    Run,
    RunEvent,
    RunKind,
)


def _principal(identity: str = "operator-1") -> LocalPrincipal:
    return LocalPrincipal(
        id=identity,
        namespace_id="local",
        capabilities=frozenset({OperatorCapability.READ}),
    )


def _run(
    *,
    run_id: str = "run-stream",
    kind: RunKind = RunKind.GENERAL,
    engagement_id: str = "engagement-stream",
    node_id: str = "node-stream",
) -> Run:
    return Run(
        id=run_id,
        engagement_id=engagement_id,
        node_id=node_id,
        objective=Objective(description="Stream durable events"),
        kind=kind,
        workspace_path="/tmp/riftx-event-stream",
    )


def _snapshot(run: Run, *, principal: LocalPrincipal | None = None) -> RunReadAuthorizationSnapshot:
    return RunReadAuthorizationSnapshot(
        run_id=run.id,
        run_kind=run.kind,
        engagement_id=run.engagement_id,
        node_id=run.node_id,
        principal=principal or _principal(),
    )


class _Request:
    def __init__(self) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                control_plane=SimpleNamespace(
                    settings=SimpleNamespace(
                        sse_poll_interval_seconds=0,
                        sse_heartbeat_seconds=3600,
                    )
                )
            )
        )
        self.disconnect_checks = 0

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return False


class _EventService:
    def __init__(self, batches: Sequence[Sequence[RunEvent]]) -> None:
        self._batches = [list(batch) for batch in batches]
        self.calls: list[tuple[str, int, int, bool]] = []

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        require_run: bool,
    ) -> list[RunEvent]:
        self.calls.append((run_id, after_sequence, limit, require_run))
        if not self._batches:
            raise AssertionError("Event list ran after stream authorization was denied")
        return self._batches.pop(0)


class _BatchAuthorizer:
    def __init__(self, second_error: Exception | None = None) -> None:
        self.second_error = second_error
        self.calls = 0

    async def revalidate_stream_snapshot(
        self,
        _request: object,
        _frozen: RunReadAuthorizationSnapshot,
    ) -> None:
        self.calls += 1
        if self.calls == 2 and self.second_error is not None:
            raise self.second_error


@pytest.mark.parametrize(
    "second_error",
    [
        pytest.param(resource_not_accessible(), id="owner-drift"),
        pytest.param(
            AuthenticationError(
                "local_operator_authentication_failed",
                "Local operator authentication failed",
            ),
            id="auth-denied",
        ),
    ],
)
async def test_stream_reauthorizes_before_every_batch_and_denial_reads_or_emits_nothing(
    second_error: Exception,
) -> None:
    run = _run()
    service = _EventService(
        [
            [
                RunEvent(
                    id="event-1",
                    run_id=run.id,
                    sequence=1,
                    event_type="run.created",
                )
            ]
        ]
    )
    authorizer = _BatchAuthorizer(second_error)
    iterator = event_routes.stream_events(
        run.id,
        _Request(),  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        event_routes._EventStreamAdmission(  # noqa: SLF001
            cursor=0,
            authorization=_snapshot(run),
        ),
        authorizer,  # type: ignore[arg-type]
        follow=True,
    )

    first = await anext(iterator)
    assert first.id == "1"
    assert first.event == "run.created"

    with pytest.raises(type(second_error)):
        await anext(iterator)

    assert authorizer.calls == 2
    assert service.calls == [(run.id, 0, 1000, False)]
    await iterator.aclose()


async def test_stream_revalidates_the_whole_batch_before_first_output() -> None:
    run = _run()
    service = _EventService(
        [
            [
                RunEvent(
                    id="event-valid",
                    run_id=run.id,
                    sequence=1,
                    event_type="run.created",
                ),
                RunEvent(
                    id="event-foreign",
                    run_id="run-foreign",
                    sequence=2,
                    event_type="run.updated",
                ),
            ]
        ]
    )
    authorizer = _BatchAuthorizer()
    iterator = event_routes.stream_events(
        run.id,
        _Request(),  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        event_routes._EventStreamAdmission(  # noqa: SLF001
            cursor=0,
            authorization=_snapshot(run),
        ),
        authorizer,  # type: ignore[arg-type]
        follow=False,
    )

    with pytest.raises(ResourceNotAccessibleError):
        await anext(iterator)

    assert authorizer.calls == 1
    assert service.calls == [(run.id, 0, 1000, False)]
    await iterator.aclose()


class _RunService:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.resolve_calls = 0
        self.get_calls = 0

    async def resolve_kind(self, run_id: str) -> RunKind:
        assert run_id == self.run.id
        self.resolve_calls += 1
        return self.run.kind

    async def get_run(self, run_id: str) -> Run:
        assert run_id == self.run.id
        self.get_calls += 1
        return self.run.model_copy(deep=True)


async def test_stream_snapshot_reauthenticates_before_any_owner_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_service = _RunService(_run())
    authorizer = RunReadAuthorizer(
        run_service=run_service,  # type: ignore[arg-type]
        principal=_principal(),
    )
    frozen = await authorizer.require_stream_snapshot(run_service.run.id)
    monkeypatch.setattr(
        api_dependencies,
        "authorize_local_operator",
        AsyncMock(
            side_effect=AuthenticationError(
                "local_operator_authentication_failed",
                "Local operator authentication failed",
            )
        ),
    )

    with pytest.raises(AuthenticationError):
        await authorizer.revalidate_stream_snapshot(object(), frozen)  # type: ignore[arg-type]

    assert run_service.resolve_calls == 1
    assert run_service.get_calls == 1


async def test_stream_snapshot_rejects_a_different_authenticated_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_service = _RunService(_run())
    authorizer = RunReadAuthorizer(
        run_service=run_service,  # type: ignore[arg-type]
        principal=_principal(),
    )
    frozen = await authorizer.require_stream_snapshot(run_service.run.id)
    monkeypatch.setattr(
        api_dependencies,
        "authorize_local_operator",
        AsyncMock(return_value=_principal("operator-replacement")),
    )

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await authorizer.revalidate_stream_snapshot(object(), frozen)  # type: ignore[arg-type]

    assert captured.value.code == "resource_not_accessible"
    assert run_service.resolve_calls == 2
    assert run_service.get_calls == 2


async def test_code_audit_stream_snapshot_is_retired_and_fail_closed() -> None:
    run = _run(kind=RunKind.CODE_AUDIT)
    run_service = _RunService(run)
    authorizer = RunReadAuthorizer(
        run_service=run_service,  # type: ignore[arg-type]
        principal=_principal(),
    )

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await authorizer.require_stream_snapshot(run.id)

    assert captured.value.code == "resource_not_accessible"
    assert run_service.get_calls == 0


async def test_stream_admission_preserves_last_event_id_cursor() -> None:
    run = _run()
    authorizer = SimpleNamespace(
        require_stream_snapshot=AsyncMock(return_value=_snapshot(run))
    )

    admission = await event_routes._prepare_stream_cursor(  # noqa: SLF001
        run,
        authorizer,  # type: ignore[arg-type]
        after_sequence=2,
        last_event_id="7",
    )

    assert admission.cursor == 7
    assert admission.authorization.run_id == run.id
