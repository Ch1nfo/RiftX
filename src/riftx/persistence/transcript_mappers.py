"""Mappings for the durable provider-neutral transcript."""

from riftx.domain import AgentMessage, MessageRole, MessageType, MessageVisibility

from .orm import AgentMessageRecord


def agent_message_to_record(message: AgentMessage) -> AgentMessageRecord:
    return AgentMessageRecord(
        id=message.id,
        run_id=message.run_id,
        session_id=message.session_id,
        agent_id=message.agent_id,
        parent_message_id=message.parent_message_id,
        role=message.role.value,
        message_type=message.message_type.value,
        content=message.content,
        structured_content_json=message.structured_content,
        tool_call_id=message.tool_call_id,
        execution_id=message.execution_id,
        artifact_ids_json=message.artifact_ids,
        visibility=message.visibility.value,
        compacted_by_checkpoint_id=message.compacted_by_checkpoint_id,
        token_count=message.token_count,
        sequence=message.sequence,
        created_at=message.created_at,
    )


def agent_message_from_record(record: AgentMessageRecord) -> AgentMessage:
    return AgentMessage(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        parent_message_id=record.parent_message_id,
        role=MessageRole(record.role),
        message_type=MessageType(record.message_type),
        content=record.content,
        structured_content=record.structured_content_json,
        tool_call_id=record.tool_call_id,
        execution_id=record.execution_id,
        artifact_ids=list(record.artifact_ids_json),
        visibility=MessageVisibility(record.visibility),
        compacted_by_checkpoint_id=record.compacted_by_checkpoint_id,
        token_count=record.token_count,
        sequence=record.sequence,
        created_at=record.created_at,
    )
