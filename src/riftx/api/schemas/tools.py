"""Tool registry read schemas."""

import hashlib
import json

from pydantic import BaseModel

from riftx.application.services import ToolRegistryView
from riftx.domain import ApprovalLevel, ExecutorType, ToolState
from riftx.tools import RawToolDefinition, ToolDefinition
from riftx.tools.models import (
    ExecutionPolicy,
    ToolOutputConfig,
    VersionProbe,
)


class ToolUpdateRequest(RawToolDefinition):
    """Complete replacement definition persisted to tools.yaml."""

    def to_definition(self) -> RawToolDefinition:
        return RawToolDefinition.model_validate(self.model_dump())


class RegisteredToolResponse(BaseModel):
    definition: ToolDefinition
    state: ToolState


class ToolDefinitionSummaryResponse(BaseModel):
    """Public Tool definition metadata with environment values removed."""

    id: str
    enabled: bool
    command: list[str]
    executor: ExecutorType
    short_description: str | None
    description: str | None
    capabilities: list[str]
    synonyms: list[str]
    input_schema: dict[str, object] | None
    version_probe: VersionProbe | None
    approval_level: ApprovalLevel
    timeout_seconds: float
    output: ToolOutputConfig
    environment_variables: list[str]

    @classmethod
    def from_definition(cls, definition: ToolDefinition) -> "ToolDefinitionSummaryResponse":
        return cls(
            id=definition.id,
            enabled=definition.enabled,
            command=definition.command,
            executor=definition.executor,
            short_description=definition.short_description,
            description=definition.description,
            capabilities=definition.capabilities,
            synonyms=definition.synonyms,
            input_schema=definition.input_schema,
            version_probe=definition.version_probe,
            approval_level=definition.approval_level,
            timeout_seconds=definition.timeout_seconds,
            output=definition.output,
            environment_variables=sorted(definition.environment),
        )


class RegisteredToolSummaryResponse(BaseModel):
    definition: ToolDefinitionSummaryResponse
    state: ToolState


class ToolRegistrySummaryResponse(BaseModel):
    node_id: str
    generation: int
    source_digest: str
    execution_policy: ExecutionPolicy
    tools: list[RegisteredToolSummaryResponse]

    @classmethod
    def from_view(cls, view: ToolRegistryView) -> "ToolRegistrySummaryResponse":
        tools = [
            RegisteredToolSummaryResponse(
                definition=ToolDefinitionSummaryResponse.from_definition(item.definition),
                state=item.state,
            )
            for item in view.tools
        ]
        return cls(
            node_id=view.node_id,
            generation=view.generation,
            source_digest=_public_source_digest(view.execution_policy, tools),
            execution_policy=view.execution_policy,
            tools=tools,
        )


class ToolRegistryResponse(BaseModel):
    """Administrator-only Tool registry including environment values."""

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


def _public_source_digest(
    execution_policy: ExecutionPolicy,
    tools: list[RegisteredToolSummaryResponse],
) -> str:
    """Digest only public configuration fields, never hidden environment values."""

    content = json.dumps(
        {
            "execution_policy": execution_policy.value,
            "tools": [tool.definition.model_dump(mode="json") for tool in tools],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(content).hexdigest()
