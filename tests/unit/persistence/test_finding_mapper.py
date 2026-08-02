from datetime import UTC, datetime, timedelta

import pytest

from riftx.domain import Finding, FindingEvidence, FindingSeverity, FindingStatus
from riftx.persistence.mappers import (
    apply_finding_to_record,
    finding_from_record,
    finding_to_record,
)


def test_finding_mapper_round_trip() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at = created_at + timedelta(seconds=1)
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

    restored = finding_from_record(
        finding_to_record(
            finding,
            created_at=created_at,
            updated_at=updated_at,
        )
    )

    assert restored == finding.model_copy(
        update={"created_at": created_at, "updated_at": updated_at}
    )

    updated = finding.model_copy(
        update={"title": "Updated service", "status": FindingStatus.RESOLVED}
    )
    record = finding_to_record(
        finding,
        created_at=finding.created_at,
        updated_at=finding.updated_at,
    )
    original_updated_at = record.updated_at
    apply_finding_to_record(updated, record)
    assert finding_from_record(record) == updated.model_copy(
        update={"updated_at": original_updated_at}
    )
    assert record.updated_at == original_updated_at


@pytest.mark.parametrize("field_name", ["id", "run_id", "created_at"])
def test_finding_mapper_rejects_immutable_field_changes(field_name: str) -> None:
    finding = Finding(
        id="finding-1",
        run_id="run-1",
        title="Finding",
        severity=FindingSeverity.INFO,
    )
    record = finding_to_record(
        finding,
        created_at=finding.created_at,
        updated_at=finding.updated_at,
    )
    replacement = {
        "id": "finding-2",
        "run_id": "run-2",
        "created_at": finding.created_at + timedelta(seconds=1),
    }[field_name]

    with pytest.raises(ValueError, match="immutable"):
        apply_finding_to_record(
            finding.model_copy(update={field_name: replacement}),
            record,
        )
