from __future__ import annotations

import hashlib
import json

from riftx.audit import (
    DetectorFileResult,
    DetectorFileStatus,
    DetectorRuleMetadata,
    DetectorRunReceipt,
    DetectorSignal,
    SourceCaptureDecision,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
    build_audit_reports,
    build_file_inventory,
    normalize_detector_signals,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _inventory():
    entries = []
    for path, decision, reason in (
        ("app.py", SourceCaptureDecision.INCLUDED, SourceCaptureReason.INCLUDED),
        ("vendor/lib.py", SourceCaptureDecision.EXCLUDED, SourceCaptureReason.VENDOR_EXCLUDED),
    ):
        content = "password = secret\n"
        entries.append(
            SourceManifestEntry(
                path=SourceManifestPath.from_bytes(path.encode()),
                object_type=SourceManifestObjectType.REGULAR_FILE,
                origin=SourceManifestOrigin.LOCAL_DIRECTORY,
                mode=0o100644,
                size=len(content),
                sha256=_digest(content) if decision is SourceCaptureDecision.INCLUDED else None,
                git_blob_id=None,
                language="python",
                classification=(
                    SourceClassification.SOURCE
                    if decision is SourceCaptureDecision.INCLUDED
                    else SourceClassification.VENDOR
                ),
                decision=decision,
                reason=reason,
            )
        )
    return build_file_inventory(
        SourceManifest.create(
            source_kind=SourceManifestSourceKind.DIRECTORY,
            commit_sha=None,
            head_commit_sha=None,
            capture_policy_digest=_digest("policy"),
            entries=tuple(entries),
        )
    )


def test_json_and_markdown_reports_are_deterministic_and_complete() -> None:
    inventory = _inventory()
    signal = DetectorSignal(
        rule_id="secret.hardcoded_credential",
        rule_version="1.0.0",
        relative_path="app.py",
        blob_digest=inventory.included_entries()[0].blob_digest or "",
        line=1,
        column=1,
        end_line=1,
        end_column=10,
        message="Credential value is hard-coded",
        evidence='password = "[REDACTED]"',
    )
    findings = normalize_detector_signals((signal,))
    receipt = DetectorRunReceipt(
        registry_digest=_digest("registry"),
        inventory_digest=inventory.inventory_digest,
        limits_digest=_digest("limits"),
        files=(DetectorFileResult("app.py", DetectorFileStatus.COMPLETED, signals=(signal,)),),
        signals=(signal,),
        cancelled=False,
        run_digest=_digest("run"),
    )
    rule = DetectorRuleMetadata(
        rule_id="secret.hardcoded_credential",
        version="1.0.0",
        implementation_digest=_digest("implementation"),
        title="Hard-coded credential",
    )

    first = build_audit_reports(
        inventory=inventory, detector_receipt=receipt, findings=findings, rules=(rule,)
    )
    second = build_audit_reports(
        inventory=inventory,
        detector_receipt=receipt,
        findings=tuple(reversed(findings)),
        rules=(rule,),
    )

    assert first == second
    payload = json.loads(first.json_text)
    assert payload["summary"]["finding_count"] == 1
    assert payload["summary"]["severity_counts"] == {"high": 1}
    assert payload["findings"][0]["path"] == "app.py"
    assert payload["skipped"][0]["reason"] == "vendor_excluded"
    assert payload["rules"][0]["version"] == "1.0.0"
    assert "## Findings" in first.markdown_text
    assert "## Rule Versions" in first.markdown_text
    assert "vendor/lib.py" in first.markdown_text
    assert "[REDACTED]" in first.markdown_text
    assert first.json_text.endswith("\n") and first.markdown_text.endswith("\n")
    assert len(first.report_digest) == 64


def test_report_rejects_cross_inventory_receipt() -> None:
    inventory = _inventory()
    receipt = DetectorRunReceipt(
        registry_digest=_digest("registry"),
        inventory_digest=_digest("other"),
        limits_digest=_digest("limits"),
        files=(),
        signals=(),
        cancelled=False,
        run_digest=_digest("run"),
    )

    import pytest

    with pytest.raises(ValueError, match="different Inventory"):
        build_audit_reports(inventory=inventory, detector_receipt=receipt, findings=(), rules=())
