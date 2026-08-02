from __future__ import annotations

from pathlib import Path

import pytest

from riftx.evaluation import (
    ReleaseGate,
    ReleaseGateEvaluator,
    ReleaseGateEvidence,
    release_gate_manifest,
)

ROOT = Path(__file__).resolve().parents[2]


def test_release_manifest_covers_all_fifteen_gates_and_existing_tests() -> None:
    manifest = release_gate_manifest()

    assert set(manifest) == set(ReleaseGate)
    for _, selectors in manifest.values():
        assert selectors
        for selector in selectors:
            path, separator, test_name = selector.partition("::")
            assert separator and test_name.startswith("test_")
            source = ROOT / path
            assert source.is_file(), selector
            assert f"def {test_name}" in source.read_text(), selector


def test_release_gate_report_requires_every_gate_to_pass() -> None:
    manifest = release_gate_manifest()
    evidence = [
        ReleaseGateEvidence(
            gate=gate,
            passed=True,
            test_selectors=list(selectors),
            detail=detail,
        )
        for gate, (detail, selectors) in manifest.items()
    ]

    ready = ReleaseGateEvaluator().evaluate(evidence)
    assert ready.ready
    assert len(ready.gates) == 15

    evidence[0].passed = False
    blocked = ReleaseGateEvaluator().evaluate(evidence)
    assert not blocked.ready


def test_release_gate_report_rejects_missing_evidence() -> None:
    with pytest.raises(ValueError, match="missing release gate evidence"):
        ReleaseGateEvaluator().evaluate([])
