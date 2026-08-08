import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from riftx.application.errors import EntityNotFoundError
from riftx.application.services import (
    ClosureOutcome,
    ClosureVerifierApplicationService,
    closure_event_id,
    closure_event_payload,
    closure_report_digest,
)
from riftx.domain import Objective, Run, SuccessCriterion
from riftx.evidence import (
    Evidence,
    EvidenceCreatorType,
    EvidenceKind,
    EvidenceRedactionStatus,
    EvidenceReplayMetadata,
    EvidenceReplayStrategy,
    EvidenceScope,
    EvidenceTrustClass,
    SourceLocator,
)
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReproductionContract,
)
from riftx.tasks import Task, TaskEvidenceRequirement, TaskGraph, TaskStatus


def _run(*criteria: SuccessCriterion) -> Run:
    return Run(
        id="run-1",
        kind="general",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Verify Closure"),
        success_criteria=list(criteria),
        workspace_path="/tmp/run-1",
    )


def _evidence(evidence_id: str, *, replayable: bool = True) -> Evidence:
    locator = SourceLocator(uri=f"execution://{evidence_id}/stdout")
    return Evidence(
        id=evidence_id,
        kind=EvidenceKind.EXECUTION_OUTPUT,
        source_uri=locator.source_uri,
        digest="a" * 64,
        run_id="run-1",
        creator_type=EvidenceCreatorType.TOOL,
        created_by="run_shell",
        trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
        scope=EvidenceScope(engagement_id="engagement-1", run_id="run-1"),
        redaction_status=EvidenceRedactionStatus.METADATA_ONLY,
        replay=(
            EvidenceReplayMetadata(
                strategy=EvidenceReplayStrategy.SOURCE_LOOKUP,
                replayable=True,
                expected_digest="a" * 64,
                source_digest="b" * 64,
                parameters_digest="c" * 64,
            )
            if replayable
            else EvidenceReplayMetadata(
                strategy=EvidenceReplayStrategy.NOT_REPLAYABLE,
                replayable=False,
                expected_digest="a" * 64,
                reason="The upstream source is no longer available",
            )
        ),
        locator=locator,
    )


def _confirmed_finding(evidence_id: str) -> ReasoningNode:
    return ReasoningNode(
        id="finding-1",
        run_id="run-1",
        kind=ReasoningNodeKind.FINDING,
        status=ReasoningNodeStatus.CONFIRMED,
        claim="Confirmed authorization bypass",
        evidence_ids=(evidence_id,),
        reproduction_contract=ReproductionContract(
            steps=("Send the authorized replay request",),
            expected_outcome="The protected response is returned",
            target_refs=("https://target.example/protected",),
            parameters_digest="d" * 64,
        ),
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )


def _service(
    *,
    run: Run | None,
    task_graph: TaskGraph | None,
    reasoning_graph: ReasoningGraph | None,
    evidence: list[Evidence],
) -> ClosureVerifierApplicationService:
    return ClosureVerifierApplicationService(
        runs=SimpleNamespace(get=AsyncMock(return_value=run)),
        task_graphs=SimpleNamespace(get=AsyncMock(return_value=task_graph)),
        reasoning_graphs=SimpleNamespace(get=AsyncMock(return_value=reasoning_graph)),
        evidence=SimpleNamespace(list_by_ids=AsyncMock(return_value=tuple(evidence))),
    )


async def test_closure_completes_with_mapped_evidence_explanations_and_replay() -> None:
    run = _run(
        SuccessCriterion(description="Preserve verified evidence"),
        SuccessCriterion(description="Optional follow-up", required=False),
    )
    task_graph = TaskGraph(
        run_id=run.id,
        tasks=[
            Task(
                id="task-1",
                run_id=run.id,
                sequence=1,
                title="Collect evidence",
                stop_condition="No further authorized targets remain",
            ),
            Task(
                id="task-2",
                run_id=run.id,
                sequence=2,
                title="Out-of-scope follow-up",
                status=TaskStatus.BLOCKED,
                blocked_reason="The target is outside the engagement scope",
            ),
        ],
        evidence_requirements=[
            TaskEvidenceRequirement(
                id="requirement-1",
                run_id=run.id,
                task_id="task-1",
                evidence_type="execution_output",
                description="Preserve verified evidence",
                success_criterion_index=0,
                evidence_refs=["criterion-evidence"],
            )
        ],
    )
    reasoning_graph = ReasoningGraph(
        run_id=run.id,
        nodes=[_confirmed_finding("finding-evidence")],
    )
    report = await _service(
        run=run,
        task_graph=task_graph,
        reasoning_graph=reasoning_graph,
        evidence=[_evidence("criterion-evidence"), _evidence("finding-evidence")],
    ).verify(run.id)

    assert report.outcome is ClosureOutcome.COMPLETE
    assert report.reason_codes == ()
    assert report.success_criteria[0].satisfied is True
    assert report.success_criteria[1].reason_codes == ("success_criterion_unmapped",)
    assert [item.explained for item in report.incomplete_tasks] == [True, True]
    assert report.confirmed_findings[0].replayable is True
    assert closure_report_digest(report) == closure_report_digest(report)
    assert closure_event_id(report) == closure_event_id(report)
    payload = closure_event_payload(report)
    assert payload["outcome"] == "complete"
    assert payload["satisfied_success_criterion_count"] == 1
    assert "Preserve verified evidence" not in json.dumps(payload)


async def test_closure_downgrades_unproven_or_unexplained_state_to_partial() -> None:
    run = _run(
        SuccessCriterion(description="Evidence exists"),
        SuccessCriterion(description="Required unmapped criterion"),
    )
    task_graph = TaskGraph(
        run_id=run.id,
        tasks=[
            Task(
                id="task-1",
                run_id=run.id,
                sequence=1,
                title="Unexplained pending work",
            )
        ],
        evidence_requirements=[
            TaskEvidenceRequirement(
                id="requirement-1",
                run_id=run.id,
                task_id="task-1",
                evidence_type="execution_output",
                description="Evidence exists",
                success_criterion_index=0,
                evidence_refs=["missing-evidence"],
            )
        ],
    )
    reasoning_graph = ReasoningGraph(
        run_id=run.id,
        nodes=[_confirmed_finding("non-replayable-evidence")],
    )
    report = await _service(
        run=run,
        task_graph=task_graph,
        reasoning_graph=reasoning_graph,
        evidence=[_evidence("non-replayable-evidence", replayable=False)],
    ).verify(run.id)

    assert report.outcome is ClosureOutcome.PARTIAL
    assert set(report.reason_codes) == {
        "confirmed_finding_evidence_not_replayable",
        "pending_task_explanation_missing",
        "success_criterion_evidence_missing",
        "success_criterion_unmapped",
    }
    assert report.success_criteria[0].satisfied is False
    assert report.incomplete_tasks[0].explained is False
    assert report.confirmed_findings[0].replayable is False


async def test_closure_requires_an_existing_run() -> None:
    service = _service(run=None, task_graph=None, reasoning_graph=None, evidence=[])
    with pytest.raises(EntityNotFoundError):
        await service.verify("missing-run")
