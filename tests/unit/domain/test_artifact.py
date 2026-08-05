import pytest
from pydantic import ValidationError

from riftx.domain import (
    ARTIFACT_INGEST_PROVENANCE_SCHEMA_VERSION,
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    canonical_artifact_storage_key,
)


def _artifact(**updates: object) -> Artifact:
    values: dict[str, object] = {
        "id": "artifact-1",
        "run_id": "run-1",
        "name": "result.json",
        "path": "/legacy/result.json",
        "mime_type": "application/json",
        "sha256": "a" * 64,
        "size": 12,
    }
    values.update(updates)
    return Artifact.model_validate(values)


def test_artifact_enums_cover_the_frozen_access_and_trust_contract() -> None:
    assert {value.value for value in ArtifactAccessClass} == {
        "public_export",
        "audit_internal",
        "restricted_sensitive",
    }
    assert {value.value for value in ArtifactContentTrust} == {
        "generated",
        "untrusted_source",
        "untrusted_tool_output",
    }
    assert {value.value for value in ArtifactIngestMethod} == {
        "legacy_migrated",
        "local_nofollow_fd",
        "control_plane_bytes",
        "authenticated_chunk_stream",
    }


def test_legacy_compatible_defaults_are_conservative_and_canonical() -> None:
    artifact = _artifact()

    assert artifact.audit_id is None
    assert artifact.access_class is ArtifactAccessClass.PUBLIC_EXPORT
    assert artifact.content_trust is ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT
    assert artifact.storage_key == ("runs/run-1/artifacts/artifact-1/result.json")
    assert artifact.ingest_provenance == ArtifactIngestProvenance(
        schema_version=ARTIFACT_INGEST_PROVENANCE_SCHEMA_VERSION,
        method=ArtifactIngestMethod.LEGACY_MIGRATED,
    )


def test_artifact_repr_excludes_storage_locators() -> None:
    rendered = repr(_artifact())

    assert "/legacy/result.json" not in rendered
    assert "runs/run-1/artifacts/artifact-1/result.json" not in rendered


@pytest.mark.parametrize("access_class", list(ArtifactAccessClass))
@pytest.mark.parametrize("content_trust", list(ArtifactContentTrust))
def test_audit_owned_artifact_accepts_every_access_and_trust_class(
    access_class: ArtifactAccessClass,
    content_trust: ArtifactContentTrust,
) -> None:
    artifact = _artifact(
        audit_id="audit-1",
        access_class=access_class,
        content_trust=content_trust,
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.AUTHENTICATED_CHUNK_STREAM,
        ),
    )

    assert artifact.access_class is access_class
    assert artifact.content_trust is content_trust


@pytest.mark.parametrize(
    "access_class",
    [
        ArtifactAccessClass.AUDIT_INTERNAL,
        ArtifactAccessClass.RESTRICTED_SENSITIVE,
    ],
)
def test_non_public_artifact_requires_audit_owner(
    access_class: ArtifactAccessClass,
) -> None:
    with pytest.raises(ValidationError, match="requires an Audit owner"):
        _artifact(access_class=access_class)


@pytest.mark.parametrize("omitted", ["access_class", "ingest_provenance"])
def test_audit_owned_artifact_requires_explicit_safe_classification(omitted: str) -> None:
    values: dict[str, object] = {
        "audit_id": "audit-1",
        "access_class": ArtifactAccessClass.RESTRICTED_SENSITIVE,
        "ingest_provenance": ArtifactIngestProvenance(
            method=ArtifactIngestMethod.AUTHENTICATED_CHUNK_STREAM,
        ),
    }
    del values[omitted]

    with pytest.raises(ValidationError, match="explicit"):
        _artifact(**values)


def test_audit_owned_artifact_rejects_legacy_migration_provenance() -> None:
    with pytest.raises(ValidationError, match="legacy ingest provenance"):
        _artifact(
            audit_id="audit-1",
            access_class=ArtifactAccessClass.RESTRICTED_SENSITIVE,
            ingest_provenance=ArtifactIngestProvenance(
                method=ArtifactIngestMethod.LEGACY_MIGRATED,
            ),
        )


@pytest.mark.parametrize("method", list(ArtifactIngestMethod))
def test_artifact_accepts_each_typed_ingest_method(method: ArtifactIngestMethod) -> None:
    artifact = _artifact(
        execution_id="execution-1",
        ingest_provenance=ArtifactIngestProvenance(
            method=method,
            producer_node_id="node-1",
            producer_execution_id="execution-1",
        ),
    )

    assert artifact.ingest_provenance.method is method


def test_producer_execution_must_equal_artifact_execution() -> None:
    with pytest.raises(ValidationError, match="producer_execution_id"):
        _artifact(
            execution_id="execution-1",
            ingest_provenance={
                "method": "local_nofollow_fd",
                "producer_execution_id": "execution-other",
            },
        )


def test_explicit_noncanonical_storage_key_is_never_repaired() -> None:
    with pytest.raises(ValidationError, match="storage_key is not canonical"):
        _artifact(storage_key="runs/run-1/artifacts/artifact-other/result.json")


@pytest.mark.parametrize(
    "component",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\rb",
        "a\nb",
        "a\x7fb",
        "a\x80b",
        "a\u202eb",
        "a\ue000b",
        "审计.txt",
        "x" * 256,
    ],
)
def test_canonical_storage_key_rejects_unsafe_components(component: str) -> None:
    with pytest.raises(ValueError):
        canonical_artifact_storage_key(component, "artifact-1", "result.json")
    with pytest.raises(ValueError):
        canonical_artifact_storage_key("run-1", component, "result.json")
    with pytest.raises(ValueError):
        canonical_artifact_storage_key("run-1", "artifact-1", component)


def test_generated_artifact_identity_and_storage_key_are_one_atomic_default() -> None:
    artifact = Artifact(
        run_id="run-1",
        name="result.json",
        path="/legacy/result.json",
        mime_type="application/json",
        sha256="a" * 64,
        size=12,
    )

    assert artifact.storage_key == canonical_artifact_storage_key(
        artifact.run_id,
        artifact.id,
        artifact.name,
    )


@pytest.mark.parametrize(
    "mime_type",
    [" text/plain", "text/plain\r\nX-Canary: 1", "application/☤"],
)
def test_artifact_rejects_mime_types_that_are_unsafe_for_response_headers(
    mime_type: str,
) -> None:
    with pytest.raises(ValidationError, match="printable ASCII"):
        _artifact(mime_type=mime_type)
