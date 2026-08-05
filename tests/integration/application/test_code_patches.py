from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.services.artifacts import ArtifactApplicationService
from riftx.application.services.code_artifacts import ArtifactCodePublisher
from riftx.code import CodeWorkspaceService
from riftx.domain import ArtifactAccessClass, Engagement, Objective, Run, RunKind
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemySnapshotRepository,
)
from riftx.runner import RunnerPaths


async def test_patch_receipt_survives_restart_and_is_run_bound(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'code-patch.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    artifacts = SQLAlchemyArtifactRepository(database.session_factory)
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n")
    await engagements.create(Engagement(id="engagement-1", name="Code patch"))
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            kind=RunKind.GENERAL,
            objective=Objective(description="Patch code"),
            workspace_path=str(workspace),
        )
    )
    await runs.create(
        Run(
            id="run-2",
            engagement_id="engagement-1",
            node_id="local",
            kind=RunKind.GENERAL,
            objective=Objective(description="Other workspace"),
            workspace_path=str(other_workspace),
        )
    )
    paths = RunnerPaths(tmp_path / "runner")

    def build_service() -> CodeWorkspaceService:
        artifact_service = ArtifactApplicationService(
            run_repository=runs,
            execution_repository=SQLAlchemyExecutionRepository(database.session_factory),
            artifact_repository=artifacts,
            event_repository=events,
            paths=paths,
        )
        return CodeWorkspaceService(
            runs=runs,
            audits=SQLAlchemyAuditAggregateReadRepository(database.session_factory),
            snapshots=SQLAlchemySnapshotRepository(database.session_factory),
            snapshot_store=None,
            max_snapshot_file_bytes=5 * 1024 * 1024,
            artifacts=ArtifactCodePublisher(artifact_service),
        )

    try:
        applied = await build_service().apply_patch(
            "run-1",
            patch=(
                "*** Begin Patch\n"
                "*** Update File: app.py\n"
                "@@\n"
                "-value = 1\n"
                "+value = 2\n"
                "*** End Patch"
            ),
            expected_sha256=hashlib.sha256(b"value = 1\n").hexdigest(),
        )
        assert target.read_text() == "value = 2\n"
        receipt = await artifacts.get(applied.receipt_artifact_id)
        assert receipt is not None
        assert receipt.run_id == "run-1"
        assert receipt.access_class is ArtifactAccessClass.PUBLIC_EXPORT
        assert receipt.mime_type == "application/vnd.riftx.code-patch-receipt+json"

        with pytest.raises(ResourceNotAccessibleError):
            await build_service().revert_patch(
                "run-2",
                receipt_artifact_id=applied.receipt_artifact_id,
            )

        reverted = await build_service().revert_patch(
            "run-1",
            receipt_artifact_id=applied.receipt_artifact_id,
        )
        assert reverted.action == "reverted"
        assert target.read_text() == "value = 1\n"
    finally:
        await database.dispose()
