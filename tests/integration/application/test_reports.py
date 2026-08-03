from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    ArtifactApplicationService,
    GenerateReports,
    RegisterArtifact,
    RegisterArtifactContent,
    ReportApplicationService,
)
from riftx.domain import (
    Artifact,
    Engagement,
    Finding,
    FindingEvidence,
    FindingSeverity,
    Objective,
    ReportFormat,
    Run,
    RunKind,
    RunStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import RunnerPaths


async def test_code_audit_generic_artifact_and_report_mutations_are_side_effect_free(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-report-fence.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    artifacts = SQLAlchemyArtifactRepository(database.session_factory)
    findings = SQLAlchemyFindingRepository(database.session_factory)
    reports = SQLAlchemyReportRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-audit", name="Audit report fence"))
    run = Run(
        kind=RunKind.CODE_AUDIT,
        id="audit-run",
        engagement_id="engagement-audit",
        node_id="local",
        objective=Objective(description="Reject generic Artifact and Report"),
        workspace_path=str(tmp_path / "audit-output"),
    )
    await runs.create(run)
    runner_root = tmp_path / "runner-state"
    artifact_service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=executions,
        artifact_repository=artifacts,
        event_repository=events,
        paths=RunnerPaths(runner_root),
    )
    report_service = ReportApplicationService(
        run_repository=runs,
        finding_repository=findings,
        artifact_repository=artifacts,
        report_repository=reports,
        event_repository=events,
        artifact_service=artifact_service,
    )
    baseline_events = await events.list_after(run.id)

    operations = (
        artifact_service.register(
            run.id,
            RegisterArtifact(source_path="/sensitive/host/path-must-not-be-opened"),
        ),
        artifact_service.register_content(
            run.id,
            RegisterArtifactContent(
                content=b"must not persist",
                name="forged.txt",
                mime_type="text/plain",
            ),
        ),
        report_service.generate(run.id, GenerateReports(formats=[ReportFormat.JSON])),
    )
    for operation in operations:
        with pytest.raises(ApplicationConflictError) as captured:
            await operation
        assert captured.value.code == "run_kind_operation_unsupported"

    assert await artifacts.list(run.id) == []
    assert await reports.list(run.id) == []
    assert await events.list_after(run.id) == baseline_events
    assert not runner_root.exists()
    await database.dispose()


async def test_report_service_generates_safe_linked_immutable_outputs(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    report_repository = SQLAlchemyReportRepository(database.session_factory)
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    await engagement_repository.create(Engagement(id="engagement-1", name="Report test"))
    await run_repository.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Inspect <script>alert(1)</script>"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    await run_repository.update_status("run-1", RunStatus.PREPARING)
    await run_repository.update_status("run-1", RunStatus.RUNNING)
    await run_repository.update_status("run-1", RunStatus.COMPLETED)
    raw_path = tmp_path / "raw.txt"
    raw_path.write_text("RAW-TERMINAL-SECRET")
    raw_artifact = Artifact(
        id="artifact-proof",
        run_id="run-1",
        name="proof.txt",
        path=str(raw_path),
        mime_type="text/plain",
        sha256="b" * 64,
        size=19,
        description="Banner proof",
    )
    await artifact_repository.create(raw_artifact)
    await finding_repository.create(
        Finding(
            id="finding-1",
            run_id="run-1",
            title="Unsafe <img src=x onerror=alert(1)>",
            severity=FindingSeverity.HIGH,
            description="Service is externally reachable.",
            evidence=[
                FindingEvidence(
                    artifact_id="artifact-proof",
                    description="Open proof",
                    location="line:1",
                )
            ],
            impact="Metadata disclosure",
            recommendation="Restrict access",
        )
    )
    await event_repository.append(
        "run-1",
        "agent.tool_completed",
        {
            "tool": "scanner",
            "execution_id": "execution-1",
            "status": "exited",
            "exit_code": 0,
            "stdout": "RAW-TERMINAL-SECRET",
            "transcript": "RAW-TERMINAL-SECRET",
        },
    )
    await event_repository.append(
        "run-1",
        "agent.completion_requested",
        {"run_summary": "Verified one exposed service."},
    )
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=RunnerPaths(tmp_path / "runner"),
    )
    service = ReportApplicationService(
        run_repository=run_repository,
        finding_repository=finding_repository,
        artifact_repository=artifact_repository,
        report_repository=report_repository,
        event_repository=event_repository,
        artifact_service=artifact_service,
    )

    reports = await service.generate("run-1", GenerateReports())

    assert [item.format for item in reports] == [
        ReportFormat.MARKDOWN,
        ReportFormat.HTML,
        ReportFormat.JSON,
    ]
    assert all(item.finding_ids == ["finding-1"] for item in reports)
    artifacts = {item.id: item for item in await artifact_repository.list("run-1", limit=1000)}
    contents: dict[ReportFormat, str] = {}
    for report in reports:
        artifact = artifacts[report.artifact_id]
        content = await artifact_service.read_content_slice(
            artifact.id,
            expected_run_id="run-1",
            max_bytes=max(1, artifact.size),
        )
        assert content.eof is True
        contents[report.format] = content.data.decode("utf-8")

    evidence_url = "/api/v1/artifacts/artifact-proof/content"
    assert evidence_url in contents[ReportFormat.MARKDOWN]
    assert evidence_url in contents[ReportFormat.HTML]
    assert evidence_url in contents[ReportFormat.JSON]
    assert "RAW-TERMINAL-SECRET" not in contents[ReportFormat.MARKDOWN]
    assert "RAW-TERMINAL-SECRET" not in contents[ReportFormat.HTML]
    assert "RAW-TERMINAL-SECRET" not in contents[ReportFormat.JSON]
    assert "<script>alert(1)</script>" not in contents[ReportFormat.HTML]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in contents[ReportFormat.HTML]
    assert "<img src=x onerror=alert(1)>" not in contents[ReportFormat.HTML]

    source = await service.build_source("run-1")
    assert [item.id for item in source.artifacts] == ["artifact-proof"]
    tool_event = next(
        item for item in source.key_events if item.event_type == "agent.tool_completed"
    )
    assert tool_event.payload == {
        "tool": "scanner",
        "execution_id": "execution-1",
        "status": "exited",
        "exit_code": 0,
    }

    generated_again = await service.generate(
        "run-1",
        GenerateReports(reuse_existing=True),
    )
    assert [item.id for item in generated_again] == [item.id for item in reports]
    await database.dispose()
