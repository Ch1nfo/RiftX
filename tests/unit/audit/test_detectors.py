from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from riftx.audit import (
    DetectorCancellation,
    DetectorFailure,
    DetectorFileStatus,
    DetectorInput,
    DetectorMatch,
    DetectorRegistry,
    DetectorRuleMetadata,
    DetectorRunLimits,
    FileInventory,
    LocalDetectorRunner,
    LocalSnapshotViewEntry,
    LocalSnapshotViewError,
    LocalSnapshotViewFailure,
    SnapshotBlobObjectType,
    SourceCaptureDecision,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
    build_file_inventory,
)


def _digest(value: bytes | str) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def _entry(
    relative_path: str,
    content: str,
    *,
    language: str,
    category: SourceClassification = SourceClassification.SOURCE,
) -> SourceManifestEntry:
    encoded = content.encode("utf-8")
    return SourceManifestEntry(
        path=SourceManifestPath.from_bytes(relative_path.encode("utf-8")),
        object_type=SourceManifestObjectType.REGULAR_FILE,
        origin=SourceManifestOrigin.LOCAL_DIRECTORY,
        mode=0o100644,
        size=len(encoded),
        sha256=_digest(encoded),
        git_blob_id=None,
        language=language,
        classification=category,
        decision=SourceCaptureDecision.INCLUDED,
        reason=SourceCaptureReason.INCLUDED,
    )


def _inventory(*entries: SourceManifestEntry) -> FileInventory:
    manifest = SourceManifest.create(
        source_kind=SourceManifestSourceKind.DIRECTORY,
        commit_sha=None,
        head_commit_sha=None,
        capture_policy_digest=_digest("policy"),
        entries=entries,
    )
    return build_file_inventory(manifest)


class _MemoryView:
    def __init__(
        self,
        inventory: FileInventory,
        content: dict[str, str],
        *,
        failures: set[str] | None = None,
    ) -> None:
        self._content = content
        self._failures = failures or set()
        self.reads: list[str] = []
        self._entries = tuple(
            LocalSnapshotViewEntry(
                relative_path=entry.relative_path or "invalid",
                object_type=SnapshotBlobObjectType.REGULAR_FILE,
                size=entry.size or 0,
                mode=0o100644,
                content_digest=entry.blob_digest or _digest("missing"),
            )
            for entry in inventory.included_entries()
        )

    def entries(self):
        return self._entries

    def read_text(
        self,
        relative_path: str,
        *,
        max_bytes: int,
        max_characters: int,
    ) -> str:
        self.reads.append(relative_path)
        if relative_path in self._failures:
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.SNAPSHOT_INTEGRITY)
        value = self._content[relative_path]
        if len(value.encode("utf-8")) > max_bytes or len(value) > max_characters:
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED)
        return value


class _Detector:
    def __init__(self, metadata: DetectorRuleMetadata, operation) -> None:
        self._metadata = metadata
        self._operation = operation
        self.inputs: list[DetectorInput] = []

    @property
    def metadata(self) -> DetectorRuleMetadata:
        return self._metadata

    def detect(self, detector_input: DetectorInput):
        self.inputs.append(detector_input)
        return self._operation(detector_input)


def _metadata(
    rule_id: str,
    *,
    languages: tuple[str, ...] = (),
    categories: tuple[SourceClassification, ...] = (),
) -> DetectorRuleMetadata:
    return DetectorRuleMetadata(
        rule_id=rule_id,
        version="1.0.0",
        implementation_digest=_digest(f"implementation:{rule_id}"),
        title=f"Rule {rule_id}",
        supported_languages=languages,
        supported_categories=categories,
    )


def _runner(*detectors: _Detector, **limit_updates: int) -> LocalDetectorRunner:
    limits = DetectorRunLimits(**limit_updates)
    return LocalDetectorRunner(DetectorRegistry(detectors), limits)


def test_registry_metadata_is_fixed_validated_and_deterministic() -> None:
    python = _Detector(_metadata("python.rule", languages=("python",)), lambda _: ())
    generic = _Detector(_metadata("generic.rule"), lambda _: ())

    first = DetectorRegistry((python, generic))
    second = DetectorRegistry((generic, python))

    assert [value.rule_id for value in first.metadata()] == [
        "generic.rule",
        "python.rule",
    ]
    assert first.registry_digest == second.registry_digest
    with pytest.raises(FrozenInstanceError):
        first.metadata()[0].title = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unique"):
        DetectorRegistry((generic, generic))
    with pytest.raises(ValueError, match="sorted"):
        _metadata("bad.languages", languages=("python", "javascript"))
    with pytest.raises(ValueError, match="rule_id"):
        _metadata("Bad Rule")


def test_runner_orders_files_rules_and_matches_deterministically() -> None:
    javascript_content = "first\nsecond\n"
    python_content = "alpha\nbeta\n"
    inventory = _inventory(
        _entry("z.py", python_content, language="python"),
        _entry("a.js", javascript_content, language="javascript"),
    )
    view = _MemoryView(
        inventory,
        {"a.js": javascript_content, "z.py": python_content},
    )
    later = _Detector(
        _metadata("z.rule"),
        lambda _: (
            DetectorMatch(2, 1, "second match", "private-evidence"),
            DetectorMatch(1, 1, "first match", "first"),
        ),
    )
    earlier = _Detector(
        _metadata("a.rule", languages=("python",)),
        lambda _: (DetectorMatch(1, 2, "python match", "alpha"),),
    )

    first = _runner(later, earlier).run(view=view, inventory=inventory)
    second = _runner(earlier, later).run(
        view=_MemoryView(
            inventory,
            {"a.js": javascript_content, "z.py": python_content},
        ),
        inventory=inventory,
    )

    assert first == second
    assert view.reads == ["a.js", "z.py"]
    assert [(value.relative_path, value.line, value.rule_id) for value in first.signals] == [
        ("a.js", 1, "z.rule"),
        ("a.js", 2, "z.rule"),
        ("z.py", 1, "z.rule"),
        ("z.py", 1, "a.rule"),
        ("z.py", 2, "z.rule"),
    ]
    assert first.files[0].status is DetectorFileStatus.COMPLETED
    assert first.cancelled is False
    assert "alpha" not in repr(earlier.inputs[0])
    assert "private-evidence" not in repr(first.signals[1])


def test_detector_failure_and_invalid_output_are_isolated_per_rule_and_file() -> None:
    content = {"a.py": "alpha\n", "b.py": "beta\n", "c.py": "gamma\n"}
    inventory = _inventory(
        *(
            _entry(path, value, language="python")
            for path, value in content.items()
        )
    )
    view = _MemoryView(inventory, content, failures={"b.py"})
    failing = _Detector(
        _metadata("a.failure"),
        lambda _: (_ for _ in ()).throw(RuntimeError("private target failure")),
    )
    invalid = _Detector(
        _metadata("b.invalid"),
        lambda _: (DetectorMatch(99, 1, "bad", "bad"),),
    )
    working = _Detector(
        _metadata("c.working"),
        lambda value: (DetectorMatch(1, 1, "found", value.content.rstrip()),),
    )

    receipt = _runner(failing, invalid, working).run(view=view, inventory=inventory)

    assert [value.status for value in receipt.files] == [
        DetectorFileStatus.COMPLETED,
        DetectorFileStatus.FAILED,
        DetectorFileStatus.COMPLETED,
    ]
    assert receipt.files[1].reason is DetectorFailure.INPUT_READ_FAILED
    assert len(receipt.files[0].failures) == 2
    assert [value.reason for value in receipt.files[0].failures] == [
        DetectorFailure.DETECTOR_FAILED,
        DetectorFailure.OUTPUT_INVALID,
    ]
    assert [(value.relative_path, value.rule_id) for value in receipt.signals] == [
        ("a.py", "c.working"),
        ("c.py", "c.working"),
    ]
    assert "private target failure" not in repr(receipt)


def test_runner_enforces_input_output_and_snapshot_limits() -> None:
    inventory = _inventory(
        _entry("a.py", "alpha\n", language="python"),
        _entry("b.py", "beta\n", language="python"),
    )
    view = _MemoryView(inventory, {"a.py": "alpha\n", "b.py": "beta\n"})
    metadata = list(view._entries)
    metadata[0] = LocalSnapshotViewEntry(
        relative_path=metadata[0].relative_path,
        object_type=metadata[0].object_type,
        size=metadata[0].size,
        mode=metadata[0].mode,
        content_digest=_digest("wrong"),
    )
    view._entries = tuple(metadata)
    noisy = _Detector(
        _metadata("noisy.rule"),
        lambda _: (
            DetectorMatch(1, 1, "one", "one"),
            DetectorMatch(1, 2, "two", "two"),
        ),
    )

    receipt = _runner(noisy, max_matches_per_rule_file=1).run(
        view=view,
        inventory=inventory,
    )

    assert receipt.files[0].reason is DetectorFailure.SNAPSHOT_MISMATCH
    assert receipt.files[1].failures[0].reason is DetectorFailure.OUTPUT_LIMIT_EXCEEDED
    assert receipt.signals == ()

    input_limited = _runner(noisy, max_file_bytes=4).run(
        view=_MemoryView(inventory, {"a.py": "alpha\n", "b.py": "beta\n"}),
        inventory=inventory,
    )
    assert all(
        value.reason is DetectorFailure.INPUT_LIMIT_EXCEEDED
        for value in input_limited.files
    )


def test_cancel_fence_discards_in_flight_detector_output_and_stops_next_file() -> None:
    inventory = _inventory(
        _entry("a.py", "alpha\n", language="python"),
        _entry("b.py", "beta\n", language="python"),
    )
    view = _MemoryView(inventory, {"a.py": "alpha\n", "b.py": "beta\n"})
    cancellation = DetectorCancellation()

    def cancel_during_detection(_value: DetectorInput):
        cancellation.cancel()
        return (DetectorMatch(1, 1, "must be fenced", "secret"),)

    detector = _Detector(_metadata("cancel.rule"), cancel_during_detection)
    receipt = _runner(detector).run(
        view=view,
        inventory=inventory,
        cancellation=cancellation,
    )

    assert receipt.cancelled is True
    assert receipt.signals == ()
    assert len(receipt.files) == 1
    assert receipt.files[0].status is DetectorFileStatus.CANCELLED
    assert view.reads == ["a.py"]
    assert len(detector.inputs) == 1


def test_total_output_budget_is_global_and_does_not_publish_partial_invocations() -> None:
    inventory = _inventory(
        _entry("a.py", "alpha\n", language="python"),
        _entry("b.py", "beta\n", language="python"),
    )
    detector = _Detector(
        _metadata("one.rule"),
        lambda value: (DetectorMatch(1, 1, "found", value.content.rstrip()),),
    )

    receipt = _runner(detector, max_total_matches=1).run(
        view=_MemoryView(inventory, {"a.py": "alpha\n", "b.py": "beta\n"}),
        inventory=inventory,
    )

    assert [(value.relative_path, value.rule_id) for value in receipt.signals] == [
        ("a.py", "one.rule")
    ]
    assert receipt.files[1].signals == ()
    assert receipt.files[1].failures[0].reason is DetectorFailure.OUTPUT_LIMIT_EXCEEDED


def test_unsupported_file_is_reported_without_detector_invocation() -> None:
    inventory = _inventory(
        _entry(
            "config/app.conf",
            "debug=false\n",
            language="unknown",
            category=SourceClassification.CONFIGURATION,
        )
    )
    view = _MemoryView(inventory, {"config/app.conf": "debug=false\n"})
    python = _Detector(
        _metadata("python.only", languages=("python",)),
        lambda _: (DetectorMatch(1, 1, "unexpected", "unexpected"),),
    )

    receipt = _runner(python).run(view=view, inventory=inventory)

    assert receipt.files[0].status is DetectorFileStatus.UNSUPPORTED
    assert receipt.signals == ()
    assert python.inputs == []
