"""Application boundary for model-proposed Working Memory mutations."""

from __future__ import annotations

from collections.abc import Sequence

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import RunRepository
from riftx.context.reducer import (
    DuplicateAttemptError,
    PlanRegressionError,
    WorkingMemoryReducer,
    WorkingMemoryReductionError,
    WorkingMemoryVersionConflict,
)
from riftx.context.working_memory import (
    AttemptRecord,
    PlanUpdateProposal,
    WorkingMemory,
    WorkingMemoryRepository,
)
from riftx.tasks import TaskGraphRepository


class WorkingMemoryProposalApplicationService:
    """Validate proposals through the Reducer before durable persistence."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        task_graphs: TaskGraphRepository,
        working_memory: WorkingMemoryRepository,
    ) -> None:
        self._runs = runs
        self._task_graphs = task_graphs
        self._working_memory = working_memory
        self._reducer = WorkingMemoryReducer()

    async def propose_plan_update(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        proposal: PlanUpdateProposal,
    ) -> WorkingMemory:
        if (
            not proposal.item_updates
            and proposal.current_focus is None
            and proposal.next_action is None
        ):
            raise _conflict(
                "working_memory_proposal_empty",
                "Working Memory Plan proposal must contain at least one update",
            )
        if proposal.item_updates and await self._task_graphs.get(run_id) is not None:
            raise _conflict(
                "task_graph_plan_authoritative",
                "Task Graph is authoritative; use Task Graph tools for plan topology changes",
            )
        return await self._reduce_and_save(
            run_id=run_id,
            expected_memory_version=expected_memory_version,
            plan_update=proposal,
        )

    async def record_attempt(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        attempt: AttemptRecord,
    ) -> WorkingMemory:
        return await self._reduce_and_save(
            run_id=run_id,
            expected_memory_version=expected_memory_version,
            attempts=(attempt,),
        )

    async def _reduce_and_save(
        self,
        *,
        run_id: str,
        expected_memory_version: int,
        plan_update: PlanUpdateProposal | None = None,
        attempts: Sequence[AttemptRecord] = (),
    ) -> WorkingMemory:
        if await self._runs.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)
        current = await self._working_memory.get_for_run(run_id)
        if current is None:
            if expected_memory_version != 0:
                raise _version_conflict(expected_memory_version, 0)
            current = WorkingMemory(run_id=run_id)
            try:
                reduced = self._reduce(
                    current,
                    expected_version=current.version,
                    plan_update=plan_update,
                    attempts=attempts,
                )
                await self._working_memory.create(current)
                return await self._working_memory.save(
                    reduced,
                    expected_version=current.version,
                )
            except RepositoryConflictError as exc:
                raise _write_conflict() from exc

        if current.version != expected_memory_version:
            raise _version_conflict(expected_memory_version, current.version)
        reduced = self._reduce(
            current,
            expected_version=expected_memory_version,
            plan_update=plan_update,
            attempts=attempts,
        )
        try:
            return await self._working_memory.save(
                reduced,
                expected_version=expected_memory_version,
            )
        except RepositoryConflictError as exc:
            raise _write_conflict() from exc

    def _reduce(
        self,
        memory: WorkingMemory,
        *,
        expected_version: int,
        plan_update: PlanUpdateProposal | None,
        attempts: Sequence[AttemptRecord],
    ) -> WorkingMemory:
        try:
            return self._reducer.reduce(
                memory,
                expected_version=expected_version,
                plan_update=plan_update,
                attempts=attempts,
            )
        except WorkingMemoryVersionConflict as exc:
            raise _version_conflict(expected_version, memory.version) from exc
        except DuplicateAttemptError as exc:
            raise _conflict("working_memory_duplicate_attempt", str(exc)) from exc
        except PlanRegressionError as exc:
            raise _conflict("working_memory_plan_regression", str(exc)) from exc
        except WorkingMemoryReductionError as exc:
            raise _conflict("working_memory_proposal_rejected", str(exc)) from exc


def _version_conflict(expected: int, actual: int) -> ApplicationConflictError:
    return _conflict(
        "working_memory_version_conflict",
        f"Working Memory version conflict: expected {expected}, found {actual}",
    )


def _write_conflict() -> ApplicationConflictError:
    return _conflict(
        "working_memory_write_conflict",
        "Working Memory changed before the Reducer committed",
    )


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)
