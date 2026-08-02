"""Application service for durable Run creation and control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    ApplicationServiceError,
    EntityNotFoundError,
    RepositoryConflictError,
    ServiceUnavailableError,
)
from riftx.application.finalization import RunFinalizationIntent
from riftx.application.ports import (
    EngagementRepository,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.application.services.run_safety import (
    RunResourceStopper,
    RunSafetyStopService,
    SafetyStopResult,
    stop_resources_payload,
)
from riftx.domain import (
    ApprovalMode,
    Engagement,
    EntryPoint,
    InvalidStateTransitionError,
    Objective,
    Run,
    RunKind,
    RunStatus,
    Scope,
    SuccessCriterion,
)
from riftx.runner import ExecutionRunner

_EVENT_LIST_PAGE_SIZE = 1000
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)
_SAFETY_FENCE_RUN_STATUSES = frozenset(
    {
        RunStatus.PAUSING,
        RunStatus.CANCELLING,
        RunStatus.COMPLETING,
    }
)


class RunWorkflowClient(Protocol):
    """Small Temporal boundary consumed by the control plane."""

    async def start_run(self, run_id: str) -> object: ...

    async def pause(self, run_id: str) -> None: ...

    async def resume(self, run_id: str) -> None: ...

    async def approve(self, run_id: str, call_id: str) -> None: ...

    async def reject(self, run_id: str, call_id: str) -> None: ...

    async def cancel_current_execution(self, run_id: str) -> None: ...

    async def cancel(self, run_id: str) -> None: ...

    async def compact(self, run_id: str, max_history_items: int = 100) -> None: ...

    async def switch_model(self, run_id: str, model_profile: str) -> None: ...

    async def append_user_message(self, run_id: str, user_input_id: str) -> None: ...

    def workflow_id(self, run_id: str) -> str: ...


class ModelProfileResolver(Protocol):
    async def resolve_profile(self, profile_name: str | None) -> str: ...


@dataclass(frozen=True, slots=True)
class CreateEngagement:
    name: str
    description: str = ""
    authorization_reference: str | None = None


@dataclass(frozen=True, slots=True)
class CreateRun:
    objective: str
    node_id: str
    approval_mode: ApprovalMode = ApprovalMode.BALANCED
    model_profile: str | None = None
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    entry_points: list[EntryPoint] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    workspace_path: str | None = None
    engagement_id: str | None = None
    engagement: CreateEngagement | None = None


class RunApplicationService:
    def __init__(
        self,
        *,
        engagement_repository: EngagementRepository,
        run_repository: RunRepository,
        event_repository: RunEventRepository,
        workflow_client: RunWorkflowClient,
        execution_repository: ExecutionRepository,
        execution_runner: ExecutionRunner,
        workspace_root: Path,
        model_profiles: ModelProfileResolver | None = None,
        resource_stoppers: Mapping[str, RunResourceStopper] | None = None,
        safety_stopper: RunSafetyStopService | None = None,
        execution_cancel_timeout_seconds: float = 5.0,
        execution_cancel_poll_seconds: float = 0.05,
        execution_cancel_max_passes: int = 5,
        workflow_signal_timeout_seconds: float = 0.5,
    ) -> None:
        if execution_cancel_timeout_seconds < 0:
            raise ValueError("execution_cancel_timeout_seconds must not be negative")
        if execution_cancel_poll_seconds <= 0:
            raise ValueError("execution_cancel_poll_seconds must be positive")
        if execution_cancel_max_passes < 1:
            raise ValueError("execution_cancel_max_passes must be positive")
        if workflow_signal_timeout_seconds <= 0:
            raise ValueError("workflow_signal_timeout_seconds must be positive")
        self._engagement_repository = engagement_repository
        self._run_repository = run_repository
        self._event_repository = event_repository
        self._workflow_client = workflow_client
        self._execution_repository = execution_repository
        self._execution_runner = execution_runner
        self._workspace_root = workspace_root
        self._model_profiles = model_profiles
        self._workflow_signal_timeout_seconds = workflow_signal_timeout_seconds
        if safety_stopper is not None and resource_stoppers is not None:
            raise ValueError("provide either safety_stopper or resource_stoppers, not both")
        self._safety_stopper = safety_stopper or RunSafetyStopService(
            execution_repository=execution_repository,
            execution_runner=execution_runner,
            resource_stoppers=resource_stoppers,
            execution_cancel_timeout_seconds=execution_cancel_timeout_seconds,
            execution_cancel_poll_seconds=execution_cancel_poll_seconds,
            execution_cancel_max_passes=execution_cancel_max_passes,
            # Standalone services may intentionally own only a subset. Both
            # production assemblies supply all controllers, while Temporal
            # cleanup injects a strict RunSafetyStopService directly.
            require_all_resource_stoppers=False,
        )

    async def create_run(self, command: CreateRun) -> Run:
        model_profile = command.model_profile
        if self._model_profiles is not None:
            model_profile = await self._model_profiles.resolve_profile(model_profile)
        engagement = await self._resolve_engagement(command)
        run = Run(
            engagement_id=engagement.id,
            node_id=command.node_id,
            kind=RunKind.GENERAL,
            objective=Objective(description=command.objective),
            success_criteria=command.success_criteria,
            entry_points=command.entry_points,
            scope=command.scope,
            approval_mode=command.approval_mode,
            model_profile=model_profile,
            workspace_path=command.workspace_path or "",
            # Creating a Run is deliberately conversation-only.  Temporal is
            # started atomically with the first persisted user message, so a
            # Control Plane can accept and display a scoped task even while
            # Temporal is offline.
            status=RunStatus.WAITING_USER,
        )
        if not run.workspace_path:
            run.workspace_path = str(self._workspace_root / run.id)
        workspace = await asyncio.to_thread(lambda: Path(run.workspace_path).expanduser().resolve())
        try:
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise ApplicationConflictError(
                "workspace_unavailable",
                f"Unable to create Run workspace {str(workspace)!r}: {exc}",
                details={"workspace_path": str(workspace)},
            ) from exc
        run.workspace_path = str(workspace)
        run.temporal_workflow_id = self._workflow_client.workflow_id(run.id)
        await self._run_repository.create(run)
        await self._event_repository.append(
            run.id,
            "conversation.context_ready",
            {
                "session_id": f"{run.id}:primary",
                "status": run.status.value,
                "objective": run.objective.description,
                "success_criteria": [item.model_dump(mode="json") for item in run.success_criteria],
                "entry_points": [item.model_dump(mode="json") for item in run.entry_points],
                "scope": run.scope.model_dump(mode="json"),
                "approval_mode": run.approval_mode.value,
                "model_profile": run.model_profile,
                "agent_started": False,
            },
        )
        return run

    async def get_run(self, run_id: str) -> Run:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        kind: RunKind = RunKind.GENERAL,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Run]:
        return list(
            await self._run_repository.list(
                status=status,
                kind=kind,
                limit=limit,
                offset=offset,
            )
        )

    async def list_runs_for_reconciliation(
        self,
        *,
        status: RunStatus,
        created_through: datetime,
        after_created_at: datetime | None = None,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[Run]:
        return list(
            await self._run_repository.list_for_reconciliation(
                status=status,
                created_through=created_through,
                after_created_at=after_created_at,
                after_id=after_id,
                limit=limit,
            )
        )

    async def stop_resources_for_cleanup(self, run_id: str) -> SafetyStopResult:
        """Reconcile a fenced normal-finalization stop in the owner process.

        Temporal performs the authoritative final transition. The Control
        Plane calls this method in the background so in-process Browser and
        Target HTTP handles are stopped by their actual owner, while the
        Worker independently requires the same durable stop evidence.
        """

        run = await self.get_run(run_id)
        if run.status not in {
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.COMPLETING,
        }:
            raise ApplicationConflictError(
                "run_cleanup_not_fenced",
                f"Cannot reconcile cleanup for run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        result = await self._stop_run_resources(run.id, drain=True)
        if not result.succeeded:
            return result

        # PAUSING/CANCELLING can survive the original API request when an
        # owner was temporarily unable to acknowledge a physical stop. Once
        # the background owner reconciler obtains complete evidence it must
        # also finish the control transition; merely stopping handles would
        # leave the Run permanently fenced and its Workflow unsignalled.
        current = await self.get_run(run.id)
        workflow_synced = current.started_at is None
        finalization_intent: RunFinalizationIntent | None = None
        if current.status is RunStatus.PAUSING:
            workflow_synced = await self._invoke_workflow_best_effort_if_started(
                current,
                "reconcile pause",
                self._workflow_client.pause,
            )
            current = await self._transition_if_possible(current, RunStatus.PAUSED)
        elif current.status is RunStatus.CANCELLING:
            current = await self._transition_if_possible(current, RunStatus.CANCELLED)
            workflow_synced = await self._invoke_workflow_best_effort_if_started(
                current,
                "reconcile cancel",
                self._workflow_client.cancel,
            )
        elif current.status is RunStatus.COMPLETING:
            finalization_intent = await self._get_finalization_intent(current.id)
            if finalization_intent is None:
                # Old fences without a trusted target remain fail-closed. A
                # guessed terminal status would be worse than a visible
                # COMPLETING Run that an operator can inspect.
                raise ApplicationConflictError(
                    "run_finalization_intent_missing",
                    f"Run {current.id!r} has no trustworthy finalization target",
                    details={"run_id": current.id},
                )
            current = await self._commit_finalization_if_possible(
                current.id,
                finalization_intent,
            )
            workflow_synced = current.started_at is None
        else:
            return result

        payload = self._stop_event_payload(result, workflow_synced=workflow_synced)
        payload["status"] = current.status.value
        if finalization_intent is not None:
            payload["finalization_target"] = finalization_intent.target.value
        await self._event_repository.append(run.id, "run.cleanup_reconciled", payload)
        return result

    async def _get_finalization_intent(self, run_id: str) -> RunFinalizationIntent | None:
        try:
            return await self._run_repository.get_finalization_intent(run_id)
        except RepositoryConflictError as exc:
            raise ApplicationConflictError(
                "run_finalization_intent_invalid",
                f"Run {run_id!r} has no trustworthy finalization target",
                details={"run_id": run_id, "reason": str(exc)},
            ) from exc

    async def pause(self, run_id: str) -> Run:
        run = await self._require_pauseable_run(run_id)
        run = await self._transition_if_possible(run, RunStatus.PAUSING)
        stop_result = await self._stop_run_resources(run.id, drain=True)
        pause_fence_acquired = run.status in {RunStatus.PAUSING, RunStatus.PAUSED}
        workflow_synced = True
        if pause_fence_acquired:
            workflow_synced = await self._invoke_workflow_best_effort_if_started(
                run,
                "pause",
                self._workflow_client.pause,
            )
        payload = self._stop_event_payload(stop_result, workflow_synced=workflow_synced)
        payload["pause_fence_acquired"] = pause_fence_acquired
        if not pause_fence_acquired:
            payload["superseded_by_status"] = run.status.value
        await self._event_repository.append(
            run.id,
            "run.pause_requested",
            payload,
        )
        self._raise_if_stop_failed(run, stop_result)
        current = await self.get_run(run.id)
        return await self._transition_if_possible(current, RunStatus.PAUSED)

    async def resume(self, run_id: str) -> Run:
        run = await self._require_safety_controllable_run(run_id, action="resume")
        if run.status is RunStatus.PAUSING:
            # PAUSING may be the durable remainder of a failed safety stop. Do
            # not turn it into PAUSED/RUNNING merely because the operator asks
            # to resume: first rerun the complete stop gate and require fresh
            # affirmative evidence for every known effect.
            run = await self.pause(run.id)
        if run.status is not RunStatus.PAUSED:
            raise ApplicationConflictError(
                "run_not_paused",
                f"Cannot resume run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        finalization_intent = await self._get_finalization_intent(run.id)
        if finalization_intent is not None:
            if finalization_intent.target is not RunStatus.FAILED:
                raise ApplicationConflictError(
                    "run_finalization_pending",
                    f"Cannot resume run {run.id!r} while it is finalizing as "
                    f"{finalization_intent.target.value}",
                    details={
                        "run_id": run.id,
                        "status": run.status.value,
                        "finalization_target": finalization_intent.target.value,
                    },
                )
            return await self._resume_failed_finalization(run, finalization_intent)
        # ``started_at`` is the authoritative local boundary between a
        # conversation-only Run and an executing Run.  In particular, legacy
        # databases may contain a ``workflow.started`` audit event even though
        # no Temporal execution was ever created.  Resuming a pre-instruction
        # Run must therefore be a local state change and must not connect to
        # Temporal (or accidentally depend on that stale marker).
        conversation_only = run.started_at is None
        resume_status = RunStatus.WAITING_USER if conversation_only else RunStatus.RUNNING
        try:
            run = await self._run_repository.update_status(run.id, resume_status)
        except RepositoryConflictError:
            current = await self.get_run(run.id)
            raced_intent = await self._get_finalization_intent(run.id)
            if (
                current.status is RunStatus.PAUSED
                and raced_intent is not None
                and raced_intent.target is RunStatus.FAILED
            ):
                return await self._resume_failed_finalization(current, raced_intent)
            raise
        if conversation_only:
            # A conversation-only Run has no Temporal execution to resume. Its
            # durable local state is authoritative until the first user
            # message performs signal-with-start.
            await self._event_repository.append(run.id, "run.resume_requested")
            return run
        try:
            await self._invoke_workflow(run, "resume", self._workflow_client.resume)
        except Exception as resume_failure:
            current = await self.get_run(run.id)
            if current.status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                # Temporal signal delivery is ambiguous on transport failure:
                # the Workflow may already be unpaused and may have admitted a
                # new effect. Never roll the local status straight back to
                # PAUSED. Re-run the complete physical-stop gate and expose its
                # failure if any effect cannot be affirmatively stopped.
                try:
                    await self.pause(current.id)
                except Exception as stop_failure:
                    raise stop_failure from resume_failure
            raise
        current = await self.get_run(run.id)
        if current.status in {
            RunStatus.PAUSING,
            RunStatus.PAUSED,
            RunStatus.COMPLETING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
        }:
            # A safety control can win after the local PAUSED -> RUNNING
            # transition but before Temporal accepts this resume signal. The
            # stale signal may therefore have cleared Workflow._paused after
            # the Control Plane established its durable fence. Re-assert pause
            # immediately; local effect admission remains closed throughout,
            # and full cancellation will send its terminal signal only after
            # every physical stop is affirmatively acknowledged.
            workflow_resuspended = True
            if current.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
            }:
                workflow_resuspended = await self._invoke_workflow_best_effort_if_started(
                    current,
                    "re-pause a concurrently superseded resume",
                    self._workflow_client.pause,
                )
            await self._event_repository.append(
                run.id,
                "run.resume_superseded",
                {
                    "status": current.status.value,
                    "workflow_resuspended": workflow_resuspended,
                },
            )
            raise ApplicationConflictError(
                "run_resume_superseded",
                f"Could not resume run {run.id!r} because a concurrent safety control "
                "took precedence",
                details={
                    "run_id": run.id,
                    "status": current.status.value,
                    "workflow_resuspended": workflow_resuspended,
                },
            )
        await self._event_repository.append(run.id, "run.resume_requested")
        return current

    async def _resume_failed_finalization(
        self,
        run: Run,
        intent: RunFinalizationIntent,
    ) -> Run:
        """Converge a failure intent left by a paused legacy Workflow."""

        try:
            fenced = await self._run_repository.fence_finalization(
                run.id,
                intent.target,
                defer_cleanup_event=intent.defer_cleanup_event,
            )
        except (InvalidStateTransitionError, RepositoryConflictError) as exc:
            current = await self.get_run(run.id)
            if current.status is not intent.target:
                raise ApplicationConflictError(
                    "run_finalization_conflict",
                    f"Could not resume failure finalization for run {run.id!r}",
                    details={
                        "run_id": run.id,
                        "status": current.status.value,
                        "finalization_target": intent.target.value,
                    },
                ) from exc
            return current

        stop_result = await self._stop_run_resources(fenced.id, drain=True)
        self._raise_if_stop_failed(fenced, stop_result)
        current = await self._commit_finalization_if_possible(fenced.id, intent)
        if current.status is not intent.target:
            raise ApplicationConflictError(
                "run_finalization_conflict",
                f"Could not resume failure finalization for run {run.id!r}",
                details={
                    "run_id": run.id,
                    "status": current.status.value,
                    "finalization_target": intent.target.value,
                },
            )
        workflow_synced = await self._invoke_workflow_best_effort_if_started(
            current,
            "resume failed cleanup",
            self._workflow_client.resume,
        )
        payload = self._stop_event_payload(stop_result, workflow_synced=workflow_synced)
        payload.update(
            {
                "status": current.status.value,
                "finalization_target": intent.target.value,
                "trigger": "resume",
            }
        )
        await self._event_repository.append(current.id, "run.cleanup_reconciled", payload)
        return current

    async def cancel_current_execution(self, run_id: str) -> Run:
        # Safety controls must remain available for a terminal Run because a
        # crashed Workflow can leave an orphaned host process behind.
        run = await self.get_run(run_id)
        stop_result = await self._stop_run_resources(run.id, drain=False)
        # Releasing the Workflow's waiting Execution while its physical stop is
        # unconfirmed would let the Agent continue and produce more effects next
        # to the still-running process.  Keep the Workflow blocked until every
        # current effect has affirmative stop evidence.
        workflow_synced = run.started_at is None
        if stop_result.succeeded:
            workflow_synced = await self._invoke_workflow_best_effort_if_started(
                run,
                "cancel the current execution",
                self._workflow_client.cancel_current_execution,
            )
        await self._event_repository.append(
            run.id,
            "execution.cancel_requested",
            self._stop_event_payload(stop_result, workflow_synced=workflow_synced),
        )
        self._raise_if_stop_failed(run, stop_result)
        return await self.get_run(run.id)

    async def cancel(self, run_id: str) -> Run:
        # Full cancellation is also an orphan-process recovery operation, so it
        # intentionally remains callable after the Workflow/Run became terminal.
        run = await self.get_run(run_id)
        if run.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            run = await self._transition_if_possible(run, RunStatus.CANCELLING)
        stop_result = await self._stop_run_resources(run.id, drain=True)
        # Temporal cleanup is not the authority for a physical safety stop.  In
        # particular, its cleanup Activity cannot prove that Browser and Target
        # HTTP effects on remote Runners have stopped.  Only notify the Workflow
        # after every effect controller returned affirmative stop evidence, and
        # make the locally fenced Run terminal first.  This also prevents an
        # in-flight Workflow cleanup from turning an unconfirmed CANCELLING Run
        # into a falsely reassuring CANCELLED Run.
        workflow_synced = run.started_at is None
        current = await self.get_run(run.id)
        if stop_result.succeeded:
            if current.status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                current = await self._transition_if_possible(current, RunStatus.CANCELLING)
                current = await self._transition_if_possible(current, RunStatus.CANCELLED)
            workflow_synced = await self._invoke_workflow_best_effort_if_started(
                run,
                "cancel",
                self._workflow_client.cancel,
            )
        await self._event_repository.append(
            run.id,
            "run.cancel_requested",
            self._stop_event_payload(stop_result, workflow_synced=workflow_synced),
        )
        self._raise_if_stop_failed(run, stop_result)
        return current

    async def compact(self, run_id: str, *, max_history_items: int = 100) -> Run:
        run = await self._require_controllable_run(run_id, action="compact context for")
        await self._invoke_workflow(
            run,
            "compact context",
            lambda target: self._workflow_client.compact(target, max_history_items),
        )
        await self._event_repository.append(
            run.id,
            "agent.context_compaction_requested",
            {"max_history_items": max_history_items},
        )
        return run

    async def switch_model(self, run_id: str, model_profile: str) -> Run:
        run = await self._require_controllable_run(run_id, action="switch the model for")
        normalized = model_profile.strip()
        if not normalized:
            raise ApplicationConflictError(
                "empty_model_profile",
                "A model profile must not be empty",
            )
        if self._model_profiles is not None:
            normalized = await self._model_profiles.resolve_profile(normalized)
        await self._invoke_workflow(
            run,
            "switch model",
            lambda target: self._workflow_client.switch_model(target, normalized),
        )
        await self._event_repository.append(
            run.id,
            "agent.model_switch_requested",
            {"model_profile": normalized},
        )
        return run

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        message_event_id: str | None = None,
    ) -> Run:
        run = await self._require_controllable_run(run_id, action="send a message to")
        if run.status in {
            RunStatus.PAUSING,
            RunStatus.COMPLETING,
            RunStatus.CANCELLING,
        }:
            raise ApplicationConflictError(
                "run_not_controllable",
                f"Cannot send a message to run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        normalized = message.strip()
        if not normalized:
            raise ApplicationConflictError(
                "empty_message",
                "A run message must not be empty",
            )
        try:
            event = await self._event_repository.append_user_message(
                run.id,
                normalized,
                event_id=message_event_id,
            )
        except RepositoryConflictError as exc:
            # Pause/cancel/completion fences and message append serialize on
            # the same Run row. Re-read after a lost race so the API returns an
            # explicit lifecycle conflict instead of signalling an instruction
            # after effect admission has closed.
            current = await self.get_run(run.id)
            if current.status in {
                RunStatus.PAUSING,
                RunStatus.COMPLETING,
                RunStatus.CANCELLING,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                raise ApplicationConflictError(
                    "run_not_controllable",
                    f"Cannot send a message to run {run.id!r} while it is {current.status.value}",
                    details={"run_id": run.id, "status": current.status.value},
                ) from exc
            if message_event_id is not None:
                raise ApplicationConflictError(
                    "message_retry_conflict",
                    "The requested message retry does not match a queued message for this Run",
                    details={
                        "run_id": run.id,
                        "message_event_id": message_event_id,
                    },
                ) from exc
            raise
        if message_event_id is not None:
            if (
                event.run_id != run.id
                or event.event_type != "user.message_queued"
                or event.payload.get("message") != normalized
            ):
                raise ApplicationConflictError(
                    "message_retry_conflict",
                    "The requested message retry does not match a queued message for this Run",
                    details={
                        "run_id": run.id,
                        "message_event_id": message_event_id,
                    },
                )
        try:
            await self._invoke_workflow(
                run,
                "send a message",
                lambda target: self._workflow_client.append_user_message(target, event.id),
            )
        except ApplicationServiceError as exc:
            # Temporal transport errors are delivery-ambiguous: the server may
            # already have accepted Signal-With-Start. Return the durable event
            # ID so clients can explicitly retry that exact instruction. The
            # Workflow de-duplicates this ID even after consuming it.
            exc.details = {
                **exc.details,
                "message_event_id": event.id,
                "retry_same_message": True,
            }
            raise
        if not await self._workflow_was_started(run):
            await self._event_repository.append(
                run.id,
                "workflow.started",
                {
                    "workflow_id": run.temporal_workflow_id,
                    "trigger": "user_message",
                },
            )
        return run

    async def _resolve_engagement(self, command: CreateRun) -> Engagement:
        if command.engagement_id and command.engagement:
            raise ApplicationConflictError(
                "engagement_conflict",
                "Provide either engagement_id or engagement, not both",
            )
        if command.engagement_id:
            engagement = await self._engagement_repository.get(command.engagement_id)
            if engagement is None:
                raise EntityNotFoundError("Engagement", command.engagement_id)
            return engagement

        requested = command.engagement or CreateEngagement(name=f"Run: {command.objective[:80]}")
        engagement = Engagement(
            name=requested.name,
            description=requested.description,
            authorization_reference=requested.authorization_reference,
        )
        return await self._engagement_repository.create(engagement)

    async def _require_controllable_run(self, run_id: str, *, action: str) -> Run:
        run = await self.get_run(run_id)
        if run.status in _TERMINAL_RUN_STATUSES | _SAFETY_FENCE_RUN_STATUSES:
            raise ApplicationConflictError(
                "run_not_controllable",
                f"Cannot {action} run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run

    async def _require_safety_controllable_run(self, run_id: str, *, action: str) -> Run:
        """Keep recovery controls available while an admission fence is active."""

        run = await self.get_run(run_id)
        if run.status in _TERMINAL_RUN_STATUSES:
            raise ApplicationConflictError(
                "run_not_controllable",
                f"Cannot {action} run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run

    async def _require_pauseable_run(self, run_id: str) -> Run:
        run = await self._require_safety_controllable_run(run_id, action="pause")
        if run.status in {RunStatus.CANCELLING, RunStatus.COMPLETING}:
            raise ApplicationConflictError(
                "run_not_controllable",
                f"Cannot pause run {run.id!r} while it is {run.status.value}",
                details={"run_id": run.id, "status": run.status.value},
            )
        return run

    async def _invoke_workflow(
        self,
        run: Run,
        action: str,
        operation: Callable[[str], Awaitable[object]],
    ) -> None:
        try:
            await operation(run.id)
        except ApplicationServiceError:
            raise
        except Exception as exc:
            raise ApplicationConflictError(
                "workflow_control_failed",
                f"Could not {action} because the Workflow rejected the control request",
                details={
                    "run_id": run.id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            ) from exc

    async def _invoke_workflow_best_effort(
        self,
        run: Run,
        action: str,
        operation: Callable[[str], Awaitable[object]],
    ) -> bool:
        try:
            # Physical effect termination is authoritative.  A disconnected
            # Temporal endpoint must never keep an emergency-stop HTTP request
            # waiting on the SDK's transport retry horizon after local/remote
            # resources have already been fenced and stopped.
            await asyncio.wait_for(
                self._invoke_workflow(run, action, operation),
                timeout=self._workflow_signal_timeout_seconds,
            )
        except Exception as exc:
            payload: dict[str, object] = {
                "action": action,
                "workflow_id": run.temporal_workflow_id,
                "error_type": type(exc).__name__,
                "reason": (
                    str(exc)
                    if not isinstance(exc, TimeoutError)
                    else "Temporal workflow signal exceeded the safety-control deadline"
                ),
            }
            if isinstance(exc, ApplicationServiceError):
                payload["error_code"] = exc.code
                payload["details"] = exc.details
            await self._event_repository.append(run.id, "workflow.signal_failed", payload)
            return False
        return True

    async def _invoke_workflow_best_effort_if_started(
        self,
        run: Run,
        action: str,
        operation: Callable[[str], Awaitable[object]],
    ) -> bool:
        # Before RUNNING is durably observed, local status fencing is both the
        # source of truth and the safer control path.  A signal-with-start may
        # have been accepted while its first Workflow task is still queued; if
        # pause signalled that execution but a local resume did not (by design),
        # it would remain paused forever.  The preparation activities re-read
        # the Run and cannot enter RUNNING from PAUSING/PAUSED/CANCELLING, while
        # Runner cancellation below still handles any anomalous orphan process.
        if run.started_at is None:
            return True
        return await self._invoke_workflow_best_effort(run, action, operation)

    async def _workflow_was_started(self, run: Run) -> bool:
        # Once RUNNING has been observed, started_at is the cheapest durable
        # proof. The event also covers old conversation-first Workflows which
        # were started before their first instruction and therefore still had
        # no started_at timestamp.
        if run.started_at is not None:
            return True
        after_sequence = 0
        while True:
            page = list(
                await self._event_repository.list_after(
                    run.id,
                    after_sequence=after_sequence,
                    limit=_EVENT_LIST_PAGE_SIZE,
                )
            )
            if any(event.event_type == "workflow.started" for event in page):
                return True
            if len(page) < _EVENT_LIST_PAGE_SIZE:
                return False
            after_sequence = page[-1].sequence

    async def _stop_run_resources(
        self,
        run_id: str,
        *,
        drain: bool,
    ) -> SafetyStopResult:
        return await self._safety_stopper.stop_run(run_id, drain=drain)

    async def _transition_if_possible(self, run: Run, target: RunStatus) -> Run:
        current = await self.get_run(run.id)
        if current.status is target:
            return current
        if not current.can_transition_to(target):
            return current
        try:
            return await self._run_repository.update_status(current.id, target)
        except (InvalidStateTransitionError, RepositoryConflictError):
            # The repository performs the authoritative transition from a
            # freshly locked row. A competing terminal/fence transition may
            # win after the optimistic read above. Treat that expected lost
            # race as "no longer possible" so pause/cancel callers still run
            # their physical-stop sweep instead of aborting before cleanup.
            raced = await self.get_run(current.id)
            if raced.status is target or not raced.can_transition_to(target):
                return raced
            raise

    async def _commit_finalization_if_possible(
        self,
        run_id: str,
        intent: RunFinalizationIntent,
    ) -> Run:
        """Atomically terminalize, while preserving a concurrent control winner."""

        try:
            return await self._run_repository.commit_finalization(
                run_id,
                intent.target,
                defer_cleanup_event=intent.defer_cleanup_event,
            )
        except (InvalidStateTransitionError, RepositoryConflictError):
            raced = await self.get_run(run_id)
            if raced.status in {
                RunStatus.PAUSING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.CANCELLED,
            } or (
                raced.status in {RunStatus.COMPLETED, RunStatus.FAILED}
                and raced.status is not intent.target
            ):
                return raced
            # A collision or malformed/missing intent while the requested
            # target owns the Run is not a benign race. Keep it retry-visible.
            raise

    @staticmethod
    def _stop_event_payload(
        result: SafetyStopResult,
        *,
        workflow_synced: bool,
    ) -> dict[str, object]:
        execution = result.resources["executions"]
        return {
            # Preserve the original execution fields for older API/CLI/Web
            # clients while exposing all effect families in one disposition.
            "execution_ids": list(execution.attempted_ids),
            "execution_nodes": execution.node_ids,
            "execution_statuses": execution.observed_statuses,
            "confirmed_execution_ids": list(execution.confirmed_ids),
            "confirmed_statuses": execution.confirmed_statuses,
            "failed_executions": execution.failures,
            "stop_resources": RunApplicationService._stop_resources_payload(result),
            "failed_resource_types": list(result.failed_resource_types),
            "workflow_synced": workflow_synced,
        }

    @staticmethod
    def _stop_resources_payload(result: SafetyStopResult) -> dict[str, object]:
        return stop_resources_payload(result)

    @staticmethod
    def _raise_if_stop_failed(run: Run, result: SafetyStopResult) -> None:
        if result.succeeded:
            return
        execution = result.resources["executions"]
        only_executions_failed = result.failed_resource_types == ("executions",)
        raise ServiceUnavailableError(
            "execution_cancel_failed" if only_executions_failed else "safety_stop_failed",
            (
                "Could not confirm that every execution requiring a safety stop was stopped"
                if only_executions_failed
                else "Could not confirm that every active effect was safely stopped"
            ),
            details={
                "run_id": run.id,
                "execution_ids": list(execution.attempted_ids),
                "execution_nodes": execution.node_ids,
                "execution_statuses": execution.observed_statuses,
                "confirmed_execution_ids": list(execution.confirmed_ids),
                "confirmed_statuses": execution.confirmed_statuses,
                "failed_executions": execution.failures,
                "stop_resources": RunApplicationService._stop_resources_payload(result),
                "failed_resource_types": list(result.failed_resource_types),
            },
        )
