from riftx.context import TaskGraphContextSource, WorkingMemoryContextSource
from riftx.context.items import ContextItemKind
from riftx.context.working_memory import PlanItem, RunPlan, WorkingMemory
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
