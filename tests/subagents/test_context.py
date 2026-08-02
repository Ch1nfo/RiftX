from __future__ import annotations

from riftx.context import (
    ConfirmedFact,
    ContextCompiler,
    CurrentFocus,
    EvidenceSource,
    PlanItem,
    RunPlan,
    WorkingMemory,
    WorkingMemoryContextSource,
)
from riftx.runtime.lifecycle import ContextCompileRequest, ContextPurpose


class MemoryRepository:
    def __init__(self, memory: WorkingMemory) -> None:
        self.memory = memory

    async def get_for_run(self, run_id: str) -> WorkingMemory | None:
        return self.memory if self.memory.run_id == run_id else None


def fact(fact_id: str, subject: str) -> ConfirmedFact:
    source = f"artifact://{fact_id}"
    return ConfirmedFact(
        id=fact_id,
        run_id="run-1",
        subject=subject,
        predicate="service:443.product",
        value="nginx",
        natural_language=f"{subject}:443 runs nginx",
        confidence=0.99,
        source_refs=[source],
        source_types={source: EvidenceSource.DETERMINISTIC_PARSER},
    )


async def test_subagent_context_contains_only_selected_facts_and_delegation_contract() -> None:
    memory = WorkingMemory(
        run_id="run-1",
        current_focus=CurrentFocus(phase="exploit", objective="Primary secret focus"),
        run_plan=RunPlan(items=[PlanItem(task="Primary private plan", sequence=1)]),
        confirmed_facts=[fact("selected", "10.0.0.1"), fact("unrelated", "10.0.0.2")],
    )
    compiler = ContextCompiler(
        sources=[WorkingMemoryContextSource(MemoryRepository(memory))]
    )

    compiled = await compiler.compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="subagent-1",
            agent_id="subagent:recon",
            purpose=ContextPurpose.SUBAGENT_DELEGATION,
            model_profile="test-model",
            objective="Inspect 10.0.0.1",
            run_contract={
                "run_contract_summary": "Authorized target subset",
                "relevant_scope": ["10.0.0.1"],
            },
            selected_fact_ids=["selected"],
        )
    )

    rendered = str(compiled.input_items)
    assert "10.0.0.1" in rendered
    assert "10.0.0.2" not in rendered
    assert "Primary private plan" not in rendered
    assert "Primary secret focus" not in rendered
    assert "isolated RiftX Subagent" in compiled.system_instructions
    assert "RiftX primary agent" not in compiled.system_instructions
