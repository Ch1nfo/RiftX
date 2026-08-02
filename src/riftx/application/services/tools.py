"""Application service for node-local tool discovery and refresh."""

from dataclasses import dataclass

from riftx.application.errors import EntityNotFoundError
from riftx.domain import ToolState
from riftx.tools import RawToolDefinition, ToolDefinition, ToolRegistry, ToolSnapshot
from riftx.tools.models import ExecutionPolicy


@dataclass(frozen=True, slots=True)
class RegisteredToolView:
    definition: ToolDefinition
    state: ToolState


@dataclass(frozen=True, slots=True)
class ToolRegistryView:
    node_id: str
    generation: int
    source_digest: str
    execution_policy: ExecutionPolicy
    tools: list[RegisteredToolView]


class ToolApplicationService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def node_id(self) -> str:
        return self._registry.node_id

    async def list_tools(self, node_id: str) -> ToolRegistryView:
        self._require_node(node_id)
        try:
            snapshot = self._registry.snapshot
        except RuntimeError:
            snapshot = await self._registry.refresh()
        return self._view(snapshot)

    async def refresh_tools(self, node_id: str) -> ToolRegistryView:
        self._require_node(node_id)
        return self._view(await self._registry.refresh())

    async def update_tool(
        self,
        node_id: str,
        tool_id: str,
        definition: RawToolDefinition,
    ) -> ToolRegistryView:
        self._require_node(node_id)
        return self._view(await self._registry.update_tool(tool_id, definition))

    def _require_node(self, node_id: str) -> None:
        if node_id != self._registry.node_id:
            raise EntityNotFoundError("Node", node_id)

    def _view(self, snapshot: ToolSnapshot) -> ToolRegistryView:
        return ToolRegistryView(
            node_id=snapshot.node_id,
            generation=snapshot.generation,
            source_digest=snapshot.source_digest,
            execution_policy=self._registry.config.execution_policy,
            tools=[
                RegisteredToolView(
                    definition=definition,
                    state=snapshot.states[tool_id],
                )
                for tool_id, definition in sorted(snapshot.definitions.items())
            ],
        )
