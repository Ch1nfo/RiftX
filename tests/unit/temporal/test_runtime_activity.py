from __future__ import annotations

from dataclasses import dataclass, field

from riftx.runtime.lifecycle import RunCycleRequest, RunCycleResult
from riftx.runtime.types import YieldReason
from riftx.temporal.models import RunAgentCycleActivityInput, RuntimeYieldReason
from riftx.temporal.runtime_activity import RuntimeCycleActivities


@dataclass
class FakeCoordinator:
    requests: list[RunCycleRequest] = field(default_factory=list)

    async def run_cycle(self, request: RunCycleRequest) -> RunCycleResult:
        self.requests.append(request)
        return RunCycleResult(
            run_id=request.run_id,
            session_id=request.session_id,
            cycle_id=request.cycle_id or "missing-cycle",
            yield_reason=YieldReason.TOOL_RUNNING,
            model_call_count=1,
            tool_call_count=1,
            provider_state_id="provider-state-1",
            waiting_execution_id="execution-1",
        )


@dataclass
class FakeInitializer:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def ensure_primary_session(self, run_id: str, session_id: str) -> None:
        self.calls.append((run_id, session_id))


@dataclass
class FakeUserInputResolver:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def resolve_user_input(
        self,
        run_id: str,
        session_id: str,
        user_input_id: str,
    ) -> str:
        self.calls.append((run_id, session_id, user_input_id))
        return "transcript-message-1"


@dataclass
class FakeExecutionInputResolver:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def resolve_execution_input(
        self,
        run_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        self.calls.append((run_id, execution_id))
        return {
            "id": f"tool-result:{execution_id}",
            "type": "tool_result",
            "content": {"execution_id": execution_id, "context_summary": "done"},
            "source_refs": [f"artifact://runs/{run_id}/executions/{execution_id}/stdout"],
            "required": True,
        }


async def test_runtime_cycle_activity_maps_only_durable_identifiers() -> None:
    coordinator = FakeCoordinator()
    initializer = FakeInitializer()
    user_input_resolver = FakeUserInputResolver()
    execution_input_resolver = FakeExecutionInputResolver()
    activities = RuntimeCycleActivities(
        coordinator,
        worker_id="worker-local",
        session_initializer=initializer,
        user_input_resolver=user_input_resolver,
        execution_input_resolver=execution_input_resolver,
    )

    result = await activities.run_agent_cycle_activity(
        RunAgentCycleActivityInput(
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            latest_user_message_id="user-input-event-1",
            completed_execution_id="execution-0",
            approval_id="approval-1",
        )
    )

    assert coordinator.requests == [
        RunCycleRequest(
            run_id="run-1",
            session_id="session-1",
            worker_id="worker-local",
            cycle_id="cycle-1",
            latest_user_message_id="transcript-message-1",
            input_items=[
                {
                    "id": "tool-result:execution-0",
                    "type": "tool_result",
                    "content": {
                        "execution_id": "execution-0",
                        "context_summary": "done",
                    },
                    "source_refs": [
                        "artifact://runs/run-1/executions/execution-0/stdout"
                    ],
                    "required": True,
                },
                {
                    "type": "approval_decision",
                    "approval_id": "approval-1",
                    "source_refs": ["approval://approval-1"],
                },
            ],
        )
    ]
    assert result.yield_reason is RuntimeYieldReason.TOOL_RUNNING
    assert result.waiting_object_id == "execution-1"
    assert result.checkpoint_id == "provider-state-1"
    assert initializer.calls == [("run-1", "session-1")]
    assert user_input_resolver.calls == [
        ("run-1", "session-1", "user-input-event-1")
    ]
    assert execution_input_resolver.calls == [("run-1", "execution-0")]
