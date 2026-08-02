from pathlib import Path

from riftx.application.services import ArtifactApplicationService
from riftx.application.services.artifacts import RegisterArtifactContent
from riftx.connectors import ConnectorSource, ConnectorSubmission
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyConnectorSubmissionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import RunnerPaths


async def test_connector_submission_survives_database_restart(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'connector.db'}"
    database = Database(database_url)
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Connector"))
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Analyze capture"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    artifact_service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
        artifact_repository=SQLAlchemyArtifactRepository(database.session_factory),
        event_repository=events,
        paths=RunnerPaths(tmp_path / "runner"),
    )
    artifacts = []
    for name in ("request.http", "response.http", "manifest.json"):
        artifacts.append(
            await artifact_service.register_content(
                "run-1",
                RegisterArtifactContent(
                    content=name.encode(), name=name, mime_type="application/octet-stream"
                ),
            )
        )
    repository = SQLAlchemyConnectorSubmissionRepository(database.session_factory)
    item = ConnectorSubmission(
        id="submission-1",
        run_id="run-1",
        capture_id="capture-1",
        source=ConnectorSource.BURP,
        fingerprint="a" * 64,
        request_artifact_id=artifacts[0].id,
        response_artifact_id=artifacts[1].id,
        manifest_artifact_id=artifacts[2].id,
        summary={"method": "GET", "url": "https://example.com/"},
    )
    await repository.create(item)
    await database.dispose()

    reopened = Database(database_url)
    restored = await SQLAlchemyConnectorSubmissionRepository(
        reopened.session_factory
    ).get(ConnectorSource.BURP, "capture-1")
    assert restored == item
    await reopened.dispose()
