"""AUD-105 application contracts for immutable Artifact bytes and metadata."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import riftx.application.services.artifacts as artifact_service_module
from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ResourceNotAccessibleError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    ArtifactOwnerBinding,
    ArtifactRepository,
    AuditAggregateReadRepository,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.application.services.artifacts import (
    ArtifactApplicationService,
    ArtifactContentSlice,
    RegisterArtifact,
    RegisterArtifactContent,
)
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    Objective,
    Run,
    RunKind,
)
from riftx.runner import (
    ArtifactContentFailure,
    ArtifactContentStoreError,
    LocalArtifactContentStore,
    RunnerPaths,
    StoredArtifactContent,
)


class _RunReads:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def get(self, run_id: str) -> Run | None:
        return self.run if run_id == self.run.id else None


class _UnusedExecutions:
    async def get(self, _execution_id: str) -> None:
        return None


class _AuditReads:
    def __init__(self, *, audit_id: str, run_id: str) -> None:
        self._audit_id = audit_id
        self._run_id = run_id

    async def get_by_run_authorized(self, run_id: str, *, authorize: object) -> object:
        assert run_id == self._run_id
        authorize(  # type: ignore[operator]
            SimpleNamespace(
                requested_audit_id=self._audit_id,
                audit_id=self._audit_id,
                scan_run_id=self._run_id,
                run_id=self._run_id,
                run_kind=RunKind.CODE_AUDIT.value,
            )
        )
        return SimpleNamespace(
            audit=SimpleNamespace(value=SimpleNamespace(id=self._audit_id))
        )


class _Events:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, dict[str, object]]] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> None:
        self.appended.append((run_id, event_type, payload or {}))


class _Artifacts:
    def __init__(self) -> None:
        self.current: Artifact | None = None
        self.created: list[Artifact] = []
        self.create_error: Exception | None = None
        self.read_error: Exception | None = None

    def _read(self) -> None:
        if self.read_error is not None:
            raise self.read_error

    async def create(self, artifact: Artifact) -> Artifact:
        self.created.append(artifact)
        if self.create_error is not None:
            raise self.create_error
        self.current = artifact
        return artifact

    async def get_run_id(self, artifact_id: str) -> str | None:
        self._read()
        if self.current is None or self.current.id != artifact_id:
            return None
        return self.current.run_id

    async def get(self, artifact_id: str) -> Artifact | None:
        self._read()
        if self.current is None or self.current.id != artifact_id:
            return None
        return self.current

    async def get_for_reconciliation(self, artifact_id: str) -> Artifact | None:
        return await self.get(artifact_id)

    async def resolve_owner(self, artifact_id: str) -> ArtifactOwnerBinding | None:
        self._read()
        artifact = await self.get(artifact_id)
        if artifact is None:
            return None
        return ArtifactOwnerBinding(
            artifact_id=artifact.id,
            run_id=artifact.run_id,
            audit_id=artifact.audit_id,
            access_class=artifact.access_class,
            run_kind=(RunKind.CODE_AUDIT if artifact.audit_id is not None else RunKind.GENERAL),
            audit_run_id=artifact.run_id if artifact.audit_id is not None else None,
        )

    async def get_for_audit(
        self,
        artifact_id: str,
        audit_id: str,
        run_id: str,
    ) -> Artifact | None:
        self._read()
        artifact = await self.get(artifact_id)
        if artifact is None:
            return None
        if artifact.audit_id != audit_id or artifact.run_id != run_id:
            return None
        return artifact

    async def list(
        self,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Artifact]:
        self._read()
        values = [
            artifact
            for artifact in (self.current,)
            if artifact is not None
            and artifact.run_id == run_id
            and (execution_id is None or artifact.execution_id == execution_id)
        ]
        return values[offset : offset + limit]

    async def list_for_audit(
        self,
        audit_id: str,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Artifact]:
        self._read()
        values = await self.list(
            run_id,
            execution_id=execution_id,
            limit=limit,
            offset=offset,
        )
        return [artifact for artifact in values if artifact.audit_id == audit_id]


class _AmbiguousCreateArtifacts(_Artifacts):
    def __init__(
        self,
        outcome: Callable[[Artifact], Artifact | None],
        *,
        read_fails: bool = False,
    ) -> None:
        super().__init__()
        self._outcome = outcome
        self._read_fails = read_fails

    async def create(self, artifact: Artifact) -> Artifact:
        self.created.append(artifact)
        self.current = self._outcome(artifact)
        raise RepositoryUnavailableError("create outcome is unknown")

    async def get_for_reconciliation(self, artifact_id: str) -> Artifact | None:
        if self._read_fails:
            raise RepositoryUnavailableError("reconciliation unavailable")
        return await super().get_for_reconciliation(artifact_id)


class _BlockingCreateArtifacts(_Artifacts):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def create(self, artifact: Artifact) -> Artifact:
        self.created.append(artifact)
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class _BlockingReconciliationArtifacts(_BlockingCreateArtifacts):
    def __init__(self) -> None:
        super().__init__()
        self.reconciliation_started = asyncio.Event()
        self.reconciliation_release = asyncio.Event()

    async def get_for_reconciliation(self, _artifact_id: str) -> Artifact | None:
        self.reconciliation_started.set()
        await self.reconciliation_release.wait()
        return None


class _FaultyOwnerArtifacts(_Artifacts):
    """Return a row without applying any requested identity predicate."""

    async def get(self, _artifact_id: str) -> Artifact | None:
        return self.current

    async def get_for_audit(
        self,
        _artifact_id: str,
        _audit_id: str,
        _run_id: str,
    ) -> Artifact | None:
        return self.current


class _BlockingSnapshotStore(LocalArtifactContentStore):
    def __init__(self, paths: RunnerPaths, *, max_artifact_bytes: int) -> None:
        super().__init__(paths, max_artifact_bytes=max_artifact_bytes)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.storage_key: str | None = None
        self.discard_calls = 0

    def snapshot_bytes(
        self,
        content: bytes,
        *,
        storage_key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> StoredArtifactContent:
        self.storage_key = storage_key
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release snapshot")
        result = super().snapshot_bytes(
            content,
            storage_key=storage_key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        self.finished.set()
        return result

    def discard(self, storage_key: str) -> None:
        assert self.finished.is_set()
        self.discard_calls += 1
        super().discard(storage_key)


class _Lease:
    def __init__(self, content: bytes, *, failure: bool = False) -> None:
        self._content = content
        self._offset = 0
        self._failure = failure
        self.closed = False
        self.verified = False

    def seek(self, offset: int) -> None:
        self._offset = offset

    def read(self, max_bytes: int) -> bytes:
        if self._failure:
            raise ArtifactContentStoreError(ArtifactContentFailure.STORAGE_INTEGRITY)
        result = self._content[self._offset : self._offset + max_bytes]
        self._offset += len(result)
        return result

    def verify_unchanged(self) -> None:
        self.verified = True

    def close(self) -> None:
        self.closed = True


class _BlockingLease(_Lease):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.started = threading.Event()
        self.release = threading.Event()
        self.active = False
        self.closed_while_active = False

    def read(self, max_bytes: int) -> bytes:
        self.active = True
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release read")
        try:
            return super().read(max_bytes)
        finally:
            self.active = False

    def close(self) -> None:
        self.closed_while_active |= self.active
        super().close()


class _OpenStore:
    def __init__(self, lease: _Lease | None = None) -> None:
        self.max_artifact_bytes = 1024
        self.lease = lease
        self.open_calls: list[tuple[str, str, int]] = []

    def open_verified(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> _Lease:
        self.open_calls.append((storage_key, expected_sha256, expected_size))
        if self.lease is None:
            raise AssertionError("Artifact content must not be opened")
        return self.lease


class _BlockingOpenStore(_OpenStore):
    def __init__(self, lease: _Lease) -> None:
        super().__init__(lease)
        self.started = threading.Event()
        self.release = threading.Event()

    def open_verified(
        self,
        *,
        storage_key: str,
        expected_sha256: str,
        expected_size: int,
    ) -> _Lease:
        self.open_calls.append((storage_key, expected_sha256, expected_size))
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release open")
        assert self.lease is not None
        return self.lease


def _run(tmp_path: Path) -> Run:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return Run(
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Artifact application contract"),
        kind=RunKind.GENERAL,
        workspace_path=str(workspace),
    )


def _service(
    tmp_path: Path,
    artifacts: _Artifacts,
    *,
    content_store: object | None = None,
    max_artifact_bytes: int = 1024,
    run: Run | None = None,
    audits: object | None = None,
) -> tuple[ArtifactApplicationService, _Events, RunnerPaths]:
    run = run or _run(tmp_path)
    events = _Events()
    paths = RunnerPaths(tmp_path / "runner")
    service = ArtifactApplicationService(
        run_repository=cast(RunRepository, _RunReads(run)),
        execution_repository=cast(ExecutionRepository, _UnusedExecutions()),
        artifact_repository=cast(ArtifactRepository, artifacts),
        event_repository=cast(RunEventRepository, events),
        paths=paths,
        max_artifact_bytes=max_artifact_bytes,
        content_store=cast(LocalArtifactContentStore, content_store),
        audit_repository=cast(AuditAggregateReadRepository, audits),
    )
    return service, events, paths


def _artifact(
    paths: RunnerPaths,
    *,
    artifact_id: str = "artifact-1",
    run_id: str = "run-1",
    audit_id: str | None = None,
    access_class: ArtifactAccessClass = ArtifactAccessClass.PUBLIC_EXPORT,
    name: str = "evidence.txt",
    content: bytes = b"payload",
) -> Artifact:
    destination = paths.artifact(run_id, artifact_id, name)
    return Artifact(
        id=artifact_id,
        run_id=run_id,
        audit_id=audit_id,
        access_class=access_class,
        content_trust=ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT,
        name=name,
        path=f"/legacy/{artifact_id}/{name}",
        storage_key=destination.storage_key,
        ingest_provenance=ArtifactIngestProvenance(
            method=(
                ArtifactIngestMethod.AUTHENTICATED_CHUNK_STREAM
                if audit_id is not None
                else ArtifactIngestMethod.LEGACY_MIGRATED
            ),
            producer_node_id="node-1" if audit_id is not None else None,
        ),
        mime_type="text/plain",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def _different_persisted_artifact(artifact: Artifact) -> Artifact:
    paths = RunnerPaths(Path("/tmp/riftx-artifact-contract-placeholder"))
    return _artifact(
        paths,
        artifact_id=artifact.id,
        run_id=artifact.run_id,
        name="foreign.txt",
        content=b"foreign",
    )


@pytest.mark.parametrize(
    "mime_type",
    (
        "application/\N{CADUCEUS}",
        "text/plain\r\nX-Injected: true",
        " text/plain",
    ),
    ids=("non-ascii", "crlf", "leading-space"),
)
async def test_invalid_mime_is_rejected_before_snapshot_metadata_or_event(
    tmp_path: Path,
    mime_type: str,
) -> None:
    artifacts = _Artifacts()
    store = _OpenStore()
    service, events, paths = _service(tmp_path, artifacts, content_store=store)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"must not be stored",
                name="invalid-mime.txt",
                mime_type=mime_type,
            ),
        )

    assert captured.value.code == "invalid_artifact_mime_type"
    assert artifacts.created == []
    assert events.appended == []
    assert store.open_calls == []
    assert paths.root.exists() is False


async def test_audit_content_registration_and_read_are_exact_owner_bound(
    tmp_path: Path,
) -> None:
    audit_id = "audit-1"
    run = _run(tmp_path).model_copy(update={"kind": RunKind.CODE_AUDIT})
    artifacts = _Artifacts()
    service, events, _ = _service(
        tmp_path,
        artifacts,
        run=run,
        audits=_AuditReads(audit_id=audit_id, run_id=run.id),
    )
    content = b"owner-bound code source"

    artifact = await service.register_audit_content(
        audit_id,
        run.id,
        RegisterArtifactContent(
            content=content,
            name="source.bin",
            mime_type="application/octet-stream",
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
        ),
    )
    result = await service.read_audit_content_slice(
        artifact.id,
        audit_id=audit_id,
        run_id=run.id,
        max_bytes=5,
    )

    assert artifact.audit_id == audit_id
    assert artifact.access_class is ArtifactAccessClass.AUDIT_INTERNAL
    assert artifact.ingest_provenance.method is ArtifactIngestMethod.CONTROL_PLANE_BYTES
    assert result.data == content[:5]
    assert result.eof is False
    assert events.appended[0][2]["audit_id"] == audit_id

    with pytest.raises(ResourceNotAccessibleError):
        await service.register_audit_content(
            "foreign-audit",
            run.id,
            RegisterArtifactContent(
                content=b"must not persist",
                name="foreign.bin",
                mime_type="application/octet-stream",
            ),
        )
    assert len(artifacts.created) == 1


async def test_oversize_file_registration_has_no_metadata_event_or_blob_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_service_module, "new_id", lambda: "artifact-oversize")
    artifacts = _Artifacts()
    service, events, paths = _service(
        tmp_path,
        artifacts,
        max_artifact_bytes=4,
    )
    source = tmp_path / "workspace" / "oversize.bin"
    source.write_bytes(b"12345")
    destination = paths.artifact("run-1", "artifact-oversize", source.name)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.register("run-1", RegisterArtifact(source_path=str(source)))

    assert captured.value.code == "artifact_size_limit_exceeded"
    assert artifacts.created == []
    assert events.appended == []
    assert not destination.content.exists()
    assert not destination.directory.exists()


@pytest.mark.parametrize(
    "create_error",
    (
        RepositoryConflictError("duplicate Artifact"),
        RepositoryUnavailableError("write failed"),
    ),
    ids=("conflict", "failure"),
)
async def test_repository_create_failure_discards_published_blob(
    tmp_path: Path,
    create_error: Exception,
) -> None:
    artifacts = _Artifacts()
    artifacts.create_error = create_error
    service, events, paths = _service(tmp_path, artifacts)

    with pytest.raises(type(create_error)):
        await service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"uncommitted",
                name="failure.txt",
                mime_type="text/plain",
            ),
        )

    [attempted] = artifacts.created
    destination = paths.artifact(attempted.run_id, attempted.id, attempted.name)
    assert events.appended == []
    assert not destination.content.exists()
    assert not destination.directory.exists()


@pytest.mark.parametrize(
    ("outcome", "read_fails", "retained"),
    (
        (lambda artifact: artifact, False, True),
        (_different_persisted_artifact, False, False),
        (lambda _artifact: None, False, False),
        # When the commit outcome cannot be reconciled, retaining a possible
        # orphan is safer than deleting bytes an exact committed row may own.
        (lambda _artifact: None, True, True),
    ),
    ids=("exact-row", "different-row", "absent-row", "reconciliation-failed"),
)
async def test_ambiguous_create_discards_blob_only_when_row_is_proven_nonmatching(
    tmp_path: Path,
    outcome: Callable[[Artifact], Artifact | None],
    read_fails: bool,
    retained: bool,
) -> None:
    artifacts = _AmbiguousCreateArtifacts(outcome, read_fails=read_fails)
    service, events, paths = _service(tmp_path, artifacts)

    with pytest.raises(RepositoryUnavailableError):
        await service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"ambiguous",
                name="ambiguous.txt",
                mime_type="text/plain",
            ),
        )

    [attempted] = artifacts.created
    destination = paths.artifact(attempted.run_id, attempted.id, attempted.name)
    assert destination.content.exists() is retained
    assert destination.directory.exists() is retained
    assert events.appended == []


@pytest.mark.parametrize("cancellation_count", (1, 2), ids=("single", "double"))
async def test_cancelling_snapshot_waits_then_discards_completed_publish(
    tmp_path: Path,
    cancellation_count: int,
) -> None:
    artifacts = _Artifacts()
    paths = RunnerPaths(tmp_path / "runner")
    store = _BlockingSnapshotStore(paths, max_artifact_bytes=1024)
    service, events, _ = _service(tmp_path, artifacts, content_store=store)
    task = asyncio.create_task(
        service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"cancel snapshot",
                name="snapshot.txt",
                mime_type="text/plain",
            ),
        )
    )
    assert await asyncio.to_thread(store.started.wait, 2)

    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert store.finished.is_set() is False
    assert store.discard_calls == 0
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.storage_key is not None
    destination = paths.artifact_from_storage_key(store.storage_key)
    assert artifacts.created == []
    assert events.appended == []
    assert store.finished.is_set() is True
    assert store.discard_calls == 1
    assert not destination.exists()
    assert not destination.parent.exists()


async def test_cancelling_database_create_discards_uncommitted_blob(
    tmp_path: Path,
) -> None:
    artifacts = _BlockingCreateArtifacts()
    service, events, paths = _service(tmp_path, artifacts)
    task = asyncio.create_task(
        service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"cancel create",
                name="create.txt",
                mime_type="text/plain",
            ),
        )
    )
    await asyncio.wait_for(artifacts.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [attempted] = artifacts.created
    destination = paths.artifact(attempted.run_id, attempted.id, attempted.name)
    assert artifacts.current is None
    assert events.appended == []
    assert not destination.content.exists()
    assert not destination.directory.exists()


async def test_double_cancellation_waits_for_database_reconciliation_cleanup(
    tmp_path: Path,
) -> None:
    artifacts = _BlockingReconciliationArtifacts()
    service, events, paths = _service(tmp_path, artifacts)
    task = asyncio.create_task(
        service.register_content(
            "run-1",
            RegisterArtifactContent(
                content=b"double cancel create",
                name="double-create.txt",
                mime_type="text/plain",
            ),
        )
    )
    await asyncio.wait_for(artifacts.started.wait(), timeout=2)

    task.cancel()
    await asyncio.wait_for(artifacts.reconciliation_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    artifacts.reconciliation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [attempted] = artifacts.created
    destination = paths.artifact(attempted.run_id, attempted.id, attempted.name)
    assert events.appended == []
    assert not destination.content.exists()
    assert not destination.directory.exists()


_RepositoryOperation = Callable[[ArtifactApplicationService], Awaitable[object]]


def _repository_operations() -> tuple[tuple[str, _RepositoryOperation], ...]:
    return (
        ("get", lambda service: service.get("artifact-1")),
        ("resolve-run", lambda service: service.resolve_run_id("artifact-1")),
        ("resolve-owner", lambda service: service.resolve_owner("artifact-1")),
        (
            "get-for-audit",
            lambda service: service.get_for_audit("artifact-1", audit_id="audit-1", run_id="run-1"),
        ),
        ("list", lambda service: service.list("run-1")),
        (
            "list-for-audit",
            lambda service: service.list_for_audit("audit-1", "run-1"),
        ),
        (
            "open-public",
            lambda service: service.open_public_content("artifact-1", expected_run_id="run-1"),
        ),
        (
            "open-audit",
            lambda service: service.open_audit_content(
                "artifact-1", audit_id="audit-1", run_id="run-1"
            ),
        ),
    )


@pytest.mark.parametrize(
    ("operation_name", "operation"),
    _repository_operations(),
    ids=lambda value: value if isinstance(value, str) else None,
)
@pytest.mark.parametrize(
    "repository_error",
    (
        RepositoryIntegrityError(
            "Artifact",
            "/private/RIFTX_PERSISTENCE_CANARY",
            reason_code="invalid_persisted_state",
        ),
        RepositoryUnavailableError("RIFTX_PERSISTENCE_CANARY raw driver failure"),
    ),
    ids=("integrity", "unavailable"),
)
async def test_repository_read_failures_map_to_sanitized_unavailable(
    tmp_path: Path,
    operation_name: str,
    operation: _RepositoryOperation,
    repository_error: Exception,
) -> None:
    artifacts = _Artifacts()
    artifacts.read_error = repository_error
    store = _OpenStore()
    service, _events, _paths = _service(tmp_path, artifacts, content_store=store)

    with pytest.raises(ServiceUnavailableError) as captured:
        await operation(service)

    assert operation_name
    assert captured.value.code == "artifact_persistence_unavailable"
    assert captured.value.message == "Artifact metadata is temporarily unavailable"
    assert captured.value.details == {}
    assert "RIFTX_PERSISTENCE_CANARY" not in str(captured.value)
    assert store.open_calls == []


@pytest.mark.parametrize("mode", ("public", "audit"))
async def test_exact_owner_mismatch_is_rejected_before_content_open(
    tmp_path: Path,
    mode: str,
) -> None:
    artifacts = _FaultyOwnerArtifacts()
    store = _OpenStore()
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    if mode == "public":
        artifacts.current = _artifact(paths, artifact_id="foreign-artifact")
        operation = service.open_public_content(
            "artifact-1",
            expected_run_id="run-1",
        )
    else:
        artifacts.current = _artifact(
            paths,
            audit_id="foreign-audit",
            access_class=ArtifactAccessClass.AUDIT_INTERNAL,
        )
        # A repository must apply the SQL owner predicate, but the application
        # boundary still revalidates a malicious or faulty implementation.
        operation = service.open_audit_content(
            "artifact-1",
            audit_id="audit-1",
            run_id="run-1",
        )

    with pytest.raises(ResourceNotAccessibleError) as captured:
        await operation

    assert captured.value.code == "resource_not_accessible"
    assert store.open_calls == []


async def test_noncanonical_storage_key_is_rejected_without_store_open(
    tmp_path: Path,
) -> None:
    artifacts = _Artifacts()
    store = _OpenStore()
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    valid = _artifact(paths)
    artifacts.current = valid.model_copy(
        update={"storage_key": "runs/run-1/artifacts/artifact-1/foreign.txt"}
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.open_public_content("artifact-1", expected_run_id="run-1")

    assert captured.value.code == "artifact_integrity_mismatch"
    assert store.open_calls == []


@pytest.mark.parametrize("cancellation_count", (1, 2), ids=("single", "double"))
async def test_cancelling_content_open_waits_then_closes_returned_lease(
    tmp_path: Path,
    cancellation_count: int,
) -> None:
    artifacts = _Artifacts()
    lease = _Lease(b"payload")
    store = _BlockingOpenStore(lease)
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    artifacts.current = _artifact(paths)
    task = asyncio.create_task(service.open_public_content("artifact-1", expected_run_id="run-1"))
    assert await asyncio.to_thread(store.started.wait, 2)

    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert lease.closed is False
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.closed is True


async def test_read_content_slice_closes_verified_lease_on_success(
    tmp_path: Path,
) -> None:
    artifacts = _Artifacts()
    lease = _Lease(b"payload")
    store = _OpenStore(lease)
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    artifacts.current = _artifact(paths)

    result = await service.read_content_slice(
        "artifact-1",
        expected_run_id="run-1",
        offset=2,
        max_bytes=3,
    )

    assert result == ArtifactContentSlice(
        artifact=artifacts.current,
        data=b"ylo",
        offset=2,
        next_offset=5,
        eof=False,
    )
    assert lease.verified is True
    assert lease.closed is True


@pytest.mark.parametrize("failure", ("offset", "read"))
async def test_read_content_slice_closes_verified_lease_on_failure(
    tmp_path: Path,
    failure: str,
) -> None:
    artifacts = _Artifacts()
    lease = _Lease(b"payload", failure=failure == "read")
    store = _OpenStore(lease)
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    artifacts.current = _artifact(paths)

    if failure == "offset":
        with pytest.raises(ValueError, match="beyond content size"):
            await service.read_content_slice(
                "artifact-1",
                expected_run_id="run-1",
                offset=8,
                max_bytes=3,
            )
    else:
        with pytest.raises(ApplicationConflictError) as captured:
            await service.read_content_slice(
                "artifact-1",
                expected_run_id="run-1",
                max_bytes=3,
            )
        assert captured.value.code == "artifact_integrity_mismatch"

    assert lease.closed is True


@pytest.mark.parametrize("cancellation_count", (1, 2), ids=("single", "double"))
async def test_cancelling_read_slice_waits_for_worker_then_closes_lease(
    tmp_path: Path,
    cancellation_count: int,
) -> None:
    artifacts = _Artifacts()
    lease = _BlockingLease(b"payload")
    store = _OpenStore(lease)
    service, _events, paths = _service(tmp_path, artifacts, content_store=store)
    artifacts.current = _artifact(paths)
    task = asyncio.create_task(
        service.read_content_slice(
            "artifact-1",
            expected_run_id="run-1",
            max_bytes=3,
        )
    )
    assert await asyncio.to_thread(lease.started.wait, 2)

    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)

    assert task.done() is False
    assert lease.closed is False
    assert lease.closed_while_active is False
    lease.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.closed is True
    assert lease.closed_while_active is False
