from riftx.domain import Report, ReportFormat
from riftx.persistence.mappers import report_from_record, report_to_record


def test_report_mapper_round_trip() -> None:
    report = Report(
        id="report-1",
        run_id="run-1",
        format=ReportFormat.HTML,
        artifact_id="artifact-1",
        finding_ids=["finding-1", "finding-2"],
    )

    restored = report_from_record(report_to_record(report))

    assert restored == report
