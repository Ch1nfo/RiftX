"""Built-in Context Sources for durable Working Memory and recent transcript state."""

from __future__ import annotations

from typing import Protocol

from riftx.domain import AgentMessage, MessageRole, MessageType
from riftx.runtime.lifecycle import ContextCompileRequest, ContextPurpose

from .items import ContextItem, ContextItemKind, ContextLayer
from .models import ProcessedToolResult
from .working_memory import AttemptStatus, WorkingMemoryRepository


class TranscriptReader(Protocol):
    async def list_by_session(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[AgentMessage]: ...


class WorkingMemoryContextSource:
    """Expose authoritative Working Memory as independently budgetable state."""

    def __init__(self, repository: WorkingMemoryRepository) -> None:
        self._repository = repository

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        memory = await self._repository.get_for_run(request.run_id)
        if memory is None:
            return []
        ref = f"working-memory://{memory.id}/versions/{memory.version}"
        if request.purpose is ContextPurpose.SUBAGENT_DELEGATION:
            selected = set(request.selected_fact_ids)
            facts = [fact for fact in memory.confirmed_facts if fact.id in selected]
            if not facts:
                return []
            return [
                _memory_item(
                    memory.id,
                    "selected-facts",
                    [fact.model_dump(mode="json") for fact in facts],
                    ref,
                    kind=ContextItemKind.CONFIRMED_FACT,
                    priority=100,
                    required=True,
                    compressible=False,
                )
            ]
        items: list[ContextItem] = []
        if memory.current_focus is not None:
            items.append(
                _memory_item(
                    memory.id,
                    "focus",
                    memory.current_focus.model_dump(mode="json"),
                    ref,
                    kind=ContextItemKind.CURRENT_FOCUS,
                    priority=92,
                )
            )
        items.append(
            _memory_item(
                memory.id,
                "plan",
                memory.run_plan.model_dump(mode="json"),
                ref,
                kind=ContextItemKind.CURRENT_PLAN,
                priority=100,
                required=True,
                compressible=False,
            )
        )
        if memory.confirmed_facts:
            items.append(
                _memory_item(
                    memory.id,
                    "facts",
                    [fact.model_dump(mode="json") for fact in memory.confirmed_facts],
                    ref,
                    kind=ContextItemKind.CONFIRMED_FACT,
                    priority=94,
                )
            )
        if memory.hypotheses:
            items.append(
                _memory_item(
                    memory.id,
                    "hypotheses",
                    [item.model_dump(mode="json") for item in memory.hypotheses],
                    ref,
                    kind=ContextItemKind.HYPOTHESIS,
                    priority=93,
                )
            )
        failed_attempts = [
            attempt.model_dump(mode="json")
            for attempt in memory.attempts
            if attempt.result_status is AttemptStatus.FAILED
        ]
        if failed_attempts:
            items.append(
                _memory_item(
                    memory.id,
                    "failed-attempts",
                    failed_attempts,
                    ref,
                    kind=ContextItemKind.FAILED_ATTEMPT,
                    priority=100,
                    required=True,
                    compressible=False,
                )
            )
        if memory.pending_questions:
            items.append(
                _memory_item(
                    memory.id,
                    "pending-questions",
                    [item.model_dump(mode="json") for item in memory.pending_questions],
                    ref,
                    priority=90,
                )
            )
        if memory.user_decisions:
            items.append(
                _memory_item(
                    memory.id,
                    "user-decisions",
                    [item.model_dump(mode="json") for item in memory.user_decisions],
                    ref,
                    priority=96,
                )
            )
        if memory.pending_approvals:
            items.append(
                _memory_item(
                    memory.id,
                    "pending-approvals",
                    memory.pending_approvals,
                    ref,
                    kind=ContextItemKind.PENDING_APPROVAL,
                    priority=100,
                    required=True,
                    compressible=False,
                )
            )
        if memory.active_executions:
            items.append(
                _memory_item(
                    memory.id,
                    "active-executions",
                    [item.model_dump(mode="json") for item in memory.active_executions],
                    ref,
                    kind=ContextItemKind.ACTIVE_EXECUTION,
                    priority=100,
                    required=True,
                    compressible=False,
                )
            )
        if memory.active_terminals:
            items.append(
                _memory_item(
                    memory.id,
                    "active-terminals",
                    [item.model_dump(mode="json") for item in memory.active_terminals],
                    ref,
                    kind=ContextItemKind.ACTIVE_TERMINAL,
                    priority=100,
                    required=True,
                    compressible=False,
                )
            )
        if memory.next_action is not None:
            items.append(
                _memory_item(
                    memory.id,
                    "next-action",
                    memory.next_action.model_dump(mode="json"),
                    ref,
                    priority=96,
                )
            )
        return items


class TranscriptContextSource:
    """Load recent durable messages while allowing old low-value content to be evicted first."""

    def __init__(self, repository: TranscriptReader, *, max_items: int = 100) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._repository = repository
        self._max_items = max_items

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        messages = await self._repository.list_by_session(
            request.session_id,
            limit=self._max_items,
        )
        return [
            self._message_item(request, message)
            for message in messages
            if message.compacted_by_checkpoint_id is None
        ]

    def _message_item(
        self,
        request: ContextCompileRequest,
        message: AgentMessage,
    ) -> ContextItem:
        required = message.id == request.latest_user_message_id or (
            message.message_type is MessageType.APPROVAL
        )
        layer = ContextLayer.RECENT_CONVERSATION
        kind = ContextItemKind.GENERAL
        if message.message_type is MessageType.TOOL_RESULT_REFERENCE:
            layer = ContextLayer.RELEVANT_TOOL_RESULTS
            kind = ContextItemKind.TOOL_PREVIEW
        elif message.message_type is MessageType.SUBAGENT_RESULT:
            layer = ContextLayer.SUBAGENT_RESULTS
            kind = ContextItemKind.SUBAGENT_RESULT
        elif message.role is MessageRole.ASSISTANT:
            kind = ContextItemKind.ASSISTANT_DETAIL
        elif message.role is MessageRole.USER and not required:
            kind = ContextItemKind.CHITCHAT
        content: object
        if message.structured_content is not None:
            content = message.structured_content
        else:
            content = {
                "role": message.role.value,
                "content": message.content or "",
                "message_type": message.message_type.value,
            }
        return ContextItem(
            id=message.id,
            layer=layer,
            kind=kind,
            content=content,
            priority=100 if required else 60,
            required=required,
            compressible=not required,
            removable=not required,
            source_refs=[f"message://{message.id}"],
            sequence=message.sequence,
        )


def processed_tool_result_context_item(
    result: ProcessedToolResult,
    *,
    sequence: int = 0,
) -> ContextItem:
    """Expose only the bounded summary and logical Artifact refs to the model."""

    artifact_refs = [reference.uri for reference in result.raw_artifacts]
    return ContextItem(
        id=f"tool-result:{result.execution_id}",
        layer=ContextLayer.RELEVANT_TOOL_RESULTS,
        kind=ContextItemKind.TOOL_PREVIEW,
        content={
            "execution_id": result.execution_id,
            "tool_id": result.tool_id,
            "status": result.status.value,
            "exit_code": result.exit_code,
            "context_summary": result.context_summary,
            "artifact_refs": artifact_refs,
        },
        priority=75,
        compressible=True,
        removable=True,
        source_refs=artifact_refs or [f"execution://{result.execution_id}"],
        sequence=sequence,
        metadata={"execution_id": result.execution_id},
    )


def _memory_item(
    memory_id: str,
    suffix: str,
    content: object,
    source_ref: str,
    *,
    kind: ContextItemKind = ContextItemKind.GENERAL,
    priority: int,
    required: bool = False,
    compressible: bool = True,
) -> ContextItem:
    return ContextItem(
        id=f"{memory_id}:{suffix}",
        layer=ContextLayer.WORKING_MEMORY,
        kind=kind,
        content=content,
        priority=priority,
        required=required,
        compressible=compressible,
        removable=not required,
        source_refs=[source_ref],
        metadata={"working_memory_id": memory_id},
    )
