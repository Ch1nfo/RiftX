from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import (
    Engagement,
    MessageRole,
    MessageType,
    MessageVisibility,
    Objective,
    Run,
    TranscriptMessageDraft,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.runtime.session import SessionManager
from riftx.runtime.types import ProviderState, SessionStatus


async def build_manager(tmp_path: Path) -> tuple[Database, SessionManager, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'session.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    for run_id in ("run-1", "run-2"):
        await runs.create(
            Run(
                id=run_id,
                engagement_id="engagement-1",
                node_id="node-1",
                objective=Objective(description=f"Objective for {run_id}"),
                workspace_path=str(tmp_path / run_id),
            )
        )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    providers = SQLAlchemyProviderStateRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    manager = SessionManager(
        run_repository=runs,
        session_repository=sessions,
        transcript_repository=transcript,
        provider_state_repository=providers,
    )
    return database, manager, {
        "runs": runs,
        "sessions": sessions,
        "providers": providers,
        "transcript": transcript,
    }


def draft(
    content: str,
    *,
    message_type: MessageType = MessageType.USER_MESSAGE,
    role: MessageRole = MessageRole.USER,
) -> TranscriptMessageDraft:
    return TranscriptMessageDraft(
        agent_id="primary",
        role=role,
        message_type=message_type,
        content=content,
        visibility=MessageVisibility.USER_VISIBLE,
    )


async def test_create_suspend_resume_and_recover_session(tmp_path: Path) -> None:
    database, manager, repos = await build_manager(tmp_path)
    created = await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    assert created.status is SessionStatus.ACTIVE
    first = await manager.append_message("session-1", draft("start"), expected_last_sequence=0)
    state = await repos["providers"].create(
        ProviderState(
            id="state-1",
            session_id="session-1",
            provider="fake",
            model="fake-model",
            engine_type="fake",
            engine_version="1",
            state={"cursor": 1},
        )
    )
    suspended = await manager.suspend_session("session-1", provider_state_id=state.id)
    assert suspended.status is SessionStatus.SUSPENDED

    loaded = await manager.load_session("session-1")
    assert loaded.session.id == "session-1"
    assert loaded.provider_state == state
    assert loaded.transcript == [first]

    resumed = await manager.resume_session("session-1")
    assert resumed.session.status is SessionStatus.ACTIVE
    await database.dispose()


async def test_parent_child_sessions_must_share_run(tmp_path: Path) -> None:
    database, manager, _ = await build_manager(tmp_path)
    parent = await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="parent"
    )
    child = await manager.create_session(
        run_id="run-1",
        model_profile="fake-model",
        parent_session_id=parent.id,
        agent_type="subagent",
        session_id="child",
    )
    assert child.parent_session_id == parent.id
    parent_message = await manager.append_message("parent", draft("parent message"))
    child_message = await manager.append_message("child", draft("child message"))
    assert parent_message.sequence == child_message.sequence == 1

    with pytest.raises(RepositoryConflictError, match="another run"):
        await manager.create_session(
            run_id="run-2",
            model_profile="fake-model",
            parent_session_id=parent.id,
            session_id="invalid-child",
        )
    await database.dispose()


async def test_transcript_sequence_and_all_required_message_types(tmp_path: Path) -> None:
    database, manager, _ = await build_manager(tmp_path)
    await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    required = list(MessageType)
    messages = []
    for sequence, message_type in enumerate(required, start=1):
        role = (
            MessageRole.USER
            if message_type is MessageType.USER_MESSAGE
            else MessageRole.ASSISTANT
        )
        messages.append(
            await manager.append_message(
                "session-1",
                draft(message_type.value, message_type=message_type, role=role),
                expected_last_sequence=sequence - 1,
            )
        )
    assert [message.sequence for message in messages] == list(range(1, len(required) + 1))
    assert [message.message_type for message in messages] == required
    assert (await manager.load_session("session-1")).transcript == messages
    await database.dispose()


async def test_concurrent_expected_sequence_has_one_stable_conflict(tmp_path: Path) -> None:
    database, manager, _ = await build_manager(tmp_path)
    await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    results = await asyncio.gather(
        manager.append_message("session-1", draft("one"), expected_last_sequence=0),
        manager.append_message("session-1", draft("two"), expected_last_sequence=0),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, RepositoryConflictError)]
    assert len(conflicts) == 1
    assert len((await manager.load_session("session-1")).transcript) == 1
    await database.dispose()


async def test_closed_session_rejects_new_transcript_writes(tmp_path: Path) -> None:
    database, manager, _ = await build_manager(tmp_path)
    await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    closed = await manager.close_session("session-1")
    assert closed.status is SessionStatus.COMPLETED
    with pytest.raises(RepositoryConflictError, match="closed"):
        await manager.append_message("session-1", draft("too late"))
    await database.dispose()


async def test_session_loads_when_referenced_provider_state_is_missing(tmp_path: Path) -> None:
    database, manager, repos = await build_manager(tmp_path)
    created = await manager.create_session(
        run_id="run-1", model_profile="fake-model", session_id="session-1"
    )
    created.provider_state_id = "missing-state"
    await repos["sessions"].save(created)

    loaded = await manager.load_session("session-1")
    assert loaded.session.provider_state_id == "missing-state"
    assert loaded.provider_state is None
    await database.dispose()
