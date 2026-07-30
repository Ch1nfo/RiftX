"""Agent Engine delegation-event adapter for the parallel Subagent orchestrator."""

from __future__ import annotations

import json

from riftx.application.errors import ApplicationConflictError

from .models import DelegationPacket, SubagentResult
from .orchestrator import SubagentOrchestrator


class ModelDelegationExecutor:
    def __init__(self, orchestrator: SubagentOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def execute(
        self,
        parent_session_id: str,
        requests: list[dict[str, object]],
    ) -> list[SubagentResult]:
        delegations = [
            DelegationPacket.model_validate(_arguments(request)) for request in requests
        ]
        return await self._orchestrator.execute_many(
            parent_session_id=parent_session_id,
            delegations=delegations,
        )


def _arguments(request: dict[str, object]) -> dict[str, object]:
    raw = request.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApplicationConflictError(
                "invalid_subagent_delegation",
                "Subagent delegation arguments are not valid JSON",
            ) from exc
        if isinstance(payload, dict):
            return payload
    if "task" in request:
        return dict(request)
    raise ApplicationConflictError(
        "invalid_subagent_delegation",
        "Subagent delegation requires a structured Delegation Packet",
    )
