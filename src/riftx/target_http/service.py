"""Scope and approval gate for idempotent Runner Target HTTP requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    PentestBudgetExceededError,
    pentest_budget_exhaustion_details,
)
from riftx.application.ports import RunEventRepository
from riftx.application.services.artifacts import (
    ArtifactApplicationService,
    RegisterArtifactContent,
)
from riftx.application.services.runs import require_interactive_run_operation
from riftx.capabilities import (
    CapabilityKind,
    CapabilitySelectionStore,
    CapabilityVersion,
    CapabilityVersionStatus,
)
from riftx.domain import ArtifactContentTrust, Run, RunStatus
from riftx.execution import build_execution_key
from riftx.persistence.repositories import SQLAlchemyRunRepository
from riftx.persistence.runtime_repositories import SQLAlchemyToolCallIntentRepository
from riftx.runtime.types import ToolCallIntent, ToolCallStatus
from riftx.scope import ScopeGuard, ScopeTargetKind

from .errors import (
    TargetHttpRunnerExecutionCancelledError,
    TargetHttpRunnerExecutionUncertainError,
)
from .models import (
    TargetHttpExchange,
    TargetHttpResult,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
    TargetHttpSubmission,
)
from .redaction import safe_url_metadata

EffectGuard = Callable[[], Awaitable[None]]
BudgetExhaustionHandler = Callable[[str], Awaitable[None]]

_TARGET_HTTP_TOOL_IDS = frozenset({"request_target_url", "target_http_request"})
_ACTIVE_INTENT_STATUSES = frozenset(
    {
        ToolCallStatus.READY,
        ToolCallStatus.EXECUTING,
    }
)
_TERMINAL_INTENT_STATUSES = frozenset(
    {
        ToolCallStatus.COMPLETED,
        ToolCallStatus.REJECTED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
    }
)
_EFFECT_BLOCKED_RUN_STATUSES = frozenset(
    {
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }
)


class TargetHttpRunner(Protocol):
    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TargetHttpExchange: ...

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: Sequence[str],
    ) -> list[TargetHttpRunnerStopOutcome]: ...


class TargetHttpRequestRepository(Protocol):
    async def get_by_execution_key(self, execution_key: str) -> TargetHttpResult | None: ...

    async def get_for_run(
        self,
        run_id: str,
        request_id: str,
    ) -> TargetHttpResult | None: ...

    async def create(
        self,
        submission: TargetHttpSubmission,
        result: TargetHttpResult,
    ) -> TargetHttpResult: ...


class TargetHttpCredentialReferenceAuthorizer(Protocol):
    async def require_allowed(
        self,
        *,
        run_id: str,
        session_id: str,
        references: Sequence[str],
    ) -> None: ...


class CapabilityCredentialReferenceAuthorizer:
    """Allow only references pinned in this Session's selected Techniques."""

    def __init__(self, selections: CapabilitySelectionStore) -> None:
        self._selections = selections

    async def require_allowed(
        self,
        *,
        run_id: str,
        session_id: str,
        references: Sequence[str],
    ) -> None:
        requested = frozenset(references)
        if not requested:
            return
        allowed: set[str] = set()
        selections = await self._selections.list_selections(
            session_id,
            kind=CapabilityKind.TECHNIQUE,
        )
        for selection in selections:
            if (
                not selection.active
                or selection.run_id != run_id
                or selection.session_id != session_id
            ):
                continue
            try:
                version = CapabilityVersion.model_validate(
                    selection.snapshot.get("capability_version")
                )
            except (TypeError, ValueError) as exc:
                raise ApplicationConflictError(
                    "target_http_credential_selection_invalid",
                    "Target HTTP credential permission snapshot is invalid",
                ) from exc
            manifest = version.manifest
            if (
                version.status is not CapabilityVersionStatus.ACTIVE
                or manifest.kind is not CapabilityKind.TECHNIQUE
                or manifest.capability_id != selection.capability_id
                or manifest.version != selection.version
                or version.manifest_digest != selection.digest
                or manifest.provenance.source is not selection.source
            ):
                raise ApplicationConflictError(
                    "target_http_credential_selection_invalid",
                    "Target HTTP credential permission snapshot failed integrity validation",
                )
            allowed.update(manifest.permission.credential_references)
        if not requested <= allowed:
            raise ApplicationConflictError(
                "target_http_credential_reference_forbidden",
                "Target HTTP credential reference is outside the Session Capability permission",
                details={"forbidden_reference_count": len(requested - allowed)},
            )


@dataclass(frozen=True, slots=True)
class TargetHttpRunStopResult:
    """Fail-closed, per-intent evidence for Run lifecycle aggregation."""

    run_id: str
    attempted_ids: tuple[str, ...]
    node_ids: dict[str, str]
    initial_statuses: dict[str, str]
    observed_statuses: dict[str, str]
    confirmed_statuses: dict[str, str]
    failures: dict[str, str]

    @property
    def confirmed_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.confirmed_statuses))

    @property
    def succeeded(self) -> bool:
        return not self.failures


class TargetHttpApplicationService:
    def __init__(
        self,
        *,
        runs: SQLAlchemyRunRepository,
        tool_calls: SQLAlchemyToolCallIntentRepository,
        requests: TargetHttpRequestRepository,
        runner: TargetHttpRunner,
        artifacts: ArtifactApplicationService,
        events: RunEventRepository | None = None,
        target_http_tool_ids: Sequence[str] = tuple(_TARGET_HTTP_TOOL_IDS),
        credential_references: TargetHttpCredentialReferenceAuthorizer | None = None,
        budget_exhaustion_handler: BudgetExhaustionHandler | None = None,
    ) -> None:
        if not target_http_tool_ids:
            raise ValueError("Target HTTP must own at least one Tool Call id")
        self._runs = runs
        self._tool_calls = tool_calls
        self._requests = requests
        self._runner = runner
        self._artifacts = artifacts
        self._events = events
        self._target_http_tool_ids = frozenset(target_http_tool_ids)
        self._credential_references = credential_references
        self._budget_exhaustion_handler = budget_exhaustion_handler
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    async def execute(self, submission: TargetHttpSubmission) -> TargetHttpResult:
        request = submission.request
        expected_key = build_execution_key(
            run_id=submission.run_id,
            session_id=submission.session_id,
            tool_call_id=submission.tool_call_id,
            attempt_group="initial",
        )
        if request.execution_key != expected_key:
            raise ApplicationConflictError(
                "target_http_execution_key_mismatch",
                "Target HTTP execution key does not match its Run/Session/Tool identity",
            )
        lock = self._locks.setdefault(request.execution_key, asyncio.Lock())
        self._lock_users[request.execution_key] = self._lock_users.get(request.execution_key, 0) + 1
        async with lock:
            try:
                # RunKind admission deliberately precedes idempotent replay.
                # Generic execution results must never become an alternate
                # read/mutation surface for Code Audit Runs.
                run = require_interactive_run_operation(
                    await self._require_run(submission.run_id)
                )
                existing = await self._requests.get_by_execution_key(request.execution_key)
                if existing is not None:
                    if existing.request_hash != request.fingerprint:
                        raise ApplicationConflictError(
                            "target_http_idempotency_conflict",
                            "Target HTTP execution key was already used for another request",
                        )
                    return existing
                self._raise_if_run_blocks_effect(run)
                if run.node_id != submission.node_id:
                    raise ApplicationConflictError(
                        "target_http_node_mismatch",
                        "Target HTTP must execute on the Run's Runner node",
                    )
                ScopeGuard(run.scope).require(request.url, kind=ScopeTargetKind.URL)
                intent = await self._tool_calls.get(submission.tool_call_id)
                if (
                    intent is None
                    or intent.run_id != submission.run_id
                    or intent.session_id != submission.session_id
                ):
                    raise EntityNotFoundError("ToolCallIntent", submission.tool_call_id)
                if intent.tool_id not in self._target_http_tool_ids:
                    raise ApplicationConflictError(
                        "target_http_tool_mismatch",
                        "Tool Call intent is not owned by the Target HTTP boundary",
                    )
                self._raise_if_intent_inactive(intent)
                if request.credential_references:
                    if self._credential_references is None:
                        raise ApplicationConflictError(
                            "target_http_credential_reference_unavailable",
                            "Target HTTP credential references are not configured",
                        )
                    await self._credential_references.require_allowed(
                        run_id=submission.run_id,
                        session_id=submission.session_id,
                        references=request.credential_references,
                    )
                try:
                    claim = await self._tool_calls.claim_execution(
                        intent.id,
                        execution_key=request.execution_key,
                        attempt_group="initial",
                        target_interaction_tool_ids=self._target_http_tool_ids,
                    )
                except PentestBudgetExceededError as exc:
                    details = pentest_budget_exhaustion_details(
                        submission.run_id,
                        exc,
                    )
                    concurrency_limited = (
                        exc.budget_name == "max_concurrent_target_interactions"
                    )
                    await self._event(
                        submission.run_id,
                        (
                            "pentest.budget_capacity_reached"
                            if concurrency_limited
                            else "pentest.budget_exhausted"
                        ),
                        details,
                    )
                    if (
                        not concurrency_limited
                        and self._budget_exhaustion_handler is not None
                    ):
                        await self._budget_exhaustion_handler(submission.run_id)
                    raise ApplicationConflictError(
                        (
                            "pentest_budget_capacity_reached"
                            if concurrency_limited
                            else "pentest_budget_exhausted"
                        ),
                        (
                            "Pentest target interaction concurrency is at capacity"
                            if concurrency_limited
                            else "Pentest target interaction budget is exhausted"
                        ),
                        details=details,
                    ) from exc
                if not claim.acquired:
                    raise ApplicationConflictError(
                        "target_http_execution_claim_conflict",
                        "Target HTTP Tool Call cannot claim this execution identity",
                        details={
                            "run_id": submission.run_id,
                            "tool_call_id": intent.id,
                            "status": claim.intent.status.value,
                        },
                    )
                intent = claim.intent

                async def effect_guard() -> None:
                    await self._require_effect_allowed(
                        submission.run_id,
                        submission.tool_call_id,
                    )

                try:
                    await effect_guard()
                    await self._event(
                        submission.run_id,
                        "target_http.request_started",
                        {
                            "url_summary": safe_url_metadata(request.url),
                            "url_redacted": True,
                        },
                    )
                    exchange = await self._runner.execute(
                        TargetHttpRunnerRequest(
                            run_id=submission.run_id,
                            session_id=submission.session_id,
                            tool_call_id=submission.tool_call_id,
                            node_id=submission.node_id,
                            scope=run.scope,
                            request=request,
                        ),
                        effect_guard=effect_guard,
                    )
                    # The request may have completed concurrently with a Run
                    # stop.  Re-read both fences before any result can revive
                    # the cancelled intent or become a new durable effect.
                    await effect_guard()
                    result = await self._save_artifacts(submission, exchange)
                    await effect_guard()
                    result = await self._requests.create(submission, result)
                    await effect_guard()
                except TargetHttpRunnerExecutionCancelledError as exc:
                    await self._apply_runner_stop_ack(
                        submission,
                        exc.stop_outcome,
                    )
                    raise
                except asyncio.CancelledError:
                    outcome = await self._stop_cancelled_execution(submission)
                    await self._apply_runner_stop_ack(submission, outcome)
                    raise
                except TargetHttpRunnerExecutionUncertainError as exc:
                    # A failed/replayed durable Runner command or a locally
                    # unclosed client may still own a live network effect. Only
                    # an explicit stop ACK can terminalize the intent; otherwise
                    # EXECUTING keeps future stops retryable and the Run fence intact.
                    await self._apply_runner_stop_ack(
                        submission,
                        exc.stop_outcome,
                    )
                    raise
                except ApplicationConflictError as exc:
                    if exc.code == "run_kind_operation_unsupported":
                        raise
                    _, failed = await self._tool_calls.compare_and_set_status(
                        intent.id,
                        expected={ToolCallStatus.EXECUTING},
                        target=ToolCallStatus.FAILED,
                    )
                    if failed:
                        await self._event(
                            submission.run_id,
                            "target_http.request_failed",
                            {"failure_recorded": True, "category": "request_failed"},
                        )
                    raise
                except Exception:
                    _, failed = await self._tool_calls.compare_and_set_status(
                        intent.id,
                        expected={ToolCallStatus.EXECUTING},
                        target=ToolCallStatus.FAILED,
                    )
                    if failed:
                        await self._event(
                            submission.run_id,
                            "target_http.request_failed",
                            {"failure_recorded": True, "category": "request_failed"},
                        )
                    raise
                current, completed = await self._tool_calls.compare_and_set_status(
                    intent.id,
                    expected={ToolCallStatus.EXECUTING},
                    target=ToolCallStatus.COMPLETED,
                )
                if not completed:
                    raise ApplicationConflictError(
                        "target_http_stopped",
                        "Target HTTP completed after its Tool Call intent was stopped",
                        details={
                            "run_id": submission.run_id,
                            "tool_call_id": intent.id,
                            "status": current.status.value,
                        },
                    )
                await self._event(
                    submission.run_id,
                    "target_http.response_received",
                    {
                        "response_recorded": True,
                        "status_code": result.status_code,
                    },
                )
                return result
            finally:
                self._lock_users[request.execution_key] -= 1
                if self._lock_users[request.execution_key] == 0:
                    self._lock_users.pop(request.execution_key, None)
                    self._locks.pop(request.execution_key, None)

    async def get_result(self, run_id: str, request_id: str) -> TargetHttpResult:
        require_interactive_run_operation(await self._require_run(run_id))
        result = await self._requests.get_for_run(run_id, request_id)
        if result is None or result.request_id != request_id:
            raise EntityNotFoundError("TargetHttpResult", request_id)
        return result

    async def stop_run(self, run_id: str) -> TargetHttpRunStopResult:
        """Stop every owned READY/EXECUTING intent without inventing remote ACKs."""

        run = await self._require_run(run_id)
        candidates = await self._tool_calls.active_for_run(
            run_id,
            tool_ids=self._target_http_tool_ids,
        )
        attempted_ids = tuple(sorted(intent.id for intent in candidates))
        node_ids = {intent_id: run.node_id for intent_id in attempted_ids}
        initial_statuses = {intent.id: intent.status.value for intent in candidates}
        observed_statuses = dict(initial_statuses)
        confirmed_statuses: dict[str, str] = {}
        failures: dict[str, str] = {}
        executing: dict[str, ToolCallIntent] = {}

        for intent in candidates:
            if intent.status is ToolCallStatus.EXECUTING:
                executing[intent.id] = intent
                continue
            current, cancelled = await self._tool_calls.compare_and_set_status(
                intent.id,
                expected={ToolCallStatus.READY},
                target=ToolCallStatus.CANCELLED,
            )
            observed_statuses[intent.id] = current.status.value
            if cancelled or current.status is ToolCallStatus.CANCELLED:
                confirmed_statuses[intent.id] = current.status.value
            elif current.status is ToolCallStatus.EXECUTING:
                executing[intent.id] = current
            elif current.status in _TERMINAL_INTENT_STATUSES:
                confirmed_statuses[intent.id] = current.status.value
            else:
                failures[intent.id] = (
                    "Target HTTP READY cancellation lost to unexpected intent status "
                    f"{current.status.value}"
                )

        if executing:
            try:
                runner_outcomes = await self._runner.stop_run(
                    run_id,
                    node_id=run.node_id,
                    tool_call_ids=tuple(sorted(executing)),
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                failures.update({intent_id: reason for intent_id in executing})
            else:
                outcomes: dict[str, TargetHttpRunnerStopOutcome] = {}
                duplicates: set[str] = set()
                for outcome in runner_outcomes:
                    if outcome.tool_call_id in outcomes:
                        duplicates.add(outcome.tool_call_id)
                    outcomes[outcome.tool_call_id] = outcome
                for intent_id in executing:
                    if intent_id in duplicates:
                        failures[intent_id] = "Runner returned duplicate Target HTTP stop outcomes"
                        continue
                    runner_outcome = outcomes.get(intent_id)
                    if runner_outcome is None:
                        failures[intent_id] = "Runner omitted Target HTTP stop outcome"
                        continue
                    persisted_intent = await self._tool_calls.get(intent_id)
                    if persisted_intent is None:
                        failures[intent_id] = "Target HTTP intent disappeared during stop"
                        continue
                    observed_statuses[intent_id] = persisted_intent.status.value
                    if not runner_outcome.confirmed:
                        failures[intent_id] = (
                            runner_outcome.reason or "Target HTTP stop was unconfirmed"
                        )
                        continue
                    current, _ = await self._tool_calls.compare_and_set_status(
                        intent_id,
                        expected={
                            ToolCallStatus.READY,
                            ToolCallStatus.EXECUTING,
                        },
                        target=ToolCallStatus.CANCELLED,
                    )
                    observed_statuses[intent_id] = current.status.value
                    if current.status in _TERMINAL_INTENT_STATUSES:
                        confirmed_statuses[intent_id] = current.status.value
                        failures.pop(intent_id, None)
                    else:
                        failures[intent_id] = (
                            "Runner stopped Target HTTP locally but its intent remained "
                            f"{current.status.value}"
                        )

        return TargetHttpRunStopResult(
            run_id=run_id,
            attempted_ids=attempted_ids,
            node_ids=dict(sorted(node_ids.items())),
            initial_statuses=dict(sorted(initial_statuses.items())),
            observed_statuses=dict(sorted(observed_statuses.items())),
            confirmed_statuses=dict(sorted(confirmed_statuses.items())),
            failures=dict(sorted(failures.items())),
        )

    async def _require_run(self, run_id: str) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _require_effect_allowed(self, run_id: str, intent_id: str) -> None:
        run = await self._require_run(run_id)
        require_interactive_run_operation(run)
        if run.status in _EFFECT_BLOCKED_RUN_STATUSES:
            await self._cancel_intent_if_active(intent_id)
            raise self._run_effect_blocked_error(run)
        intent = await self._tool_calls.get(intent_id)
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", intent_id)
        if intent.status is not ToolCallStatus.EXECUTING:
            raise ApplicationConflictError(
                "target_http_stopped",
                "Target HTTP Tool Call is no longer executable",
                details={
                    "run_id": run_id,
                    "tool_call_id": intent_id,
                    "status": intent.status.value,
                },
            )

    async def _cancel_intent_if_active(
        self,
        intent_id: str,
    ) -> tuple[ToolCallIntent, bool]:
        return await self._tool_calls.compare_and_set_status(
            intent_id,
            expected=_ACTIVE_INTENT_STATUSES,
            target=ToolCallStatus.CANCELLED,
        )

    async def _stop_cancelled_execution(
        self,
        submission: TargetHttpSubmission,
    ) -> TargetHttpRunnerStopOutcome:
        stop_task = asyncio.create_task(
            self._runner.stop_run(
                submission.run_id,
                node_id=submission.node_id,
                tool_call_ids=[submission.tool_call_id],
            ),
            name=(f"target-http-control-cancel:{submission.run_id}:{submission.tool_call_id}"),
        )
        while True:
            try:
                outcomes = await asyncio.shield(stop_task)
                break
            except asyncio.CancelledError:
                if not stop_task.done():
                    continue
                try:
                    outcomes = stop_task.result()
                except BaseException as exc:
                    return TargetHttpRunnerStopOutcome(
                        tool_call_id=submission.tool_call_id,
                        confirmed=False,
                        reason=(
                            "target_http_control_cancel_stop_unconfirmed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                break
            except Exception as exc:
                return TargetHttpRunnerStopOutcome(
                    tool_call_id=submission.tool_call_id,
                    confirmed=False,
                    reason=(
                        f"target_http_control_cancel_stop_unconfirmed: {type(exc).__name__}: {exc}"
                    ),
                )
        matching = [
            outcome for outcome in outcomes if outcome.tool_call_id == submission.tool_call_id
        ]
        if len(matching) != 1:
            return TargetHttpRunnerStopOutcome(
                tool_call_id=submission.tool_call_id,
                confirmed=False,
                reason="target_http_control_cancel_stop_ack_invalid",
            )
        return matching[0]

    async def _apply_runner_stop_ack(
        self,
        submission: TargetHttpSubmission,
        outcome: TargetHttpRunnerStopOutcome,
    ) -> None:
        if outcome.tool_call_id != submission.tool_call_id or not outcome.confirmed:
            return
        _, cancelled = await self._tool_calls.compare_and_set_status(
            submission.tool_call_id,
            expected={ToolCallStatus.EXECUTING},
            target=ToolCallStatus.CANCELLED,
        )
        if cancelled:
            await self._event(
                submission.run_id,
                "target_http.request_cancelled",
                {
                    "cancellation_confirmed": True,
                    "category": "runner_confirmed",
                },
            )

    @staticmethod
    def _raise_if_run_blocks_effect(run: Run) -> None:
        if run.status in _EFFECT_BLOCKED_RUN_STATUSES:
            raise TargetHttpApplicationService._run_effect_blocked_error(run)

    @staticmethod
    def _run_effect_blocked_error(run: Run) -> ApplicationConflictError:
        return ApplicationConflictError(
            "run_target_http_blocked",
            f"Run {run.id!r} cannot produce Target HTTP effects while it is {run.status.value}",
            details={"run_id": run.id, "status": run.status.value},
        )

    @staticmethod
    def _raise_if_intent_inactive(intent: ToolCallIntent) -> None:
        if intent.status in _ACTIVE_INTENT_STATUSES:
            return
        raise ApplicationConflictError(
            "target_http_not_approved",
            "Target HTTP Tool Call is not approved for execution",
            details={
                "tool_call_id": intent.id,
                "status": intent.status.value,
            },
        )

    async def _save_artifacts(self, submission, exchange):
        request = submission.request
        result = exchange.result
        request_artifact_id = None
        response_artifact_id = None
        if request.save_request:
            artifact = await self._artifacts.register_content(
                submission.run_id,
                RegisterArtifactContent(
                    content=json.dumps(
                        request.runner_payload(), ensure_ascii=False, indent=2
                    ).encode(),
                    name=f"target-http-{result.request_id}-request.json",
                    mime_type="application/json",
                    description="Immutable Target HTTP request",
                    content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
                ),
            )
            request_artifact_id = artifact.id
        if request.save_response:
            artifact = await self._artifacts.register_content(
                submission.run_id,
                RegisterArtifactContent(
                    content=exchange.response_body,
                    name=f"target-http-{result.request_id}-response.bin",
                    mime_type=result.content_type or "application/octet-stream",
                    description="Immutable Target HTTP response body",
                    content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
                ),
            )
            response_artifact_id = artifact.id
        return result.model_copy(
            update={
                "request_artifact_id": request_artifact_id,
                "response_artifact_id": response_artifact_id,
            }
        )

    async def _event(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload)
