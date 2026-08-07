import json
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from tests.integration.persistence._audit_compat import (
    NOW,
    _create_audit,
    _create_engagement,
    _create_project,
    _project,
)

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    Engagement,
    EntryPoint,
    EntryPointKind,
    Execution,
    ExecutorType,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    Scope,
)
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.persistence.mappers import artifact_to_record


async def _database_with_run(
    path: Path,
    *,
    kind: RunKind = RunKind.GENERAL,
) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Test")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind=kind,
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test"),
            entry_points=(
                [EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")]
                if kind is RunKind.PENTEST
                else []
            ),
            scope=Scope(domains=["example.test"] if kind is RunKind.PENTEST else []),
            pentest_admission=(
                PentestAdmission(
                    budget=PentestBudget(
                        max_duration_seconds=3600,
                        max_model_calls=100,
                        max_tokens=100_000,
                        max_tool_calls=200,
                        max_target_interactions=50,
                        max_concurrent_target_interactions=2,
                    )
                )
                if kind is RunKind.PENTEST
                else None
            ),
            workspace_path=str(path.parent),
        )
    )
    return database


def _artifact(artifact_id: str, *, execution_id: str | None = None) -> Artifact:
    return Artifact(
        id=artifact_id,
        run_id="run-1",
        execution_id=execution_id,
        name=f"{artifact_id}.txt",
        path=f"/tmp/{artifact_id}.txt",
        mime_type="text/plain",
        sha256=("a" if artifact_id == "artifact-1" else "b") * 64,
        size=4,
    )


async def test_artifact_repository_create_get_filter_and_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database = await _database_with_run(database_path)
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    first = _artifact("artifact-1")
    second = _artifact("artifact-2")

    await repository.create(first)
    await repository.create(second)
    await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{database_path}")
    repository = SQLAlchemyArtifactRepository(restarted.session_factory)
    assert await repository.get(first.id) == first
    assert list(await repository.list("run-1", limit=1, offset=1)) == [second]
    await restarted.dispose()


async def test_pentest_artifact_uses_the_interactive_owner_and_visibility_path(
    tmp_path: Path,
) -> None:
    database = await _database_with_run(
        tmp_path / "pentest-artifact.db",
        kind=RunKind.PENTEST,
    )
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    artifact = _artifact("artifact-pentest")

    assert await repository.create(artifact) == artifact
    assert await repository.get(artifact.id) == artifact
    assert list(await repository.list(artifact.run_id)) == [artifact]
    assert await repository.restricted_artifact_ids({artifact.id}) == frozenset()
    await database.dispose()


async def test_artifact_repository_enforces_run_and_id_constraints(tmp_path: Path) -> None:
    database = await _database_with_run(tmp_path / "riftx.db")
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    artifact = _artifact("artifact-1")
    await repository.create(artifact)

    with pytest.raises(RepositoryConflictError):
        await repository.create(artifact)
    with pytest.raises(RepositoryConflictError):
        await repository.create(artifact.model_copy(update={"id": "other", "run_id": "missing"}))
    await database.dispose()


async def test_artifact_repository_rejects_and_hides_cross_run_execution_owner(
    tmp_path: Path,
) -> None:
    database = await _database_with_run(tmp_path / "artifact-execution-owner.db")
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            kind="general",
            id="run-2",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Other Run"),
            workspace_path=str(tmp_path / "other"),
        )
    )
    execution = Execution(
        id="execution-other-run",
        execution_key="execution-key:other-run",
        run_id="run-2",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["probe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "other-stdout.log"),
        stderr_path=str(tmp_path / "other-stderr.log"),
    )
    await SQLAlchemyExecutionRepository(database.session_factory).create_if_absent(execution)
    artifact = _artifact("artifact-cross-run-execution", execution_id=execution.id).model_copy(
        update={
            "ingest_provenance": ArtifactIngestProvenance(
                method=ArtifactIngestMethod.LOCAL_NOFOLLOW_FD,
                producer_execution_id=execution.id,
            )
        }
    )
    repository = SQLAlchemyArtifactRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await repository.create(artifact)

    async with database.session_factory() as session, session.begin():
        session.add(artifact_to_record(artifact))

    assert await repository.get(artifact.id) is None
    assert list(await repository.list("run-1")) == []
    assert await repository.restricted_artifact_ids({artifact.id}) == frozenset({artifact.id})
    await database.dispose()


async def test_execution_delete_cannot_mutate_immutable_artifact_provenance(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-execution-restrict.db"
    database = await _database_with_run(database_path)
    execution = Execution(
        id="execution-artifact-owner",
        execution_key="execution-key:artifact-owner",
        run_id="run-1",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        argv=["probe"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
    )
    await SQLAlchemyExecutionRepository(database.session_factory).create_if_absent(execution)
    artifact = _artifact("artifact-execution", execution_id=execution.id).model_copy(
        update={
            "ingest_provenance": ArtifactIngestProvenance(
                method=ArtifactIngestMethod.LOCAL_NOFOLLOW_FD,
                producer_execution_id=execution.id,
            )
        }
    )
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    await repository.create(artifact)

    with pytest.raises(IntegrityError):
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM executions WHERE id = :execution_id"),
                {"execution_id": execution.id},
            )

    assert await repository.get(artifact.id) == artifact
    await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{database_path}")
    restarted_repository = SQLAlchemyArtifactRepository(restarted.session_factory)
    assert await restarted_repository.get(artifact.id) == artifact
    await restarted.dispose()


async def _database_with_audits(path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    project = _project()
    await _create_project(database, project)
    await _create_audit(database, audit_id="audit-1", run_id="audit-run-1", snapshot_id=None)
    await _create_audit(database, audit_id="audit-2", run_id="audit-run-2", snapshot_id=None)
    return database


def _audit_artifact(
    artifact_id: str,
    *,
    audit_id: str = "audit-1",
    run_id: str = "audit-run-1",
    access_class: ArtifactAccessClass = ArtifactAccessClass.RESTRICTED_SENSITIVE,
    name: str | None = None,
    description: str = "",
) -> Artifact:
    artifact_name = name or f"{artifact_id}.json"
    return Artifact(
        id=artifact_id,
        run_id=run_id,
        audit_id=audit_id,
        access_class=access_class,
        content_trust=ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT,
        name=artifact_name,
        path=f"/private/{artifact_id}.json",
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.CONTROL_PLANE_BYTES,
        ),
        mime_type="application/json",
        sha256=("c" if audit_id == "audit-1" else "d") * 64,
        size=4,
        description=description,
        created_at=NOW,
    )


async def test_artifact_repository_partitions_generic_and_exact_audit_reads_before_paging(
    tmp_path: Path,
) -> None:
    database = await _database_with_audits(tmp_path / "artifact-visibility.db")
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    hidden = _audit_artifact("artifact-hidden")
    first_public = _audit_artifact(
        "artifact-public-1",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
    )
    target_http = _audit_artifact(
        "artifact-target-http",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
        name="target-http-abc-response.bin",
        description="Immutable Target HTTP response body",
    )
    second_public = _audit_artifact(
        "artifact-public-2",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
    )
    mismatched_public = _audit_artifact(
        "artifact-public-owner-mismatch",
        audit_id="audit-1",
        run_id="audit-run-2",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
    )
    foreign = _audit_artifact(
        "artifact-foreign",
        audit_id="audit-2",
        run_id="audit-run-2",
    )
    auditless_public = Artifact(
        id="artifact-public-without-audit-owner",
        run_id="audit-run-1",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
        name="auditless-public.json",
        path="/private/auditless-public.json",
        mime_type="application/json",
        sha256="e" * 64,
        size=4,
        created_at=NOW,
    )
    for artifact in (
        hidden,
        first_public,
        target_http,
        second_public,
        foreign,
    ):
        await repository.create(artifact)
    with pytest.raises(RepositoryConflictError):
        await repository.create(mismatched_public)
    with pytest.raises(RepositoryConflictError):
        await repository.create(auditless_public)

    # Simulate rows written outside the Repository boundary. Generic reads must
    # still fail closed for both mismatched and omitted Audit ownership.
    async with database.session_factory() as session, session.begin():
        session.add(artifact_to_record(mismatched_public))
        session.add(artifact_to_record(auditless_public))

    assert await repository.get_run_id(hidden.id) is None
    assert await repository.get(hidden.id) is None
    assert await repository.get_for_reconciliation(hidden.id) == hidden
    assert await repository.get(first_public.id) == first_public
    assert await repository.get_run_id(mismatched_public.id) is None
    assert await repository.get(mismatched_public.id) is None
    assert await repository.get_run_id(auditless_public.id) is None
    assert await repository.get(auditless_public.id) is None
    assert list(await repository.list("audit-run-2")) == []
    assert list(await repository.list("audit-run-1", limit=1, offset=1)) == [second_public]
    assert await repository.target_http_sensitive_ids(
        {target_http.id, first_public.id, hidden.id}
    ) == frozenset({target_http.id})
    assert await repository.restricted_artifact_ids(
        {
            hidden.id,
            first_public.id,
            mismatched_public.id,
            auditless_public.id,
            "artifact-missing",
        }
    ) == frozenset(
        {
            hidden.id,
            mismatched_public.id,
            auditless_public.id,
            "artifact-missing",
        }
    )
    assert await repository.get(target_http.id) is None
    assert await repository.get_for_reconciliation(target_http.id) == target_http

    owner = await repository.resolve_owner(hidden.id)
    assert owner is not None
    assert owner.artifact_id == hidden.id
    assert owner.run_id == "audit-run-1"
    assert owner.audit_id == "audit-1"
    assert owner.access_class is ArtifactAccessClass.RESTRICTED_SENSITIVE
    assert owner.run_kind is RunKind.CODE_AUDIT
    assert owner.audit_run_id == "audit-run-1"

    assert await repository.get_for_audit(hidden.id, "audit-1", "audit-run-1") == hidden
    assert await repository.get_for_audit(hidden.id, "audit-2", "audit-run-1") is None
    assert await repository.get_for_audit(hidden.id, "audit-1", "audit-run-2") is None
    assert list(await repository.list_for_audit("audit-1", "audit-run-1")) == [
        hidden,
        first_public,
        second_public,
        target_http,
    ]
    assert list(await repository.list_for_audit("audit-1", "audit-run-2")) == []
    assert list(await repository.list_for_audit("audit-2", "audit-run-1")) == []
    await database.dispose()


async def test_artifact_owner_resolver_projects_only_bounded_owner_columns(
    tmp_path: Path,
) -> None:
    database = await _database_with_audits(tmp_path / "artifact-owner-projection.db")
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    artifact = _audit_artifact("artifact-owner")
    await repository.create(artifact)
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        assert await repository.resolve_owner(artifact.id) is not None
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture_statement)

    select_statement = next(statement for statement in statements if "from artifacts" in statement)
    projection = select_statement.split("from artifacts", 1)[0]
    assert "artifacts.id" in projection
    assert "artifacts.run_id" in projection
    assert "artifacts.audit_id" in projection
    assert "artifacts.access_class" in projection
    assert "runs.kind" in projection
    assert "audit_scans.run_id" in projection
    for forbidden in (
        "artifacts.name",
        "artifacts.path",
        "artifacts.storage_key",
        "artifacts.ingest_provenance_json",
        "artifacts.mime_type",
        "artifacts.sha256",
        "artifacts.size",
        "artifacts.description",
    ):
        assert forbidden not in projection
    await database.dispose()


async def test_artifact_repository_normalizes_corruption_after_bounded_resolution(
    tmp_path: Path,
) -> None:
    database = await _database_with_audits(tmp_path / "artifact-corruption.db")
    repository = SQLAlchemyArtifactRepository(database.session_factory)
    artifact = _audit_artifact(
        "artifact-corrupt",
        access_class=ArtifactAccessClass.PUBLIC_EXPORT,
    )
    await repository.create(artifact)
    corrupt_provenance = json.dumps(
        {
            "schema_version": "riftx.artifact-ingest-provenance/v1",
            "method": "corrupt-method-canary",
            "producer_node_id": None,
            "producer_execution_id": None,
        }
    )
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE artifacts SET ingest_provenance_json = :provenance WHERE id = :artifact_id"
            ),
            {"provenance": corrupt_provenance, "artifact_id": artifact.id},
        )

    # The bounded authorization lookup does not materialize corrupt provenance.
    assert await repository.resolve_owner(artifact.id) is not None
    with pytest.raises(RepositoryIntegrityError) as raised:
        await repository.get(artifact.id)
    assert raised.value.entity_id == artifact.id
    assert "canary" not in str(raised.value)
    with pytest.raises(RepositoryIntegrityError):
        await repository.get_for_audit(artifact.id, "audit-1", "audit-run-1")

    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE artifacts SET ingest_provenance_json = :provenance WHERE id = :artifact_id"
            ),
            {"provenance": "{invalid-json-canary", "artifact_id": artifact.id},
        )
    with pytest.raises(RepositoryIntegrityError) as invalid_json:
        await repository.get(artifact.id)
    assert "canary" not in str(invalid_json.value)
    await database.dispose()
