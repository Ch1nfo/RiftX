from pathlib import Path

from riftx.domain import Artifact, Engagement, Objective, Report, ReportFormat, Run
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunRepository,
)


async def test_report_repository_persists_and_filters_across_restart(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Reports")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Generate report"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await SQLAlchemyArtifactRepository(database.session_factory).create(
        Artifact(
            id="artifact-1",
            run_id="run-1",
            name="report.md",
            path=str(tmp_path / "report.md"),
            mime_type="text/markdown",
            sha256="a" * 64,
            size=10,
        )
    )
    repository = SQLAlchemyReportRepository(database.session_factory)
    report = Report(
        id="report-1",
        run_id="run-1",
        format=ReportFormat.MARKDOWN,
        artifact_id="artifact-1",
        finding_ids=["finding-1"],
    )
    await repository.create(report)
    await database.dispose()

    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = SQLAlchemyReportRepository(reopened.session_factory)

    assert await restarted.get("report-1") == report
    assert await restarted.list("run-1", format=ReportFormat.MARKDOWN) == [report]
    assert await restarted.list("run-1", format=ReportFormat.HTML) == []
    await reopened.dispose()
