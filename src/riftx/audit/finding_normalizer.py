"""Normalize raw detector Signals into stable local Code Audit Findings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from riftx.domain import Finding, FindingEvidence, FindingSeverity, FindingStatus

from .detectors import DetectorSignal

AUDIT_FINDING_SCHEMA_VERSION = "riftx.audit-finding/v1"
_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"(\s*[:=]\s*[\"'])[^\"'\r\n]+([\"'])"
    ),
)
_MAX_EXCERPT_BYTES = 1024


@dataclass(frozen=True, slots=True)
class NormalizedAuditFinding:
    id: str
    rule_id: str
    rule_version: str
    title: str
    severity: FindingSeverity
    confidence: float
    relative_path: str
    blob_digest: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None
    evidence_excerpt: str = field(repr=False)
    stable_key: str = ""
    schema_version: str = field(default=AUDIT_FINDING_SCHEMA_VERSION, init=False)

    def to_finding(self, *, run_id: str, created_at: datetime) -> Finding:
        location = f"{self.relative_path}:{self.line}:{self.column}"
        return Finding(
            id=self.id,
            run_id=run_id,
            title=self.title,
            severity=self.severity,
            status=FindingStatus.DRAFT,
            affected_assets=[self.relative_path],
            description=(
                f"Rule {self.rule_id}@{self.rule_version}; confidence "
                f"{self.confidence:.2f}; location {location}."
            ),
            evidence=[
                FindingEvidence(
                    description=self.evidence_excerpt,
                    location=location,
                )
            ],
            reproduction_steps=[],
            impact="",
            recommendation="",
            created_at=created_at,
            updated_at=created_at,
        )


def normalize_detector_signals(
    signals: tuple[DetectorSignal, ...],
) -> tuple[NormalizedAuditFinding, ...]:
    """Deduplicate and normalize detector output in canonical Finding order."""

    grouped: dict[str, DetectorSignal] = {}
    for signal in signals:
        if not isinstance(signal, DetectorSignal):
            raise ValueError("normalizer input contains an invalid Signal")
        stable_key = _stable_key(signal)
        existing = grouped.get(stable_key)
        if existing is None or signal.evidence < existing.evidence:
            grouped[stable_key] = signal
    findings = tuple(
        _normalize(signal, stable_key=stable_key) for stable_key, signal in sorted(grouped.items())
    )
    return tuple(
        sorted(
            findings,
            key=lambda value: (
                value.relative_path.encode("utf-8"),
                value.line,
                value.column,
                value.rule_id,
                value.id,
            ),
        )
    )


def _normalize(signal: DetectorSignal, *, stable_key: str) -> NormalizedAuditFinding:
    severity, confidence = _risk(signal)
    return NormalizedAuditFinding(
        id=f"finding-{stable_key}",
        rule_id=signal.rule_id,
        rule_version=signal.rule_version,
        title=signal.message,
        severity=severity,
        confidence=confidence,
        relative_path=signal.relative_path,
        blob_digest=signal.blob_digest,
        line=signal.line,
        column=signal.column,
        end_line=signal.end_line,
        end_column=signal.end_column,
        evidence_excerpt=_redact(signal.evidence),
        stable_key=stable_key,
    )


def _stable_key(signal: DetectorSignal) -> str:
    payload = {
        "blob_digest": signal.blob_digest,
        "column": signal.column,
        "end_column": signal.end_column,
        "end_line": signal.end_line,
        "line": signal.line,
        "message": signal.message,
        "relative_path": signal.relative_path,
        "rule_id": signal.rule_id,
        "rule_version": signal.rule_version,
        "schema_version": AUDIT_FINDING_SCHEMA_VERSION,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(AUDIT_FINDING_SCHEMA_VERSION.encode() + b"\0" + canonical).hexdigest()


def _risk(signal: DetectorSignal) -> tuple[FindingSeverity, float]:
    message = signal.message.lower()
    if signal.rule_id.startswith("secret."):
        return FindingSeverity.HIGH, 0.98
    if "tls certificate verification" in message:
        return FindingSeverity.HIGH, 0.95
    if signal.rule_id.startswith("dependency."):
        return FindingSeverity.MEDIUM, 0.85
    if signal.rule_id.startswith("configuration."):
        return FindingSeverity.MEDIUM, 0.90
    if signal.rule_id.startswith(("python.", "javascript.")):
        if any(value in message for value in ("eval", "shell", "pickle", "dynamic")):
            return FindingSeverity.HIGH, 0.95
        return FindingSeverity.MEDIUM, 0.90
    return FindingSeverity.LOW, 0.75


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            redacted = pattern.sub(r"\1\2[REDACTED]\3", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    redacted = " ".join(redacted.split()) or "[REDACTED]"
    encoded = redacted.encode("utf-8")
    if len(encoded) <= _MAX_EXCERPT_BYTES:
        return redacted
    truncated = encoded[:_MAX_EXCERPT_BYTES]
    while True:
        try:
            return truncated.decode("utf-8").rstrip() + "…"
        except UnicodeDecodeError:
            truncated = truncated[:-1]


__all__ = [
    "AUDIT_FINDING_SCHEMA_VERSION",
    "NormalizedAuditFinding",
    "normalize_detector_signals",
]
