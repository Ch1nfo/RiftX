"""Tool registry read schemas."""

from pydantic import BaseModel

from riftx.application.services import ToolRegistryView
from riftx.domain import ToolState
from riftx.tools import RawToolDefinition, ToolDefinition
from riftx.tools.models import ExecutionPolicy


class ToolUpdateRequest(RawToolDefinition):
    """Complete replacement definition persisted to tools.yaml."""

    def to_definition(self) -> RawToolDefinition:
        return RawToolDefinition.model_validate(self.model_dump())


class RegisteredToolResponse(BaseModel):
    definition: ToolDefinition
    state: ToolState


class ToolRegistryResponse(BaseModel):
    node_id: str
    generation: int
    source_digest: str
    execution_policy: ExecutionPolicy
    tools: list[RegisteredToolResponse]

    @classmethod
    def from_view(cls, view: ToolRegistryView) -> "ToolRegistryResponse":
        return cls(
            node_id=view.node_id,
            generation=view.generation,
            source_digest=view.source_digest,
            execution_policy=view.execution_policy,
            tools=[
                RegisteredToolResponse(
                    definition=item.definition,
                    state=item.state,
                )
                for item in view.tools
            ],
        )
