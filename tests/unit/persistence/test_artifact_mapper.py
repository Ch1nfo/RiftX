from riftx.domain import Artifact
from riftx.persistence.mappers import artifact_from_record, artifact_to_record


def test_artifact_mapper_round_trip() -> None:
    artifact = Artifact(
        id="artifact-1",
        run_id="run-1",
        execution_id="execution-1",
        name="scan.xml",
        path="/tmp/run/artifacts/artifact-1/scan.xml",
        mime_type="application/xml",
        sha256="a" * 64,
        size=2048,
        description="Service scan",
    )

    restored = artifact_from_record(artifact_to_record(artifact))

    assert restored == artifact
