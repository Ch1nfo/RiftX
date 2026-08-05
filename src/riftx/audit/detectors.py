"""Bounded deterministic detector registry and local static runner."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Protocol

from .inventory import FileInventory, FileInventoryEntry
from .source_manifest import SourceClassification

DETECTOR_REGISTRY_SCHEMA_VERSION = "riftx.detector-registry/v1"
DETECTOR_RUNNER_SCHEMA_VERSION = "riftx.detector-runner/v1"
_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class DetectorFileStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class DetectorFailure(StrEnum):
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    INPUT_READ_FAILED = "input_read_failed"
    DETECTOR_FAILED = "detector_failed"
    OUTPUT_INVALID = "output_invalid"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"


@dataclass(frozen=True, slots=True)
class DetectorRuleMetadata:
    rule_id: str
    version: str
    implementation_digest: str
    title: str
    supported_languages: tuple[str, ...] = ()
    supported_categories: tuple[SourceClassification, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or _TOKEN_PATTERN.fullmatch(self.rule_id) is None:
            raise ValueError("detector rule_id is invalid")
        if not isinstance(self.version, str) or _VERSION_PATTERN.fullmatch(self.version) is None:
            raise ValueError("detector version is invalid")
        if not isinstance(self.implementation_digest, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.implementation_digest
        ) is None:
            raise ValueError("detector implementation_digest is invalid")
        _bounded_text(self.title, label="detector title", maximum_bytes=512)
        if self.supported_languages != tuple(sorted(set(self.supported_languages))):
            raise ValueError("detector languages must be sorted and unique")
        if any(
            not isinstance(language, str) or _TOKEN_PATTERN.fullmatch(language) is None
            for language in self.supported_languages
        ):
            raise ValueError("detector language is invalid")
        if any(
            not isinstance(category, SourceClassification)
            for category in self.supported_categories
        ):
            raise ValueError("detector category is invalid")
        category_values = tuple(value.value for value in self.supported_categories)
        if category_values != tuple(sorted(set(category_values))):
            raise ValueError("detector categories must be sorted and unique")

    def supports(self, entry: FileInventoryEntry) -> bool:
        return (
            not self.supported_languages or entry.language in self.supported_languages
        ) and (
            not self.supported_categories
            or entry.category in self.supported_categories
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "implementation_digest": self.implementation_digest,
            "rule_id": self.rule_id,
            "supported_categories": [
                value.value for value in self.supported_categories
            ],
            "supported_languages": list(self.supported_languages),
            "title": self.title,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class DetectorInput:
    relative_path: str
    blob_digest: str
    language: str
    category: SourceClassification
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DetectorMatch:
    line: int
    column: int
    message: str
    evidence: str = field(repr=False)
    end_line: int | None = None
    end_column: int | None = None


class StaticDetector(Protocol):
    @property
    def metadata(self) -> DetectorRuleMetadata: ...

    def detect(self, detector_input: DetectorInput) -> Sequence[DetectorMatch]: ...


@dataclass(frozen=True, slots=True)
class DetectorSignal:
    rule_id: str
    rule_version: str
    relative_path: str
    blob_digest: str
    line: int
    column: int
    message: str
    evidence: str = field(repr=False)
    end_line: int | None = None
    end_column: int | None = None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blob_digest": self.blob_digest,
            "column": self.column,
            "end_column": self.end_column,
            "end_line": self.end_line,
            "evidence": self.evidence,
            "line": self.line,
            "message": self.message,
            "relative_path": self.relative_path,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
        }


@dataclass(frozen=True, slots=True)
class DetectorRuleFailure:
    rule_id: str
    reason: DetectorFailure


@dataclass(frozen=True, slots=True)
class DetectorFileResult:
    relative_path: str
    status: DetectorFileStatus
    signals: tuple[DetectorSignal, ...] = ()
    failures: tuple[DetectorRuleFailure, ...] = ()
    reason: DetectorFailure | None = None


@dataclass(frozen=True, slots=True)
class DetectorRunLimits:
    max_file_bytes: int = 5 * 1024 * 1024
    max_text_characters: int = 5 * 1024 * 1024
    max_matches_per_rule_file: int = 256
    max_total_matches: int = 10_000
    max_message_bytes: int = 2048
    max_evidence_bytes: int = 8192

    def __post_init__(self) -> None:
        for value in (
            self.max_file_bytes,
            self.max_text_characters,
            self.max_matches_per_rule_file,
            self.max_total_matches,
            self.max_message_bytes,
            self.max_evidence_bytes,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("detector limit must be a positive integer")

    def canonical_payload(self) -> dict[str, int]:
        return {
            "max_evidence_bytes": self.max_evidence_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_matches_per_rule_file": self.max_matches_per_rule_file,
            "max_message_bytes": self.max_message_bytes,
            "max_text_characters": self.max_text_characters,
            "max_total_matches": self.max_total_matches,
        }


@dataclass(frozen=True, slots=True)
class DetectorRunReceipt:
    registry_digest: str
    inventory_digest: str
    limits_digest: str
    files: tuple[DetectorFileResult, ...]
    signals: tuple[DetectorSignal, ...]
    cancelled: bool
    run_digest: str
    schema_version: str = field(default=DETECTOR_RUNNER_SCHEMA_VERSION, init=False)


class DetectorCancellation:
    """Thread-safe monotonic cancellation flag used as a publication fence."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def _commit_if_active(self, action: Callable[[], None]) -> bool:
        """Run one publication action atomically before or not at all after cancel."""

        with self._lock:
            if self._cancelled:
                return False
            action()
            return True


class DetectorRegistry:
    """Immutable, deterministically ordered in-process detector registry."""

    def __init__(self, detectors: Sequence[StaticDetector]) -> None:
        values = tuple(detectors)
        if any(
            not isinstance(getattr(value, "metadata", None), DetectorRuleMetadata)
            or not callable(getattr(value, "detect", None))
            for value in values
        ):
            raise ValueError("detector registry entry is invalid")
        ordered = tuple(sorted(values, key=lambda value: value.metadata.rule_id))
        identities = tuple(value.metadata.rule_id for value in ordered)
        if len(identities) != len(set(identities)):
            raise ValueError("detector rule_id must be unique")
        self._detectors = ordered
        self._digest = _domain_digest(
            DETECTOR_REGISTRY_SCHEMA_VERSION,
            {
                "rules": [value.metadata.canonical_payload() for value in ordered],
                "schema_version": DETECTOR_REGISTRY_SCHEMA_VERSION,
            },
        )

    @property
    def registry_digest(self) -> str:
        return self._digest

    def detectors(self) -> tuple[StaticDetector, ...]:
        return self._detectors

    def metadata(self) -> tuple[DetectorRuleMetadata, ...]:
        return tuple(value.metadata for value in self._detectors)


class LocalDetectorRunner:
    """Run trusted built-in Python detectors over bounded Snapshot text reads."""

    def __init__(self, registry: DetectorRegistry, limits: DetectorRunLimits) -> None:
        if not isinstance(registry, DetectorRegistry) or not isinstance(
            limits, DetectorRunLimits
        ):
            raise ValueError("detector runner configuration is invalid")
        self._registry = registry
        self._limits = limits
        self._limits_digest = _domain_digest(
            DETECTOR_RUNNER_SCHEMA_VERSION,
            limits.canonical_payload(),
        )

    def run(
        self,
        *,
        view: object,
        inventory: FileInventory,
        cancellation: DetectorCancellation | None = None,
    ) -> DetectorRunReceipt:
        if not isinstance(inventory, FileInventory) or not all(
            callable(getattr(view, name, None)) for name in ("entries", "read_text")
        ):
            raise ValueError("detector run request is invalid")
        cancellation = cancellation or DetectorCancellation()
        view_entries = {entry.relative_path: entry for entry in view.entries()}
        file_results: list[DetectorFileResult] = []
        all_signals: list[DetectorSignal] = []
        for entry in inventory.included_entries():
            if cancellation.cancelled:
                break
            result = self._run_file(
                view=view,
                view_entries=view_entries,
                entry=entry,
                cancellation=cancellation,
                remaining_matches=self._limits.max_total_matches - len(all_signals),
            )
            if not cancellation._commit_if_active(
                lambda result=result: _commit_file_result(
                    result,
                    file_results,
                    all_signals,
                )
            ):
                if result.status is DetectorFileStatus.CANCELLED:
                    file_results.append(result)
                break
        signals = tuple(sorted(all_signals, key=_signal_key))
        files = tuple(file_results)
        cancelled = cancellation.cancelled
        payload = {
            "cancelled": cancelled,
            "files": [_file_payload(value) for value in files],
            "inventory_digest": inventory.inventory_digest,
            "limits_digest": self._limits_digest,
            "registry_digest": self._registry.registry_digest,
            "schema_version": DETECTOR_RUNNER_SCHEMA_VERSION,
            "signals": [value.canonical_payload() for value in signals],
        }
        return DetectorRunReceipt(
            registry_digest=self._registry.registry_digest,
            inventory_digest=inventory.inventory_digest,
            limits_digest=self._limits_digest,
            files=files,
            signals=signals,
            cancelled=cancelled,
            run_digest=_domain_digest(DETECTOR_RUNNER_SCHEMA_VERSION, payload),
        )

    def _run_file(
        self,
        *,
        view: object,
        view_entries: dict[str, object],
        entry: FileInventoryEntry,
        cancellation: DetectorCancellation,
        remaining_matches: int,
    ) -> DetectorFileResult:
        assert entry.relative_path is not None and entry.blob_digest is not None
        path = entry.relative_path
        metadata = view_entries.get(path)
        if (
            metadata is None
            or metadata.content_digest != entry.blob_digest
            or metadata.size != entry.size
        ):
            return DetectorFileResult(
                relative_path=path,
                status=DetectorFileStatus.FAILED,
                reason=DetectorFailure.SNAPSHOT_MISMATCH,
            )
        if entry.size is None or entry.size > self._limits.max_file_bytes:
            return DetectorFileResult(
                relative_path=path,
                status=DetectorFileStatus.FAILED,
                reason=DetectorFailure.INPUT_LIMIT_EXCEEDED,
            )
        try:
            content = view.read_text(
                path,
                max_bytes=self._limits.max_file_bytes,
                max_characters=self._limits.max_text_characters,
            )
        except Exception:
            return DetectorFileResult(
                relative_path=path,
                status=DetectorFileStatus.FAILED,
                reason=DetectorFailure.INPUT_READ_FAILED,
            )
        if cancellation.cancelled:
            return DetectorFileResult(path, DetectorFileStatus.CANCELLED)
        applicable = tuple(
            detector
            for detector in self._registry.detectors()
            if detector.metadata.supports(entry)
        )
        if not applicable:
            return DetectorFileResult(path, DetectorFileStatus.UNSUPPORTED)
        signals: list[DetectorSignal] = []
        failures: list[DetectorRuleFailure] = []
        for detector in applicable:
            if cancellation.cancelled:
                return DetectorFileResult(path, DetectorFileStatus.CANCELLED)
            try:
                matches = tuple(
                    detector.detect(
                        DetectorInput(
                            relative_path=path,
                            blob_digest=entry.blob_digest,
                            language=entry.language,
                            category=entry.category,
                            content=content,
                        )
                    )
                )
            except Exception:
                failures.append(
                    DetectorRuleFailure(
                        detector.metadata.rule_id,
                        DetectorFailure.DETECTOR_FAILED,
                    )
                )
                continue
            if cancellation.cancelled:
                return DetectorFileResult(path, DetectorFileStatus.CANCELLED)
            reason = self._validate_matches(
                matches,
                content=content,
                remaining_matches=remaining_matches - len(signals),
            )
            if reason is not None:
                failures.append(DetectorRuleFailure(detector.metadata.rule_id, reason))
                continue
            signals.extend(
                DetectorSignal(
                    rule_id=detector.metadata.rule_id,
                    rule_version=detector.metadata.version,
                    relative_path=path,
                    blob_digest=entry.blob_digest,
                    line=match.line,
                    column=match.column,
                    end_line=match.end_line,
                    end_column=match.end_column,
                    message=match.message,
                    evidence=match.evidence,
                )
                for match in matches
            )
        return DetectorFileResult(
            relative_path=path,
            status=DetectorFileStatus.COMPLETED,
            signals=tuple(sorted(signals, key=_signal_key)),
            failures=tuple(failures),
        )

    def _validate_matches(
        self,
        matches: tuple[object, ...],
        *,
        content: str,
        remaining_matches: int,
    ) -> DetectorFailure | None:
        if len(matches) > self._limits.max_matches_per_rule_file or len(
            matches
        ) > remaining_matches:
            return DetectorFailure.OUTPUT_LIMIT_EXCEEDED
        lines = content.split("\n")
        line_count = len(lines)
        for match in matches:
            if not isinstance(match, DetectorMatch):
                return DetectorFailure.OUTPUT_INVALID
            if (
                not 1 <= match.line <= line_count
                or match.column < 1
                or (match.end_line is None) != (match.end_column is None)
                or (match.end_line is not None and match.end_line < match.line)
                or (match.end_line is not None and match.end_line > line_count)
                or (match.end_column is not None and match.end_column < 1)
                or match.column > len(lines[match.line - 1]) + 1
                or (
                    match.end_line is not None
                    and match.end_column is not None
                    and match.end_column > len(lines[match.end_line - 1]) + 1
                )
            ):
                return DetectorFailure.OUTPUT_INVALID
            try:
                _bounded_text(
                    match.message,
                    label="detector message",
                    maximum_bytes=self._limits.max_message_bytes,
                )
                _bounded_text(
                    match.evidence,
                    label="detector evidence",
                    maximum_bytes=self._limits.max_evidence_bytes,
                )
            except ValueError:
                return DetectorFailure.OUTPUT_INVALID
        return None


def _signal_key(value: DetectorSignal) -> tuple[object, ...]:
    return (
        value.relative_path.encode("utf-8"),
        value.line,
        value.column,
        value.end_line or value.line,
        value.end_column or value.column,
        value.rule_id,
        value.message,
        value.evidence,
    )


def _commit_file_result(
    result: DetectorFileResult,
    files: list[DetectorFileResult],
    signals: list[DetectorSignal],
) -> None:
    files.append(result)
    signals.extend(result.signals)


def _file_payload(value: DetectorFileResult) -> dict[str, object]:
    return {
        "failures": [
            {"reason": item.reason.value, "rule_id": item.rule_id}
            for item in value.failures
        ],
        "reason": value.reason.value if value.reason is not None else None,
        "relative_path": value.relative_path,
        "signals": [item.canonical_payload() for item in value.signals],
        "status": value.status.value,
    }


def _bounded_text(value: object, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} is invalid")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its byte limit")
    return value


def _domain_digest(domain: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


__all__ = [
    "DETECTOR_REGISTRY_SCHEMA_VERSION",
    "DETECTOR_RUNNER_SCHEMA_VERSION",
    "DetectorCancellation",
    "DetectorFailure",
    "DetectorFileResult",
    "DetectorFileStatus",
    "DetectorInput",
    "DetectorMatch",
    "DetectorRegistry",
    "DetectorRuleFailure",
    "DetectorRuleMetadata",
    "DetectorRunLimits",
    "DetectorRunReceipt",
    "DetectorSignal",
    "LocalDetectorRunner",
    "StaticDetector",
]
