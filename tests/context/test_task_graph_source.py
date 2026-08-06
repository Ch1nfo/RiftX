from riftx.context import (
    ConfirmedFact,
    EvidenceSource,
    Hypothesis,
    TaskGraphContextSource,
    WorkingMemoryContextSource,
)
from riftx.context.items import ContextItemKind
from riftx.context.working_memory import PlanItem, RunPlan, WorkingMemory
from riftx.reasoning import (
    ReasoningCreatorType,
    ReasoningEdge,
    ReasoningGraph,
    ReasoningNode,
    ReasoningNodeKind,
    ReasoningNodeStatus,
    ReasoningRelationType,
)
from riftx.runtime.lifecycle import ContextCompileRequest, ContextPurpose
from riftx.tasks import Task, TaskDependency, TaskGraph


class TaskGraphs:
    def __init__(self, graph: TaskGraph | None) -> None:
        self.graph = graph

    async def get(self, run_id: str) -> TaskGraph | None:
        assert run_id == "run-1"
        return self.graph


class WorkingMemories:
    def __init__(self, memory: WorkingMemory) -> None:
        self.memory = memory

    async def get_for_run(self, run_id: str) -> WorkingMemory | None:
        assert run_id == "run-1"
        return self.memory


class ReasoningGraphs:
    def __init__(self, graph: ReasoningGraph | None) -> None:
        self.graph = graph

    async def get(self, run_id: str) -> ReasoningGraph | None:
        assert run_id == "run-1"
        return self.graph


def legacy_memory() -> WorkingMemory:
    source_ref = "artifact://legacy"
    return WorkingMemory(
        run_id="run-1",
        confirmed_facts=[
            ConfirmedFact(
                id="legacy-fact",
                run_id="run-1",
                subject="legacy",
                predicate="is",
                value="old",
                natural_language="LEGACY FACT",
                confidence=1,
                source_refs=[source_ref],
                source_types={source_ref: EvidenceSource.DETERMINISTIC_PARSER},
            )
        ],
        hypotheses=[Hypothesis(id="legacy-hypothesis", statement="LEGACY HYPOTHESIS")],
    )


def reasoning_graph() -> ReasoningGraph:
    candidate = ReasoningNode(
        id="fact-candidate",
        run_id="run-1",
        kind=ReasoningNodeKind.FACT_CANDIDATE,
        status=ReasoningNodeStatus.PROMOTED,
        claim="candidate",
        evidence_ids=("evidence-1",),
        creator_type=ReasoningCreatorType.PARSER,
        created_by="parser",
    )
    confirmed = ReasoningNode(
        id="confirmed-fact",
        run_id="run-1",
        kind=ReasoningNodeKind.CONFIRMED_FACT,
        status=ReasoningNodeStatus.CONFIRMED,
        claim="AUTHORITATIVE FACT",
        evidence_ids=("evidence-1",),
        creator_type=ReasoningCreatorType.REDUCER,
        created_by="reasoning-reducer",
    )
    return ReasoningGraph(
        run_id="run-1",
        version=2,
        nodes=[candidate, confirmed],
        edges=[
            ReasoningEdge(
                id="derived",
                run_id="run-1",
                source_node_id=candidate.id,
                target_node_id=confirmed.id,
                relation_type=ReasoningRelationType.DERIVED_FROM,
                evidence_ids=("evidence-1",),
                creator_type=ReasoningCreatorType.REDUCER,
                created_by="reasoning-reducer",
            )
        ],
    )


def request() -> ContextCompileRequest:
    return ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="test",
    )


async def test_task_graph_is_authoritative_and_suppresses_legacy_run_plan() -> None:
    graph = TaskGraph(
        run_id="run-1",
        version=3,
        tasks=[
            Task(id="task-1", run_id="run-1", sequence=1, title="Discover"),
            Task(id="task-2", run_id="run-1", sequence=2, title="Verify"),
        ],
        dependencies=[
            TaskDependency(
                run_id="run-1",
                task_id="task-2",
                depends_on_task_id="task-1",
            )
        ],
    )
    task_graphs = TaskGraphs(graph)
    memory = WorkingMemory(
        run_id="run-1",
        run_plan=RunPlan(items=[PlanItem(task="Legacy plan", sequence=1)]),
    )

    graph_items = await TaskGraphContextSource(task_graphs).load(request())
    memory_items = await WorkingMemoryContextSource(
        WorkingMemories(memory),  # type: ignore[arg-type]
        task_graphs,
    ).load(request())

    assert len(graph_items) == 1
    assert graph_items[0].kind is ContextItemKind.CURRENT_PLAN
    assert graph_items[0].required is True
    assert graph_items[0].content["version"] == 3
    assert graph_items[0].source_refs == ["task-graph://runs/run-1/versions/3"]
    assert all(item.id != f"{memory.id}:plan" for item in memory_items)


async def test_legacy_run_plan_remains_visible_without_task_graph() -> None:
    memory = WorkingMemory(
        run_id="run-1",
        run_plan=RunPlan(items=[PlanItem(task="Legacy plan", sequence=1)]),
    )
    items = await WorkingMemoryContextSource(
        WorkingMemories(memory),  # type: ignore[arg-type]
        TaskGraphs(None),
    ).load(request())

    assert any(item.id == f"{memory.id}:plan" for item in items)


async def test_subagent_delegation_does_not_receive_the_full_task_graph() -> None:
    graph = TaskGraph(
        run_id="run-1",
        tasks=[Task(id="task-1", run_id="run-1", sequence=1, title="Primary-only plan")],
    )
    delegation_request = request().model_copy(
        update={"purpose": ContextPurpose.SUBAGENT_DELEGATION}
    )

    items = await TaskGraphContextSource(TaskGraphs(graph)).load(delegation_request)

    assert items == []


async def test_reasoning_graph_replaces_legacy_fact_and_hypothesis_context() -> None:
    memory = legacy_memory()
    items = await WorkingMemoryContextSource(
        WorkingMemories(memory),  # type: ignore[arg-type]
        reasoning_graphs=ReasoningGraphs(reasoning_graph()),  # type: ignore[arg-type]
    ).load(request())

    reasoning = next(item for item in items if item.kind is ContextItemKind.REASONING_GRAPH)
    rendered = str(reasoning.content)
    assert "AUTHORITATIVE FACT" in rendered
    assert "LEGACY FACT" not in str([item.content for item in items])
    assert "LEGACY HYPOTHESIS" not in str([item.content for item in items])
    assert reasoning.source_refs == ["reasoning-graph://runs/run-1/versions/2"]


async def test_legacy_fact_context_remains_visible_without_reasoning_graph() -> None:
    memory = legacy_memory()
    items = await WorkingMemoryContextSource(
        WorkingMemories(memory),  # type: ignore[arg-type]
        reasoning_graphs=ReasoningGraphs(None),  # type: ignore[arg-type]
    ).load(request())

    assert any(item.kind is ContextItemKind.CONFIRMED_FACT for item in items)
    assert any(item.kind is ContextItemKind.HYPOTHESIS for item in items)


async def test_subagent_selected_facts_use_reasoning_graph_authority() -> None:
    selected = request().model_copy(
        update={
            "purpose": ContextPurpose.SUBAGENT_DELEGATION,
            "selected_fact_ids": ["confirmed-fact"],
        }
    )
    items = await WorkingMemoryContextSource(
        WorkingMemories(legacy_memory()),  # type: ignore[arg-type]
        reasoning_graphs=ReasoningGraphs(reasoning_graph()),  # type: ignore[arg-type]
    ).load(selected)

    assert len(items) == 1
    assert items[0].kind is ContextItemKind.CONFIRMED_FACT
    assert items[0].required is True
    assert "AUTHORITATIVE FACT" in str(items[0].content)
    assert "LEGACY FACT" not in str(items[0].content)
    assert items[0].source_refs == [
        "reasoning-graph://runs/run-1/versions/2",
        "evidence://evidence-1",
    ]
