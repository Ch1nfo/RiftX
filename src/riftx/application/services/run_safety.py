"""Fail-closed physical-stop orchestration for every Run effect family."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from riftx.application.ports import ExecutionRepository
from riftx.domain import Execution, ExecutionStatus
from riftx.runner import ExecutionRunner

_ACTIVE_EXECUTION_STATUSES = {
    ExecutionStatus.CREATED,
    ExecutionStatus.QUEUED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}
_PENDING_STOP_EXECUTION_STATUSES = _ACTIVE_EXECUTION_STATUSES | {
    # FAILED can describe a command/status transport failure while the owning
    # Runner still has a live process. LOST explicitly means that physical
    # termination is unknown. Both therefore require a durable stop ACK.
    ExecutionStatus.FAILED,
    ExecutionStatus.LOST,
}
_TERMINAL_STOP_AUDIT_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}
_EXECUTION_LIST_PAGE_SIZE = 1000
_REQUIRED_RESOURCE_TYPES = ("browser_sessions", "target_http_requests")


class RunResourceStopResult(Protocol):
    """Evidence returned by a bounded stopper for one effect family."""

    @property
    def attempted_ids(self) -> tuple[str, ...]: ...

    @property
    def node_ids(self) -> dict[str, str]: ...

    @property
    def observed_statuses(self) -> dict[str, str]: ...

    @property
    def confirmed_statuses(self) -> dict[str, str]: ...

    @property
    def failures(self) -> dict[str, str]: ...


class RunResourceStopper(Protocol):
    async def stop_run(self, run_id: str) -> RunResourceStopResult: ...


@dataclass(frozen=True, slots=True)
class ResourceStopDisposition:
    attempted_ids: tuple[str, ...]
    node_ids: dict[str, str]
    observed_statuses: dict[str, str]
    confirmed_statuses: dict[str, str]
    failures: dict[str, str]

    @property
    def confirmed_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.confirmed_statuses))

    @property
    def succeeded(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class SafetyStopResult:
    resources: dict[str, ResourceStopDisposition]

    @property
    def failed_resource_types(self) -> tuple[str, ...]:
        return tuple(
            resource_type
            for resource_type, disposition in self.resources.items()
            if not disposition.succeeded
        )

    @property
    def succeeded(self) -> bool:
        return not self.failed_resource_types


@dataclass(frozen=True, slots=True)
class _ExecutionStopResult:
    attempted_ids: tuple[str, ...]
    node_ids: dict[str, str]
    observed_statuses: dict[str, str]
    confirmed_statuses: dict[str, str]
    failures: dict[str, str]


class RunSafetyStopService:
    """Stop Executions, Browsers, and Target HTTP with affirmative evidence.

    Callers must establish a durable Run admission fence before invoking this
    service. Missing effect-family controllers are represented as failures,
    rather than silently treating an unobservable resource family as empty.
    """

    def __init__(
        self,
        *,
        execution_repository: ExecutionRepository,
        execution_runner: ExecutionRunner,
        resource_stoppers: Mapping[str, RunResourceStopper] | None = None,
        execution_cancel_timeout_seconds: float = 5.0,
        execution_cancel_poll_seconds: float = 0.05,
        execution_cancel_max_passes: int = 5,
        resource_stop_poll_seconds: float = 0.05,
        resource_stop_max_passes: int = 20,
        require_all_resource_stoppers: bool = True,
    ) -> None:
        if execution_cancel_timeout_seconds < 0:
            raise ValueError("execution_cancel_timeout_seconds must not be negative")
        if execution_cancel_poll_seconds <= 0:
            raise ValueError("execution_cancel_poll_seconds must be positive")
        if execution_cancel_max_passes < 1:
            raise ValueError("execution_cancel_max_passes must be positive")
        if resource_stop_poll_seconds <= 0:
            raise ValueError("resource_stop_poll_seconds must be positive")
        if resource_stop_max_passes < 1:
            raise ValueError("resource_stop_max_passes must be positive")
        self._execution_repository = execution_repository
        self._execution_runner = execution_runner
        self._resource_stoppers = dict(resource_stoppers or {})
        if "executions" in self._resource_stoppers:
            raise ValueError("resource_stoppers must not replace the execution stopper")
        if any(
            not isinstance(resource_type, str) or not resource_type.strip()
            for resource_type in self._resource_stoppers
        ):
            raise ValueError("resource stopper names must not be empty")
        unknown = set(self._resource_stoppers).difference(_REQUIRED_RESOURCE_TYPES)
        if unknown:
            raise ValueError(f"unsupported resource stopper types: {sorted(unknown)!r}")
        self._require_all_resource_stoppers = require_all_resource_stoppers
        self._execution_cancel_timeout_seconds = execution_cancel_timeout_seconds
        self._execution_cancel_poll_seconds = execution_cancel_poll_seconds
        self._execution_cancel_max_passes = execution_cancel_max_passes
        self._resource_stop_poll_seconds = resource_stop_poll_seconds
        self._resource_stop_max_passes = resource_stop_max_passes

    async def stop_run(self, run_id: str, *, drain: bool = True) -> SafetyStopResult:
        """Drain all known effects after the caller closes effect admission."""

        result = await self._stop_pass(run_id, drain=drain)
        passes = self._resource_stop_max_passes if drain else 1
        for _ in range(1, passes):
            retryable_failures = set(result.failed_resource_types).intersection(
                _REQUIRED_RESOURCE_TYPES
            )
            if not retryable_failures:
                break
            # Another process may own an in-memory Browser/HTTP handle. Its
            # reconciler observes the same durable Run fence, stops the handle,
            # and persists the ACK; re-enumeration then converges. Executions
            # are not cancelled repeatedly: their first bounded cancel already
            # waits for the owning Runner's durable status acknowledgement.
            await asyncio.sleep(self._resource_stop_poll_seconds)
            retried = await self._stop_resource_families(run_id)
            result = SafetyStopResult(
                resources={
                    "executions": result.resources["executions"],
                    **retried,
                }
            )
        return result

    async def _stop_pass(self, run_id: str, *, drain: bool) -> SafetyStopResult:
        """Perform one three-family stop and evidence pass."""

        resource_types = ["executions", *_REQUIRED_RESOURCE_TYPES]
        operations: list[Awaitable[object] | None] = [
            self._cancel_executions(run_id, drain=drain),
            *(self._stop_operation(resource_type, run_id) for resource_type in resource_types[1:]),
        ]
        present_operations = [operation for operation in operations if operation is not None]
        gathered = await asyncio.gather(*present_operations, return_exceptions=True)
        results: list[object] = []
        result_index = 0
        for resource_type, operation in zip(resource_types, operations, strict=True):
            if operation is None:
                results.append(
                    RuntimeError(f"no {resource_type} stop controller is configured")
                    if self._require_all_resource_stoppers
                    else ResourceStopDisposition((), {}, {}, {}, {})
                )
                continue
            results.append(gathered[result_index])
            result_index += 1

        return SafetyStopResult(
            resources={
                resource_type: self._normalize_stop_disposition(resource_type, result)
                for resource_type, result in zip(resource_types, results, strict=True)
            }
        )

    async def _stop_resource_families(
        self,
        run_id: str,
    ) -> dict[str, ResourceStopDisposition]:
        operations = [
            self._stop_operation(resource_type, run_id)
            for resource_type in _REQUIRED_RESOURCE_TYPES
        ]
        present_operations = [operation for operation in operations if operation is not None]
        gathered = await asyncio.gather(*present_operations, return_exceptions=True)
        results: list[object] = []
        result_index = 0
        for resource_type, operation in zip(
            _REQUIRED_RESOURCE_TYPES,
            operations,
            strict=True,
        ):
            if operation is None:
                results.append(
                    RuntimeError(f"no {resource_type} stop controller is configured")
                    if self._require_all_resource_stoppers
                    else ResourceStopDisposition((), {}, {}, {}, {})
                )
                continue
            results.append(gathered[result_index])
            result_index += 1
        return {
            resource_type: self._normalize_stop_disposition(resource_type, result)
            for resource_type, result in zip(_REQUIRED_RESOURCE_TYPES, results, strict=True)
        }

    def _stop_operation(self, resource_type: str, run_id: str) -> Awaitable[object] | None:
        stopper = self._resource_stoppers.get(resource_type)
        return stopper.stop_run(run_id) if stopper is not None else None

    @staticmethod
    def _normalize_stop_disposition(
        resource_type: str,
        result: object,
    ) -> ResourceStopDisposition:
        if isinstance(result, BaseException):
            return RunSafetyStopService._failed_controller_disposition(resource_type, result)

        try:
            disposition = cast(RunResourceStopResult, result)
            attempted_ids = tuple(disposition.attempted_ids)
            node_ids = dict(disposition.node_ids)
            observed_statuses = dict(disposition.observed_statuses)
            confirmed_statuses = dict(disposition.confirmed_statuses)
            failures = dict(disposition.failures)
            if any(not isinstance(item, str) or not item for item in attempted_ids):
                raise ValueError("attempted resource IDs must be non-empty strings")
            if any(
                not isinstance(item, str) or not item
                for mapping in (node_ids, observed_statuses, confirmed_statuses, failures)
                for item in mapping
            ):
                raise ValueError("stop evidence keys must be non-empty strings")
            if any(
                not isinstance(value, str)
                for mapping in (node_ids, observed_statuses, confirmed_statuses, failures)
                for value in mapping.values()
            ):
                raise ValueError("stop evidence values must be strings")
        except (AttributeError, TypeError, ValueError) as exc:
            return RunSafetyStopService._failed_controller_disposition(
                resource_type,
                RuntimeError(f"invalid stop evidence: {exc}"),
            )

        all_ids = set(attempted_ids)
        for mapping in (node_ids, observed_statuses, confirmed_statuses, failures):
            all_ids.update(mapping)
        allowed_confirmed_statuses = {
            "executions": frozenset(
                {
                    ExecutionStatus.COMPLETED.value,
                    ExecutionStatus.EXITED.value,
                    ExecutionStatus.CANCELLED.value,
                    ExecutionStatus.HARD_TIMEOUT.value,
                }
            ),
            "browser_sessions": frozenset({"closed"}),
            "target_http_requests": frozenset({"completed", "rejected", "failed", "cancelled"}),
        }[resource_type]
        for resource_id, status in tuple(confirmed_statuses.items()):
            if status in allowed_confirmed_statuses:
                continue
            confirmed_statuses.pop(resource_id)
            failures.setdefault(
                resource_id,
                f"{resource_type} stop returned untrusted confirmation status {status!r}",
            )
        for resource_id in all_ids.difference(confirmed_statuses, failures):
            failures[resource_id] = f"{resource_type} stop returned no affirmative confirmation"
        return ResourceStopDisposition(
            attempted_ids=tuple(sorted(all_ids)),
            node_ids=dict(sorted(node_ids.items())),
            observed_statuses=dict(sorted(observed_statuses.items())),
            confirmed_statuses=dict(sorted(confirmed_statuses.items())),
            failures=dict(sorted(failures.items())),
        )

    @staticmethod
    def _failed_controller_disposition(
        resource_type: str,
        failure: BaseException,
    ) -> ResourceStopDisposition:
        controller_id = f"{resource_type}:controller"
        return ResourceStopDisposition(
            attempted_ids=(controller_id,),
            node_ids={},
            observed_statuses={},
            confirmed_statuses={},
            failures={controller_id: f"{type(failure).__name__}: {failure}"},
        )

    async def _cancel_executions(
        self,
        run_id: str,
        *,
        drain: bool,
    ) -> _ExecutionStopResult:
        attempted: set[str] = set()
        node_ids: dict[str, str] = {}
        observed_statuses: dict[str, str] = {}
        confirmed: dict[str, str] = {}
        failures: dict[str, str] = {}
        passes = self._execution_cancel_max_passes if drain else 1

        for _ in range(passes):
            candidates = [
                execution
                for execution in await self._list_run_executions(run_id)
                if _requires_execution_stop(execution) and execution.id not in attempted
            ]
            if not candidates:
                break
            attempted.update(execution.id for execution in candidates)
            node_ids.update({execution.id: execution.node_id for execution in candidates})
            observed_statuses.update(
                {execution.id: execution.status.value for execution in candidates}
            )
            results = await asyncio.gather(
                *(self._cancel_and_confirm(execution) for execution in candidates),
                return_exceptions=True,
            )
            for execution, result in zip(candidates, results, strict=True):
                if isinstance(result, BaseException):
                    failures[execution.id] = f"{type(result).__name__}: {result}"
                    continue
                observed_statuses[execution.id] = result.status.value
                if _has_affirmative_stop_proof(result):
                    confirmed[execution.id] = result.status.value
                    failures.pop(execution.id, None)
                else:
                    failures[execution.id] = (
                        "execution cancellation returned without durable physical-stop proof"
                    )
            if not drain:
                break
            await asyncio.sleep(0)

        refreshed = await self._list_run_executions(run_id)
        remaining = []
        for execution in refreshed:
            if _has_affirmative_stop_proof(execution):
                if execution.id in attempted:
                    node_ids[execution.id] = execution.node_id
                    observed_statuses[execution.id] = execution.status.value
                    confirmed[execution.id] = execution.status.value
                    failures.pop(execution.id, None)
                continue
            if execution.id in attempted:
                node_ids[execution.id] = execution.node_id
                observed_statuses[execution.id] = execution.status.value
            if _requires_execution_stop(execution) and execution.id not in confirmed:
                remaining.append(execution)
            elif execution.id in attempted:
                failures.setdefault(
                    execution.id,
                    "execution reached a terminal status without durable physical-stop proof",
                )
        for execution in remaining:
            attempted.add(execution.id)
            failures.setdefault(
                execution.id,
                f"stop was not confirmed; execution remains {execution.status.value}",
            )
        return _ExecutionStopResult(
            attempted_ids=tuple(sorted(attempted)),
            node_ids=dict(sorted(node_ids.items())),
            observed_statuses=dict(sorted(observed_statuses.items())),
            confirmed_statuses=dict(sorted(confirmed.items())),
            failures=dict(sorted(failures.items())),
        )

    async def _list_run_executions(self, run_id: str) -> list[Execution]:
        executions: list[Execution] = []
        offset = 0
        while True:
            page = list(
                await self._execution_repository.list(
                    run_id,
                    limit=_EXECUTION_LIST_PAGE_SIZE,
                    offset=offset,
                )
            )
            executions.extend(page)
            if len(page) < _EXECUTION_LIST_PAGE_SIZE:
                return executions
            offset += len(page)

    async def _cancel_and_confirm(self, execution: Execution) -> Execution:
        await self._execution_runner.cancel(execution.id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._execution_cancel_timeout_seconds
        while True:
            current = await self._execution_repository.get(execution.id)
            if current is None:
                raise RuntimeError("execution disappeared before stop could be confirmed")
            if _has_affirmative_stop_proof(current):
                return current
            if loop.time() >= deadline:
                if current.status is ExecutionStatus.LOST:
                    raise TimeoutError(
                        "execution remains lost; cancellation was queued but the Runner "
                        "did not acknowledge that the process stopped"
                    )
                if current.status in _TERMINAL_STOP_AUDIT_STATUSES:
                    raise TimeoutError(
                        f"execution reached {current.status.value} without durable "
                        "physical-stop proof"
                    )
                raise TimeoutError(f"execution remains {current.status.value} after cancellation")
            await asyncio.sleep(self._execution_cancel_poll_seconds)


def _requires_execution_stop(execution: Execution) -> bool:
    if _has_affirmative_stop_proof(execution):
        return False
    if execution.status in _PENDING_STOP_EXECUTION_STATUSES:
        return True
    # Every terminal execution without proof must be audited.  Missing PID/PGID
    # can itself be an incomplete/crashed admission record; it is not absence
    # evidence once started_at/process_created_at exists.
    return execution.status in _TERMINAL_STOP_AUDIT_STATUSES


def _has_affirmative_stop_proof(execution: Execution) -> bool:
    if (
        execution.status in _TERMINAL_STOP_AUDIT_STATUSES
        and execution.physical_stop_confirmed_at is not None
    ):
        return True
    # Backward-compatible, intrinsically safe proof for rows cancelled before
    # any process identity or activation could exist.  New writers also stamp
    # physical_stop_confirmed_at, but old pre-spawn rows remain unambiguous.
    return execution.status is ExecutionStatus.CANCELLED and all(
        value is None
        for value in (
            execution.started_at,
            execution.process_created_at,
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
        )
    )


def stop_resources_payload(result: SafetyStopResult) -> dict[str, object]:
    return {
        resource_type: {
            "attempted_ids": list(disposition.attempted_ids),
            "node_ids": disposition.node_ids,
            "observed_statuses": disposition.observed_statuses,
            "confirmed_ids": list(disposition.confirmed_ids),
            "confirmed_statuses": disposition.confirmed_statuses,
            "failures": disposition.failures,
            "succeeded": disposition.succeeded,
        }
        for resource_type, disposition in result.resources.items()
    }
