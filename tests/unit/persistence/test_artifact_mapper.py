import pytest

from riftx.application.errors import RepositoryIntegrityError
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
)
from riftx.persistence.mappers import artifact_from_record, artifact_to_record


def test_artifact_mapper_round_trip() -> None:
    artifact = Artifact(
        id="artifact-1",
        run_id="run-1",
        execution_id="execution-1",
        audit_id="audit-1",
        access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
        content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
        name="scan.xml",
        path="/tmp/run/artifacts/artifact-1/scan.xml",
        storage_key="runs/run-1/artifacts/artifact-1/scan.xml",
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.LOCAL_NOFOLLOW_FD,
            producer_node_id="node-1",
            producer_execution_id="execution-1",
        ),
        mime_type="application/xml",
        sha256="a" * 64,
        size=2048,
        description="Service scan",
    )

    restored = artifact_from_record(artifact_to_record(artifact))

    assert restored == artifact


@pytest.mark.parametrize(
    ("field", "corrupt_value"),
    [
        ("access_class", "private-canary"),
        ("content_trust", "trusted-canary"),
        ("storage_key", "runs/wrong/artifacts/artifact-1/scan.xml"),
        ("mime_type", "text/plain\r\nX-Canary: 1"),
        ("sha256", "A" * 64),
        (
            "ingest_provenance_json",
            {
                "schema_version": "riftx.artifact-ingest-provenance/v1",
                "method": "corrupt-canary",
                "producer_node_id": None,
                "producer_execution_id": None,
            },
        ),
    ],
)
def test_artifact_mapper_normalizes_corrupt_rows(
    field: str,
    corrupt_value: object,
) -> None:
    record = artifact_to_record(
        Artifact(
            id="artifact-1",
            run_id="run-1",
            name="scan.xml",
            path="/private/path-canary",
            mime_type="application/xml",
            sha256="a" * 64,
            size=2048,
        )
    )
    setattr(record, field, corrupt_value)

    with pytest.raises(RepositoryIntegrityError) as raised:
        artifact_from_record(record)

    assert raised.value.entity == "Artifact"
    assert raised.value.entity_id == "artifact-1"
    assert "canary" not in str(raised.value)
    assert "/private/path-canary" not in str(raised.value)
