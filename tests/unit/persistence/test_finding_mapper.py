from riftx.domain import Finding, FindingEvidence, FindingSeverity, FindingStatus
from riftx.persistence.mappers import finding_from_record, finding_to_record


def test_finding_mapper_round_trip() -> None:
    finding = Finding(
        id="finding-1",
        run_id="run-1",
        title="Exposed service",
        severity=FindingSeverity.HIGH,
        status=FindingStatus.CONFIRMED,
        affected_assets=["10.0.0.1"],
        description="An administrative service is exposed.",
        evidence=[
            FindingEvidence(
                execution_id="execution-1",
                description="Port responded",
                location="stdout:12",
            )
        ],
        reproduction_steps=["Run the probe"],
        impact="Administrative access may be possible.",
        recommendation="Restrict network access.",
    )

    restored = finding_from_record(finding_to_record(finding))

    assert restored == finding
