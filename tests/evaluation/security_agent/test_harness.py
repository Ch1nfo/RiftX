from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.evaluation.security_agent import (
    BuildDescriptor,
    CapabilitySnapshot,
    EvaluationAdmissionError,
    EvaluationRunContext,
    EvaluationSubject,
    EvaluationSubjectKind,
    EvaluationSubmission,
    EvaluationTrajectory,
    EvidenceReference,
    EvidenceReplay,
    EvidenceReplayCheck,
    FindingDisposition,
    FindingObservation,
    ModelDescriptor,
    ResourceUsage,
    RuntimeDescriptor,
    ScenarioKind,
    ScenarioLoadError,
    ScenarioVisibility,
    SecurityEvaluationHarness,
    SecurityScenarioLoader,
    TrajectoryStep,
    TrajectoryStepKind,
    canonical_json,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks/security_agent"
STARTED_AT = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)


def run_context(
    run_id: str,
    *,
    memory_source_ids: tuple[str, ...] = (),
    usage: ResourceUsage | None = None,
) -> EvaluationRunContext:
    return EvaluationRunContext(
        run_id=run_id,
        subject=EvaluationSubject(
            kind=EvaluationSubjectKind.RIFTX_CONFIGURATION,
            name="riftx-official-only",
            version="357ed38e",
        ),
        build=BuildDescriptor(
            commit="357ed38e",
            dirty=False,
            schema_version="alembic-head",
            platform="test",
            python_version="3.12",
        ),
        model=ModelDescriptor(
            provider="fixture",
            model="deterministic",
            request_mode="recorded",
            configuration_digest="1" * 64,
        ),
        runtime=RuntimeDescriptor(
            control_plane="fixture",
            worker="fixture",
            runner="disabled",
            browser="disabled",
            optional_services=(),
        ),
        capabilities=CapabilitySnapshot(
            tool_versions={"fixture.read": "1.0.0"},
            skill_versions={"fixture.inspect": "1.0.0"},
            pack_versions={"official.security-eval": "1.0.0"},
            selection_digest="2" * 64,
        ),
        requested_memory_source_ids=memory_source_ids,
        started_at=STARTED_AT,
        completed_at=STARTED_AT + timedelta(seconds=1),
        usage=usage
        or ResourceUsage(
            input_tokens=1000,
            output_tokens=500,
            tool_calls=1,
            target_interactions=0,
            duration_ms=1000,
        ),
        trajectory=EvaluationTrajectory(
            steps=(
                TrajectoryStep(
                    sequence=0,
                    kind=TrajectoryStepKind.OBSERVATION,
                    summary="inspected the immutable fixture",
                    tool_id="fixture.read",
                    input_tokens=100,
                    output_tokens=50,
                ),
            )
        ),
    )


def verified_submission(loaded_index: int = 0) -> EvaluationSubmission:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[loaded_index]
    expected = loaded.scenario.expected_findings[0]
    evidence = tuple(
        EvidenceReference(
            evidence_id=f"evidence-{index}",
            kind=kind,
            locator=f"fixture://evidence/{index}",
            digest=f"{index + 3:x}" * 64,
        )
        for index, kind in enumerate(expected.required_evidence)
    )
    replay = EvidenceReplay(
        replay_id="replay-1",
        evidence_ids=tuple(item.evidence_id for item in evidence),
        checks=(
            EvidenceReplayCheck(
                name="fixture-digest",
                passed=True,
                detail="fixture and evidence digests match",
            ),
        ),
        passed=True,
        result_digest="f" * 64,
    )
    return EvaluationSubmission(
        observations=(
            FindingObservation(
                finding_key=expected.finding_key,
                title=expected.title,
                disposition=FindingDisposition.VERIFIED,
                rationale="required evidence is present and replay passed",
                evidence=evidence,
                replay=replay,
            ),
        )
    )


def test_public_code_audit_and_web_scenarios_reset_repeatably() -> None:
    loader = SecurityScenarioLoader(BENCHMARK_ROOT)
    loaded = loader.load_all()

    assert {item.scenario.kind for item in loaded} == {
        ScenarioKind.CODE_AUDIT,
        ScenarioKind.PENETRATION_TEST,
    }
    for item in loaded:
        first = loader.reset(item, reset_at=STARTED_AT)
        second = loader.reset(item, reset_at=STARTED_AT)
        assert canonical_json(first) == canonical_json(second)
        assert first.fixture_digest == item.scenario.target.snapshot_digest


def test_sealed_regression_visibility_uses_the_same_strict_schema(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "security_agent"
    shutil.copytree(BENCHMARK_ROOT, copied_root)
    manifest = copied_root / "code_audit/command_injection/scenario.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "visibility: public_development",
            "visibility: sealed_regression",
        ),
        encoding="utf-8",
    )

    loaded = SecurityScenarioLoader(copied_root).load(manifest)

    assert loaded.scenario.visibility is ScenarioVisibility.SEALED_REGRESSION


def test_run_json_is_stable_and_records_runtime_capability_time_and_tokens() -> None:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[0]
    run = SecurityEvaluationHarness().assemble_run(
        loaded,
        run_context("run-stable"),
        verified_submission(),
    )

    assert canonical_json(run) == canonical_json(run.model_copy(deep=True))
    assert run.judgement.disposition_counts[FindingDisposition.VERIFIED] == 1
    assert run.model.model == "deterministic"
    assert run.runtime.runner == "disabled"
    assert run.capabilities.pack_versions == {"official.security-eval": "1.0.0"}
    assert run.usage.total_tokens == 1500
    assert run.started_at == STARTED_AT


def test_judge_distinguishes_every_finding_disposition() -> None:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[0]
    harness = SecurityEvaluationHarness()
    expected = loaded.scenario.expected_findings[0]

    verified = harness.assemble_run(
        loaded,
        run_context("run-verified"),
        verified_submission(),
    )
    suspected = harness.assemble_run(
        loaded,
        run_context("run-suspected"),
        EvaluationSubmission(
            observations=(
                FindingObservation(
                    finding_key=expected.finding_key,
                    title=expected.title,
                    disposition=FindingDisposition.SUSPECTED,
                    rationale="source looks dangerous but proof is incomplete",
                ),
                FindingObservation(
                    finding_key="unexpected.finding",
                    title="Unexpected candidate",
                    disposition=FindingDisposition.SUSPECTED,
                    rationale="not present in deterministic ground truth",
                ),
            )
        ),
    )
    missing = harness.assemble_run(
        loaded,
        run_context("run-missing"),
        EvaluationSubmission(),
    )

    assert verified.judgement.disposition_counts[FindingDisposition.VERIFIED] == 1
    assert suspected.judgement.disposition_counts[FindingDisposition.SUSPECTED] == 1
    assert suspected.judgement.disposition_counts[FindingDisposition.FALSE_POSITIVE] == 1
    assert missing.judgement.disposition_counts[FindingDisposition.NOT_FOUND] == 1


def test_isolated_runs_reject_undeclared_memory_and_use_unique_namespaces() -> None:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[0]
    harness = SecurityEvaluationHarness()

    with pytest.raises(EvaluationAdmissionError, match="cannot request shared memory"):
        harness.assemble_run(
            loaded,
            run_context("run-denied", memory_source_ids=("operator-memory",)),
            EvaluationSubmission(),
        )

    first = harness.assemble_run(
        loaded,
        run_context("run-one"),
        EvaluationSubmission(),
    )
    second = harness.assemble_run(
        loaded,
        run_context("run-two"),
        EvaluationSubmission(),
    )
    assert first.memory_source_ids == second.memory_source_ids == ()
    assert first.memory_namespace != second.memory_namespace


def test_comparison_is_diagnostic_and_does_not_declare_a_winner() -> None:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[0]
    harness = SecurityEvaluationHarness()
    baseline = harness.assemble_run(
        loaded,
        run_context("run-baseline"),
        EvaluationSubmission(),
    )
    candidate = harness.assemble_run(
        loaded,
        run_context("run-candidate"),
        verified_submission(),
    )

    comparison = harness.compare(baseline, candidate)

    assert comparison.disposition_deltas[FindingDisposition.VERIFIED] == 1
    assert comparison.disposition_deltas[FindingDisposition.NOT_FOUND] == -1
    assert "does not assert overall superiority" in comparison.notes[1]


def test_budget_and_fixture_tampering_fail_closed(tmp_path: Path) -> None:
    loaded = SecurityScenarioLoader(BENCHMARK_ROOT).load_all()[0]
    with pytest.raises(EvaluationAdmissionError, match="token budget exceeded"):
        SecurityEvaluationHarness().assemble_run(
            loaded,
            run_context(
                "run-over-budget",
                usage=ResourceUsage(
                    input_tokens=8001,
                    output_tokens=0,
                    tool_calls=1,
                    target_interactions=0,
                    duration_ms=1000,
                ),
            ),
            EvaluationSubmission(),
        )

    copied_root = tmp_path / "security_agent"
    shutil.copytree(BENCHMARK_ROOT, copied_root)
    copied_loader = SecurityScenarioLoader(copied_root)
    copied = copied_loader.load_all()[0]
    copied.fixture_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ScenarioLoadError, match="changed"):
        copied_loader.reset(copied, reset_at=STARTED_AT)


def test_fixture_symbolic_link_is_rejected(tmp_path: Path) -> None:
    copied_root = tmp_path / "security_agent"
    shutil.copytree(BENCHMARK_ROOT, copied_root)
    fixture = copied_root / "code_audit/command_injection/target.py"
    fixture.unlink()
    try:
        fixture.symlink_to(
            BENCHMARK_ROOT / "code_audit/command_injection/target.py"
        )
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable on this host: {exc}")

    with pytest.raises(ScenarioLoadError, match="symbolic links"):
        SecurityScenarioLoader(copied_root).load(
            copied_root / "code_audit/command_injection/scenario.yaml"
        )


def test_verified_observation_without_replayable_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="require replayable evidence"):
        FindingObservation(
            finding_key="finding",
            title="Finding",
            disposition=FindingDisposition.VERIFIED,
            rationale="unsupported claim",
        )
