"""Validated Subagent Result merging through authoritative Primary reducers."""

from __future__ import annotations

from dataclasses import dataclass

from riftx.application.errors import RepositoryConflictError
from riftx.context import WorkingMemory, WorkingMemoryReducer
from riftx.memory import MemoryWriter, MemoryWriteResult
from riftx.persistence.working_memory_repositories import (
    SQLAlchemyWorkingMemoryRepository,
)

from .models import SubagentResult


@dataclass(frozen=True, slots=True)
class PrimaryMergeResult:
    task_id: str
    working_memory_version: int | None
    memory_results: tuple[MemoryWriteResult, ...] = ()


class PrimaryResultMerger:
    """Apply only structured result proposals; never expose a child Transcript."""

    def __init__(
        self,
        working_memory: SQLAlchemyWorkingMemoryRepository,
        *,
        memory_writer: MemoryWriter | None = None,
        max_conflict_retries: int = 5,
    ) -> None:
        if max_conflict_retries < 1:
            raise ValueError("max_conflict_retries must be positive")
        self._working_memory = working_memory
        self._memory_writer = memory_writer
        self._max_conflict_retries = max_conflict_retries
        self._reducer = WorkingMemoryReducer()

    async def merge(self, run_id: str, result: SubagentResult) -> PrimaryMergeResult:
        version = await self._merge_working_memory(run_id, result)
        memory_results: list[MemoryWriteResult] = []
        if self._memory_writer is not None:
            for candidate in result.memory_candidates:
                memory_results.append(
                    await self._memory_writer.write(candidate, run_id=run_id)
                )
        return PrimaryMergeResult(
            task_id=result.task_id,
            working_memory_version=version,
            memory_results=tuple(memory_results),
        )

    async def _merge_working_memory(
        self,
        run_id: str,
        result: SubagentResult,
    ) -> int | None:
        if not result.confirmed_fact_candidates and not result.hypothesis_updates:
            current = await self._working_memory.get_for_run(run_id)
            return current.version if current is not None else None
        last_conflict: RepositoryConflictError | None = None
        for _ in range(self._max_conflict_retries):
            current = await self._working_memory.get_for_run(run_id)
            if current is None:
                try:
                    current = await self._working_memory.create(WorkingMemory(run_id=run_id))
                except RepositoryConflictError as exc:
                    last_conflict = exc
                    continue
            expected_version = current.version
            reduced = self._reducer.reduce(
                current,
                expected_version=expected_version,
                fact_candidates=result.confirmed_fact_candidates,
                hypothesis_updates=result.hypothesis_updates,
            )
            try:
                saved = await self._working_memory.save(
                    reduced,
                    expected_version=expected_version,
                )
            except RepositoryConflictError as exc:
                last_conflict = exc
                continue
            return saved.version
        raise RepositoryConflictError(
            f"could not merge Subagent task {result.task_id!r} after "
            f"{self._max_conflict_retries} Working Memory conflicts"
        ) from last_conflict
