"""Deterministic JSON and Markdown reports for one local Code Audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .detectors import DetectorFileStatus, DetectorRuleMetadata, DetectorRunReceipt
from .finding_normalizer import NormalizedAuditFinding
from .inventory import FileInventory, FileInventoryDecision

AUDIT_REPORT_SCHEMA_VERSION = "riftx.audit-report/v1"


@dataclass(frozen=True, slots=True)
class AuditReportBundle:
    json_text: str = field(repr=False)
    markdown_text: str = field(repr=False)
    json_sha256: str
    markdown_sha256: str
    report_digest: str
    finding_count: int
    schema_version: str = field(default=AUDIT_REPORT_SCHEMA_VERSION, init=False)


def build_audit_reports(
    *,
    inventory: FileInventory,
    detector_receipt: DetectorRunReceipt,
    findings: tuple[NormalizedAuditFinding, ...],
    rules: tuple[DetectorRuleMetadata, ...],
) -> AuditReportBundle:
    if detector_receipt.inventory_digest != inventory.inventory_digest:
        raise ValueError("report inputs use different Inventory identities")
    ordered_findings = tuple(
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
    ordered_rules = tuple(sorted(rules, key=lambda value: value.rule_id))
    if len({value.rule_id for value in ordered_rules}) != len(ordered_rules):
        raise ValueError("report rule identities are not unique")
    payload = _payload(inventory, detector_receipt, ordered_findings, ordered_rules)
    json_text = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    markdown_text = _markdown(payload)
    json_sha256 = hashlib.sha256(json_text.encode()).hexdigest()
    markdown_sha256 = hashlib.sha256(markdown_text.encode()).hexdigest()
    report_digest = hashlib.sha256(
        AUDIT_REPORT_SCHEMA_VERSION.encode()
        + b"\0"
        + json_sha256.encode()
        + b"\0"
        + markdown_sha256.encode()
    ).hexdigest()
    return AuditReportBundle(
        json_text=json_text,
        markdown_text=markdown_text,
        json_sha256=json_sha256,
        markdown_sha256=markdown_sha256,
        report_digest=report_digest,
        finding_count=len(ordered_findings),
    )


def _payload(inventory, receipt, findings, rules):
    severity_counts: dict[str, int] = {}
    for finding in findings:
        severity_counts[finding.severity.value] = severity_counts.get(finding.severity.value, 0) + 1
    skipped = [
        {
            "path": entry.relative_path,
            "decision": entry.decision.value,
            "reason": entry.reason,
        }
        for entry in inventory.entries
        if entry.decision is not FileInventoryDecision.INCLUDED
    ]
    unsupported = [
        {
            "path": value.relative_path,
            "status": value.status.value,
            "reason": value.reason.value if value.reason else None,
        }
        for value in receipt.files
        if value.status is not DetectorFileStatus.COMPLETED
    ]
    unsupported.extend(
        {
            "path": file_result.relative_path,
            "status": "rule_failed",
            "rule_id": failure.rule_id,
            "reason": failure.reason.value,
        }
        for file_result in receipt.files
        for failure in file_result.failures
    )
    return {
        "schema_version": AUDIT_REPORT_SCHEMA_VERSION,
        "inventory_digest": inventory.inventory_digest,
        "detector_run_digest": receipt.run_digest,
        "summary": {
            "finding_count": len(findings),
            "severity_counts": dict(sorted(severity_counts.items())),
            "inventory_files": inventory.statistics.total_files,
            "included_files": inventory.statistics.included_files,
            "excluded_files": inventory.statistics.excluded_files,
            "skipped_files": inventory.statistics.skipped_files,
            "total_known_bytes": inventory.statistics.total_known_bytes,
            "cancelled": receipt.cancelled,
        },
        "findings": [
            {
                "id": value.id,
                "rule_id": value.rule_id,
                "rule_version": value.rule_version,
                "title": value.title,
                "severity": value.severity.value,
                "confidence": value.confidence,
                "path": value.relative_path,
                "blob_digest": value.blob_digest,
                "line": value.line,
                "column": value.column,
                "end_line": value.end_line,
                "end_column": value.end_column,
                "evidence": value.evidence_excerpt,
            }
            for value in findings
        ],
        "skipped": skipped,
        "unsupported_or_failed": unsupported,
        "rules": [value.canonical_payload() for value in rules],
    }


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# RiftX Local Code Audit Report",
        "",
        "## Summary",
        "",
        f"- Findings: {summary['finding_count']}",
        f"- Files inventoried: {summary['inventory_files']}",
        f"- Files included: {summary['included_files']}",
        f"- Files excluded: {summary['excluded_files']}",
        f"- Files skipped: {summary['skipped_files']}",
        f"- Known bytes: {summary['total_known_bytes']}",
        "",
        "## Findings",
        "",
    ]
    findings = payload["findings"]
    assert isinstance(findings, list)
    if not findings:
        lines.append("No security findings were detected.")
    for finding in findings:
        assert isinstance(finding, dict)
        lines.extend(
            [
                f"### [{str(finding['severity']).upper()}] {_escape(str(finding['title']))}",
                "",
                f"- Rule: `{finding['rule_id']}@{finding['rule_version']}`",
                f"- Confidence: {float(finding['confidence']):.2f}",
                f"- Location: `{finding['path']}:{finding['line']}:{finding['column']}`",
                f"- Finding ID: `{finding['id']}`",
                "",
                f"> {_escape(str(finding['evidence']))}",
                "",
            ]
        )
    lines.extend(["## Skipped, Unsupported, or Failed", ""])
    omitted = [*payload["skipped"], *payload["unsupported_or_failed"]]
    if not omitted:
        lines.append("None.")
    for value in omitted:
        assert isinstance(value, dict)
        lines.append(
            f"- `{value.get('path') or '<opaque path>'}` — "
            f"{value.get('reason') or value.get('status')}"
        )
    lines.extend(["", "## Rule Versions", ""])
    for rule in payload["rules"]:
        assert isinstance(rule, dict)
        lines.append(f"- `{rule['rule_id']}@{rule['version']}` — {_escape(str(rule['title']))}")
    return "\n".join(lines).rstrip() + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|")


__all__ = ["AUDIT_REPORT_SCHEMA_VERSION", "AuditReportBundle", "build_audit_reports"]
