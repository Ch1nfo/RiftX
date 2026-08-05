from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    RunnerPrincipal,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runtime.types import AgentSession


def _logical_execution(
    tmp_path: Path,
    *,
    execution_id: str,
    execution_key: str,
    launch_fingerprint: str | None = "launch:v1:logical",
) -> Execution:
    return Execution(
        id=execution_id,
        execution_key=execution_key,
        launch_fingerprint=launch_fingerprint,
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        node_id="node-1",
        owner=RunnerPrincipal(instance_id="runner-1", epoch=1),
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        tool_id="printf",
        tool_version="9.0",
        cwd=str(tmp_path),
        env_diff={"LANG": "C", "REMOVED": None},
        stdout_path=str(tmp_path / f"{execution_id}.stdout"),
        stderr_path=str(tmp_path / f"{execution_id}.stderr"),
    )


async def test_execution_repository_claim_is_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(tmp_path),
        )
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    execution = Execution(
        id="execution-1",
        execution_key="stable-key",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["printf", "ok"],
        tool_id="printf",
        tool_version="coreutils 9",
        executable_path="/usr/bin/printf",
        cwd=str(tmp_path),
        platform_system="linux",
        platform_release="6.10",
        platform_architecture="x86_64",
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        process_created_at=datetime(2026, 7, 30, tzinfo=UTC),
    )

    first, first_created = await repository.create_if_absent(execution)
    duplicate = execution.model_copy(update={"id": "execution-2"})
    second, second_created = await repository.create_if_absent(duplicate)
    created_active = await repository.list_active()
    first.transition_to(ExecutionStatus.STARTING)
    await repository.save(first)
    active = await repository.list_active()
    listed = await repository.list("run-1")

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert [item.id for item in created_active] == [first.id]
    assert created_active[0].status is ExecutionStatus.CREATED
    assert [item.id for item in active] == [first.id]
    assert [item.id for item in listed] == [first.id]
    assert listed[0].tool_id == "printf"
    assert listed[0].tool_version == "coreutils 9"
    assert listed[0].executable_path == "/usr/bin/printf"
    assert listed[0].platform_system == "linux"
    assert listed[0].platform_architecture == "x86_64"
    assert listed[0].process_created_at == datetime(2026, 7, 30, tzinfo=UTC)
    await database.dispose()


async def test_execution_repository_rejects_creation_time_replacement(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'immutable-created-at.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Immutable execution order")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            workspace_path=str(tmp_path),
        )
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    created_at = datetime(2026, 8, 1, tzinfo=UTC)
    execution = Execution(
        execution_key="immutable-created-at",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        created_at=created_at,
    )
    await repository.create_if_absent(execution)
    replaced = execution.model_copy(update={"created_at": datetime(2026, 8, 2, tzinfo=UTC)})

    with pytest.raises(RepositoryConflictError, match="creation time.*immutable"):
        await repository.save(replaced)

    persisted = await repository.get(execution.id)
    assert persisted is not None and persisted.created_at == created_at
    await database.dispose()


async def test_sql_launch_fingerprint_roundtrip_and_idempotency_conflicts(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'launch-identity.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Launch identity")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test launch identity"),
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="test")
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    original = _logical_execution(
        tmp_path,
        execution_id="logical-execution",
        execution_key="logical-key",
    )

    created, was_created = await repository.create_if_absent(original)
    roundtrip = await repository.get(original.id)

    assert was_created is True
    assert created.launch_fingerprint == "launch:v1:logical"
    assert roundtrip is not None
    for field_name in (
        "execution_key",
        "launch_fingerprint",
        "run_id",
        "session_id",
        "tool_call_id",
        "attempt_group",
        "node_id",
        "owner",
        "executor_type",
        "argv",
        "command_text",
        "tool_id",
        "tool_version",
        "cwd",
        "env_diff",
    ):
        assert getattr(roundtrip, field_name) == getattr(original, field_name)

    mutations: tuple[tuple[str, object], ...] = (
        ("launch_fingerprint", "launch:v1:foreign"),
        ("run_id", "foreign-run"),
        ("session_id", None),
        ("tool_call_id", "foreign-tool-call"),
        ("attempt_group", "retry-1"),
        ("node_id", "foreign-node"),
        ("owner", RunnerPrincipal(instance_id="runner-2", epoch=1)),
        ("executor_type", ExecutorType.SHELL),
        ("argv", ["printf", "changed"]),
        ("command_text", "printf changed"),
        ("tool_id", "foreign-tool"),
        ("tool_version", "10.0"),
        ("cwd", str(tmp_path / "foreign-cwd")),
        ("env_diff", {"LANG": "foreign"}),
    )
    for index, (field_name, foreign_value) in enumerate(mutations):
        duplicate = original.model_copy(
            update={"id": f"duplicate-{index}", field_name: foreign_value}
        )
        with pytest.raises(RepositoryConflictError):
            await repository.create_if_absent(duplicate)
        rebound = original.model_copy(update={field_name: foreign_value})
        with pytest.raises(RepositoryConflictError):
            await repository.save(rebound)

    with pytest.raises(RepositoryConflictError):
        await repository.save(original.model_copy(update={"execution_key": "foreign-key"}))
    with pytest.raises(RepositoryConflictError):
        await repository.create_if_absent(
            original.model_copy(update={"execution_key": "foreign-key"})
        )
    durable = await repository.get(original.id)
    assert durable is not None
    assert durable.execution_key == original.execution_key
    assert durable.launch_fingerprint == original.launch_fingerprint
    assert durable.argv == original.argv
    assert durable.env_diff == original.env_diff
    await database.dispose()


async def test_sql_legacy_null_launch_fingerprint_replay_checks_stable_fields(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-launch.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Legacy launch")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Legacy replay"),
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="test")
    )
    repository = SQLAlchemyExecutionRepository(database.session_factory)
    legacy = _logical_execution(
        tmp_path,
        execution_id="legacy-execution",
        execution_key="legacy-key",
        launch_fingerprint=None,
    )
    assert (await repository.create_if_absent(legacy))[1] is True
    replay = legacy.model_copy(
        update={
            "id": "legacy-replay",
            "launch_fingerprint": "launch:v1:current-request",
        }
    )

    authoritative, created = await repository.create_if_absent(replay)

    assert created is False
    assert authoritative.id == legacy.id
    assert authoritative.launch_fingerprint is None
    with pytest.raises(RepositoryConflictError):
        await repository.create_if_absent(
            replay.model_copy(update={"id": "legacy-foreign", "node_id": "foreign-node"})
        )
    with pytest.raises(RepositoryConflictError):
        await repository.create_if_absent(
            replay.model_copy(update={"id": "legacy-empty-argv", "argv": []})
        )
    await database.dispose()
