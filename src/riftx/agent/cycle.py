"""One durable Primary Agent cycle and interruption checkpoint boundary."""

from __future__ import annotations

from typing import Any

from agents import Model, ModelProvider, RunConfig, Runner, RunState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import EntityNotFoundError
from riftx.models import classify_model_failure

from .checkpoints import SQLAlchemyCheckpointStore
from .context import RiftXAgentContext
from .factory import create_primary_agent
from .result import (
    AgentCycleOutput,
    AgentCycleResult,
    AgentCycleStatus,
    AgentInterruption,
)
from .services import AgentRuntimeServices
from .session import RiftXDatabaseSession

SessionFactory = async_sessionmaker[AsyncSession]


class AgentCycle:
    def __init__(
        self,
        *,
        services: AgentRuntimeServices,
        session_factory: SessionFactory,
        checkpoint_store: SQLAlchemyCheckpointStore,
        model: str | Model = "primary",
        model_provider: ModelProvider | None = None,
        max_history_items: int | None = 100,
        max_turns: int = 10,
    ) -> None:
        if isinstance(model, str) and model_provider is None:
            raise ValueError("a model_provider is required when model is a profile name")
        self._services = services
        self._session_factory = session_factory
        self._checkpoint_store = checkpoint_store
        self._model = model
        self._model_provider = model_provider
        self._max_history_items = max_history_items
        self._max_turns = max_turns

    async def run(
        self,
        context: RiftXAgentContext,
        *,
        input_text: str | None = None,
        checkpoint_id: str | None = None,
        approval_decisions: dict[str, bool] | None = None,
    ) -> AgentCycleResult:
        agent = create_primary_agent(
            self._services,
            model=context.model_profile or self._model,
        )
        session = RiftXDatabaseSession(
            context.run_id,
            self._session_factory,
            max_history_items=self._max_history_items,
        )
        await self._services.event_repository.append(
            context.run_id,
            "agent.cycle_started",
            {
                "agent_step_id": context.agent_step_id,
                "checkpoint_id": checkpoint_id,
            },
        )

        resumed_checkpoint = None
        try:
            if checkpoint_id is not None:
                resumed_checkpoint = await self._checkpoint_store.get(checkpoint_id)
                if resumed_checkpoint is None:
                    raise EntityNotFoundError("AgentCheckpoint", checkpoint_id)
                state = await RunState.from_json(
                    agent,
                    resumed_checkpoint.sdk_state,
                    context_override=context,
                    strict_context=True,
                )
                _apply_approval_decisions(state, approval_decisions)
                runner_input: str | RunState[Any] = state
                runner_context = None
            else:
                runner_input = input_text or _default_cycle_input(context)
                runner_context = context

            if self._model_provider is None:
                run_config = RunConfig(
                    tracing_disabled=True,
                    workflow_name="RiftX Agent Cycle",
                )
            else:
                run_config = RunConfig(
                    model_provider=self._model_provider,
                    tracing_disabled=True,
                    workflow_name="RiftX Agent Cycle",
                )
            result = await Runner.run(
                agent,
                runner_input,
                context=runner_context,
                session=session,
                max_turns=self._max_turns,
                run_config=run_config,
            )
        except Exception as exc:
            failure = classify_model_failure(exc)
            await self._services.event_repository.append(
                context.run_id,
                "agent.cycle_failed",
                {
                    "agent_step_id": context.agent_step_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:1000],
                    "category": failure.category.value,
                    "retryable": failure.retryable,
                },
            )
            raise

        if result.interruptions:
            state_json = result.to_state().to_json(
                context_serializer=lambda value: value.model_dump(mode="json"),
                strict_context=True,
            )
            checkpoint = await self._checkpoint_store.save(context.run_id, state_json)
            if resumed_checkpoint is not None:
                await self._checkpoint_store.resolve(resumed_checkpoint.id, "superseded")
            interruptions = [
                AgentInterruption(
                    call_id=item.call_id or "",
                    tool_name=item.qualified_name or item.name or "unknown",
                    arguments=item.arguments,
                )
                for item in result.interruptions
            ]
            await self._services.event_repository.append(
                context.run_id,
                "agent.cycle_interrupted",
                {
                    "agent_step_id": context.agent_step_id,
                    "checkpoint_id": checkpoint.id,
                    "tool_calls": [item.model_dump(mode="json") for item in interruptions],
                },
            )
            return AgentCycleResult(
                status=AgentCycleStatus.INTERRUPTED,
                checkpoint_id=checkpoint.id,
                interruptions=interruptions,
            )

        if resumed_checkpoint is not None:
            await self._checkpoint_store.resolve(resumed_checkpoint.id)

        output = AgentCycleOutput.model_validate(result.final_output)
        runtime_context = result.context_wrapper.context
        if runtime_context.plan_summary and not output.plan_summary:
            output.plan_summary = runtime_context.plan_summary
        if runtime_context.run_summary and output.run_summary is None:
            output.run_summary = runtime_context.run_summary
        if runtime_context.completion_requested:
            output.completed = True

        await self._services.event_repository.append(
            context.run_id,
            "agent.message",
            {
                "agent_step_id": context.agent_step_id,
                "message": output.assistant_message,
            },
        )
        await self._services.event_repository.append(
            context.run_id,
            "agent.plan_updated",
            {
                "agent_step_id": context.agent_step_id,
                "plan_summary": output.plan_summary,
            },
        )
        await self._services.event_repository.append(
            context.run_id,
            "agent.cycle_completed",
            {
                "agent_step_id": context.agent_step_id,
                "completed": output.completed,
                "needs_input": output.needs_input,
                "run_summary": output.run_summary,
            },
        )
        return AgentCycleResult(status=AgentCycleStatus.COMPLETED, output=output)


def _apply_approval_decisions(
    state: RunState[Any],
    decisions: dict[str, bool] | None,
) -> None:
    interruptions = state.get_interruptions()
    if not interruptions:
        return
    if decisions is None:
        raise ValueError("approval_decisions are required to resume an interrupted cycle")
    unresolved = [item.call_id for item in interruptions if item.call_id not in decisions]
    if unresolved:
        raise ValueError(f"missing approval decisions for tool calls: {unresolved}")
    for item in interruptions:
        if decisions[item.call_id or ""]:
            state.approve(item)
        else:
            state.reject(item, rejection_message="Rejected by RiftX approval decision.")


def _default_cycle_input(context: RiftXAgentContext) -> str:
    return (
        "Continue the authorized run. Review the objective and current available tools, "
        "take the next useful action, and return a concise structured cycle result. "
        f"Agent step: {context.agent_step_id}."
    )
