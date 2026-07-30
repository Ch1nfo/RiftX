"""Mappings for durable structured Working Memory state."""

from riftx.context.working_memory import WorkingMemory

from .orm import WorkingMemoryRecord

_STATE_FIELDS = {
    "current_focus",
    "run_plan",
    "confirmed_facts",
    "hypotheses",
    "attempts",
    "user_decisions",
    "pending_questions",
    "active_executions",
    "active_terminals",
    "pending_approvals",
    "next_action",
}


def working_memory_to_record(memory: WorkingMemory) -> WorkingMemoryRecord:
    return WorkingMemoryRecord(
        id=memory.id,
        run_id=memory.run_id,
        version=memory.version,
        state_json=working_memory_state(memory),
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def working_memory_state(memory: WorkingMemory) -> dict[str, object]:
    return memory.model_dump(mode="json", include=_STATE_FIELDS)


def working_memory_from_record(record: WorkingMemoryRecord) -> WorkingMemory:
    return WorkingMemory.model_validate(
        {
            "id": record.id,
            "run_id": record.run_id,
            "version": record.version,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            **record.state_json,
        }
    )
