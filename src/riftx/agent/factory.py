"""Primary Agent construction."""

from __future__ import annotations

from agents import Agent, Model, RunContextWrapper

from .context import RiftXAgentContext
from .result import AgentCycleOutput
from .services import AgentRuntimeServices
from .tools import build_agent_tools


def create_primary_agent(
    services: AgentRuntimeServices,
    *,
    model: str | Model = "primary",
) -> Agent[RiftXAgentContext]:
    return Agent(
        name="RiftX Primary Agent",
        handoff_description="Plans and executes the authorized RiftX run.",
        instructions=_primary_agent_instructions,
        model=model,
        tools=build_agent_tools(services),
        output_type=AgentCycleOutput,
    )


def _primary_agent_instructions(
    wrapper: RunContextWrapper[RiftXAgentContext],
    _: Agent[RiftXAgentContext],
) -> str:
    context = wrapper.context
    success = "\n".join(f"- {item}" for item in context.success_criteria) or "- None supplied"
    entry_points = ", ".join(context.entry_points) or "none"
    scope = ", ".join(context.scope) or "not specified"
    return f"""You are the primary execution Agent for an explicitly authorized RiftX run.

Objective: {context.objective}
Required success criteria:
{success}
Entry points: {entry_points}
Authorized scope: {scope}
Workspace: {context.workspace}

Rules:
- Use only the function tools and registered node tools visible in this run.
- Call list_available_tools before choosing a host tool when availability is uncertain.
- If a tool is missing or fails, inspect the error and re-plan with an available alternative.
- Never claim a command ran unless its tool result confirms execution.
- Stay within the supplied scope and objective.
- Create structured findings only when evidence supports them.
- Do not reveal hidden chain-of-thought. Publish only concise plan summaries and action reasons.
- Request completion only after the objective and required criteria are satisfied.

Return AgentCycleOutput as the final structured response for this cycle.
"""
