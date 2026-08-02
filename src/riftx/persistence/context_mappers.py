"""Mappings for durable Context compilation observability."""

from riftx.context import ContextCompilation, ContextManifest

from .orm import ContextCompilationRecord


def context_compilation_to_record(
    compilation: ContextCompilation,
) -> ContextCompilationRecord:
    return ContextCompilationRecord(
        id=compilation.id,
        run_id=compilation.run_id,
        session_id=compilation.session_id,
        agent_id=compilation.agent_id,
        model_profile=compilation.model_profile,
        purpose=compilation.purpose,
        manifest_json=compilation.manifest.model_dump(mode="json"),
        estimated_tokens=compilation.estimated_tokens,
        actual_input_tokens=compilation.actual_input_tokens,
        actual_output_tokens=compilation.actual_output_tokens,
        loaded_memory_ids_json=compilation.loaded_memory_ids,
        checkpoint_id=compilation.checkpoint_id,
        created_at=compilation.created_at,
    )


def context_compilation_from_record(
    record: ContextCompilationRecord,
) -> ContextCompilation:
    return ContextCompilation(
        id=record.id,
        run_id=record.run_id,
        session_id=record.session_id,
        agent_id=record.agent_id,
        model_profile=record.model_profile,
        purpose=record.purpose,
        manifest=ContextManifest.model_validate(record.manifest_json),
        estimated_tokens=record.estimated_tokens,
        actual_input_tokens=record.actual_input_tokens,
        actual_output_tokens=record.actual_output_tokens,
        loaded_memory_ids=record.loaded_memory_ids_json,
        checkpoint_id=record.checkpoint_id,
        created_at=record.created_at,
    )
