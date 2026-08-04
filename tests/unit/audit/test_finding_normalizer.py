from __future__ import annotations

from datetime import UTC, datetime

from riftx.audit import DetectorSignal, normalize_detector_signals
from riftx.domain import FindingSeverity, FindingStatus

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def _signal(**updates: object) -> DetectorSignal:
    values: dict[str, object] = {
        "rule_id": "secret.hardcoded_credential",
        "rule_version": "1.0.0",
        "relative_path": "src/app.py",
        "blob_digest": "a" * 64,
        "line": 3,
        "column": 5,
        "end_line": 3,
        "end_column": 20,
        "message": "Credential value is hard-coded",
        "evidence": 'password = "AKIA1234567890ABCDEF"',
    }
    values.update(updates)
    return DetectorSignal(**values)  # type: ignore[arg-type]


def test_normalizer_deduplicates_with_stable_identity_and_order() -> None:
    duplicate = _signal(evidence='password = "different-secret-value"')
    javascript = _signal(
        rule_id="javascript.dangerous_api",
        relative_path="a.js",
        blob_digest="b" * 64,
        line=1,
        column=1,
        end_column=5,
        message="eval executes dynamically constructed code",
        evidence="eval(input)",
    )

    first = normalize_detector_signals((_signal(), duplicate, javascript))
    second = normalize_detector_signals((javascript, duplicate, _signal()))

    assert first == second
    assert len(first) == 2
    assert [value.relative_path for value in first] == ["a.js", "src/app.py"]
    assert all(value.id == f"finding-{value.stable_key}" for value in first)
    assert first[0].severity is FindingSeverity.HIGH
    assert first[0].confidence == 0.95
    assert first[1].severity is FindingSeverity.HIGH
    assert first[1].confidence == 0.98


def test_evidence_is_redacted_bounded_and_not_exposed_by_repr() -> None:
    long_secret = "x" * 2000
    findings = normalize_detector_signals(
        (
            _signal(
                evidence=(f'password = "super-secret-password" AKIA1234567890ABCDEF {long_secret}')
            ),
        )
    )
    finding = findings[0]

    assert "super-secret-password" not in finding.evidence_excerpt
    assert "AKIA1234567890ABCDEF" not in finding.evidence_excerpt
    assert "[REDACTED]" in finding.evidence_excerpt
    assert len(finding.evidence_excerpt.encode("utf-8")) <= 1027
    assert "super-secret-password" not in repr(finding)


def test_normalized_finding_projects_to_existing_durable_finding_without_fix() -> None:
    normalized = normalize_detector_signals((_signal(),))[0]

    finding = normalized.to_finding(run_id="run-1", created_at=NOW)

    assert finding.id == normalized.id
    assert finding.run_id == "run-1"
    assert finding.status is FindingStatus.DRAFT
    assert finding.severity is FindingSeverity.HIGH
    assert finding.affected_assets == ["src/app.py"]
    assert finding.evidence[0].location == "src/app.py:3:5"
    assert finding.evidence[0].description == normalized.evidence_excerpt
    assert finding.recommendation == ""
    assert finding.reproduction_steps == []


def test_severity_and_confidence_policy_covers_all_builtin_families() -> None:
    cases = (
        ("dependency.unpinned", "Dependency is not pinned", FindingSeverity.MEDIUM, 0.85),
        ("configuration.insecure_setting", "Debug mode is enabled", FindingSeverity.MEDIUM, 0.90),
        (
            "configuration.insecure_setting",
            "TLS certificate verification is disabled",
            FindingSeverity.HIGH,
            0.95,
        ),
        ("python.dangerous_api", "pickle is used", FindingSeverity.HIGH, 0.95),
        ("javascript.dangerous_api", "DOM injection sink", FindingSeverity.MEDIUM, 0.90),
    )

    for index, (rule_id, message, severity, confidence) in enumerate(cases, 1):
        finding = normalize_detector_signals(
            (
                _signal(
                    rule_id=rule_id,
                    message=message,
                    line=index,
                    end_line=index,
                ),
            )
        )[0]
        assert (finding.severity, finding.confidence) == (severity, confidence)
