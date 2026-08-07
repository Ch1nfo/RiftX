"""Runtime bridge from durable Tool Call intent to deferred Runner Execution."""

from __future__ import annotations

import hashlib
import json
import platform
import shlex
from collections.abc import Collection
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import (
    ExecutionAdmissionIdentity,
    RunRepository,
    ToolCallIntentExecutionClaim,
    ToolCallIntentRepository,
)
from riftx.domain import ApprovalLevel, Execution, ExecutorType, Run, RunKind
from riftx.executors import EnvironmentMode, ShellKind
from riftx.runner import TerminalLaunchRequest
from riftx.runtime.engine import AgentEngineEvent
from riftx.runtime.types import AgentCycle, AgentSession, AgentStep, ToolCallIntent, ToolCallStatus
from riftx.scope import ScopeGuard
from riftx.tools import (
    ExecutionPolicy,
    PinnedToolSnapshot,
    ToolContextManager,
    ToolRegistry,
)
from riftx.tools.policy import (
    AGENT_TOOL_POLICIES,
    AgentToolAuthorization,
)

from .models import SubmitExecutionRequest, build_execution_key
from .service import ExecutionService


class DeferredExecutionSpec(BaseModel):
    """Runner launch data supplied by the Tool Proxy after policy resolution."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    executor_type: ExecutorType
    cwd: Path
    argv: list[str] = Field(default_factory=list)
    command_text: str | None = None
    tool_version: str | None = None
    shell: ShellKind | None = None
    shell_path: Path | None = None
    environment_mode: EnvironmentMode = EnvironmentMode.INHERIT
    env: dict[str, str | None] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    attempt_group: str = Field(default="initial", min_length=1, max_length=64)
    target_summary: str | None = Field(default=None, min_length=1, max_length=8192)


class _PortScanArguments(BaseModel):
    """Structured port-scan arguments; raw argv is forbidden for Pentest targets."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str | None = Field(default=None, min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=8192)
    ports: str | None = Field(default=None, min_length=1, max_length=2048)
    service_detection: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=2000)


class DeferredExecutionResolver(Protocol):
    async def resolve(
        self,
        *,
        session: AgentSession,
        event: AgentEngineEvent,
        tool_id: str,
    ) -> DeferredExecutionSpec: ...


class RegistryDeferredExecutionResolver:
    """Resolve model-visible tool arguments to trusted Runner launch data."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        registry: ToolRegistry,
        tool_context: ToolContextManager | None = None,
    ) -> None:
        self._runs = runs
        self._registry = registry
        self._tool_context = tool_context

    async def resolve(
        self,
        *,
        session: AgentSession,
        event: AgentEngineEvent,
        tool_id: str,
    ) -> DeferredExecutionSpec:
        run = await self._runs.get(session.run_id)
        if run is None:
            raise EntityNotFoundError("Run", session.run_id)
        _require_deferred_effect_policy(
            run,
            operation="service.deferred_execution.prepare",
            effect="durable_write",
        )
        pinned: PinnedToolSnapshot | None = None
        tool_version: str | None
        if self._tool_context is not None:
            authorization_check = (
                self._tool_context.assert_allowed
                if tool_id == "run_shell"
                else self._tool_context.assert_selected
            )
            pinned = await authorization_check(
                tool_id,
                run_id=session.run_id,
                session_id=session.id,
                agent_id=session.agent_type,
            )
        arguments = _arguments(event.data)
        cwd = _bounded_cwd(run.workspace_path, arguments.get("cwd"))
        if tool_id == "run_shell":
            if self._registry.config.execution_policy is not ExecutionPolicy.OPEN:
                raise ApplicationConflictError(
                    "shell_execution_disabled",
                    "run_shell is unavailable under registered_only execution policy",
                )
            script = str(arguments.get("script") or "").strip()
            if not script:
                raise ApplicationConflictError(
                    "invalid_tool_arguments",
                    "run_shell requires a non-empty script",
                )
            shell_path = Path(_default_shell_path(self._registry))
            return DeferredExecutionSpec(
                node_id=run.node_id,
                executor_type=ExecutorType.SHELL,
                cwd=cwd,
                command_text=script,
                shell=_shell_kind(shell_path),
                shell_path=shell_path,
                environment_mode=EnvironmentMode.INHERIT,
                env=_environment(arguments),
                timeout_seconds=_timeout(arguments),
            )

        if self._tool_context is not None:
            assert pinned is not None
            definition = pinned.definition
            resolved_command = pinned.resolved_command
            tool_version = pinned.version
        else:
            definition = self._registry.get_available(tool_id)
            state = self._registry.snapshot.states[tool_id]
            resolved_command = state.resolved_command or definition.command[0]
            tool_version = state.version
        target_summary: str | None = None
        if run.kind is RunKind.PENTEST and "port_scan" in definition.capabilities:
            try:
                scan = _PortScanArguments.model_validate(arguments)
            except ValidationError as exc:
                raise ApplicationConflictError(
                    "invalid_tool_arguments",
                    "Pentest port scans require structured target, ports, and "
                    "service_detection arguments",
                ) from exc
            ScopeGuard(run.scope).require(scan.target)
            argv = _port_scan_arguments(
                definition.id,
                target=scan.target,
                ports=scan.ports,
                service_detection=scan.service_detection,
            )
            timeout = scan.timeout_seconds or definition.timeout_seconds
            target_summary = scan.target
        else:
            args = arguments.get("args")
            argv = [str(item) for item in args] if isinstance(args, list) else []
            timeout = _timeout(arguments) or definition.timeout_seconds
        return DeferredExecutionSpec(
            node_id=run.node_id,
            executor_type=definition.executor,
            cwd=cwd,
            argv=[resolved_command, *definition.command[1:], *argv],
            tool_version=tool_version,
            environment_mode=EnvironmentMode.INHERIT,
            env={**definition.environment, **_environment(arguments)},
            timeout_seconds=timeout,
            target_summary=target_summary,
        )


class DeferredExecutionDispatcher:
    """Persist a stable Tool Call and submit exactly one deferred Execution."""

    def __init__(
        self,
        *,
        tool_call_repository: ToolCallIntentRepository,
        execution_service: ExecutionService,
        resolver: DeferredExecutionResolver | None = None,
    ) -> None:
        self._tool_calls = tool_call_repository
        self._executions = execution_service
        self._resolver = resolver
        self._runs = execution_service.run_repository

    async def dispatch(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
    ) -> Execution:
        intent = await self.prepare(
            session=session,
            cycle=cycle,
            step=step,
            event=event,
        )
        return await self.execute_intent(intent)

    async def pending_intents(self, session_id: str) -> list[ToolCallIntent]:
        """Return unresolved intents in their durable model-emission order."""

        return await self._tool_calls.pending_for_session(session_id)

    async def get_intent(self, intent_id: str) -> ToolCallIntent | None:
        return await self._tool_calls.get(intent_id)

    async def prepare(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
        status: ToolCallStatus = ToolCallStatus.READY,
    ) -> ToolCallIntent:
        """Resolve and persist the immutable execution snapshot without launching it."""
        _require_deferred_parent_owners(session, cycle, step)
        run = await self._require_run(session.run_id)
        _require_deferred_effect_policy(
            run,
            operation="service.deferred_execution.prepare",
            effect="durable_write",
        )
        call_id = _required_string(event.data, "call_id")
        tool_id = _tool_id(event.data)
        raw_spec = event.data.get("execution")
        if self._resolver is not None:
            spec = await self._resolver.resolve(
                session=session,
                event=event,
                tool_id=tool_id,
            )
        elif isinstance(raw_spec, dict):
            spec = DeferredExecutionSpec.model_validate(raw_spec)
        else:
            raise ApplicationConflictError(
                "deferred_execution_missing",
                f"Tool Call {call_id!r} is missing resolved execution data",
            )
        return await self._persist_intent(
            session=session,
            cycle=cycle,
            step=step,
            event=event,
            call_id=call_id,
            tool_id=tool_id,
            spec=spec,
            status=status,
        )

    async def prepare_control(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
        status: ToolCallStatus = ToolCallStatus.WAITING_APPROVAL,
    ) -> ToolCallIntent:
        """Persist an SDK-resumable control mutation without a Runner spec."""

        _require_deferred_parent_owners(session, cycle, step)
        run = await self._require_run(session.run_id)
        _require_deferred_effect_policy(
            run,
            operation="service.deferred_execution.prepare",
            effect="durable_write",
        )
        call_id = _required_string(event.data, "call_id")
        tool_id = _tool_id(event.data)
        policy = AGENT_TOOL_POLICIES.get(tool_id)
        if (
            event.data.get("approval_policy") != "explicit"
            or policy is None
            or not policy.approval_required
            or policy.authorization is not AgentToolAuthorization.DYNAMIC_APPROVAL
        ):
            raise ApplicationConflictError(
                "control_tool_approval_policy_invalid",
                "Control Tool is not authorized for explicit approval execution",
            )
        return await self._persist_intent(
            session=session,
            cycle=cycle,
            step=step,
            event=event,
            call_id=call_id,
            tool_id=tool_id,
            spec=None,
            status=status,
        )

    async def begin_control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        attempt_group: str = "control",
        target_interaction_tool_ids: Collection[str] | None = None,
    ) -> ToolCallIntent | None:
        """Claim one approved provider-control mutation immediately before execution."""

        intent = await self._control_intent(
            run_id=run_id,
            session_id=session_id,
            engine_call_id=engine_call_id,
        )
        if intent is None:
            return None
        mismatched_fields: list[str] = []
        if intent.tool_id != tool_name:
            mismatched_fields.append("tool_id")
        if _canonical_json(intent.arguments) != _canonical_json(arguments):
            mismatched_fields.append("arguments")
        if mismatched_fields:
            raise ApplicationConflictError(
                "control_tool_intent_mismatch",
                "Control Tool invocation does not match its approved durable intent",
                details={"mismatched_fields": mismatched_fields},
            )
        await self._require_resolved_intent_effect(
            intent,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        execution_key = build_execution_key(
            run_id=run_id,
            session_id=session_id,
            tool_call_id=intent.id,
            attempt_group=attempt_group,
        )
        claim = await self._tool_calls.claim_execution(
            intent.id,
            execution_key=execution_key,
            attempt_group=attempt_group,
            target_interaction_tool_ids=target_interaction_tool_ids,
        )
        if claim.newly_acquired:
            return claim.intent
        raise ApplicationConflictError(
            "control_tool_approval_not_ready",
            "Approved control Tool Call cannot claim exactly-once execution",
        )

    async def finish_control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
        succeeded: bool,
    ) -> None:
        intent = await self._control_intent(
            run_id=run_id,
            session_id=session_id,
            engine_call_id=engine_call_id,
        )
        if intent is None:
            return
        await self._require_resolved_intent_effect(
            intent,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        settled, changed = await self._tool_calls.compare_and_set_status(
            intent.id,
            expected={ToolCallStatus.EXECUTING},
            target=ToolCallStatus.COMPLETED if succeeded else ToolCallStatus.FAILED,
        )
        if not changed and settled.status not in {
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
        }:
            raise ApplicationConflictError(
                "control_tool_outcome_not_settled",
                "Control Tool Call outcome could not be durably settled",
            )

    async def _control_intent(
        self,
        *,
        run_id: str,
        session_id: str,
        engine_call_id: str,
    ) -> ToolCallIntent | None:
        for intent in await self._tool_calls.pending_for_session(session_id):
            if (
                intent.run_id == run_id
                and intent.engine_call_id == engine_call_id
                and intent.execution_spec is None
            ):
                return intent
        return None

    async def execute_intent(self, intent: ToolCallIntent) -> Execution:
        """Launch exactly the execution snapshot stored with an approved intent."""
        authoritative = await self._require_intent_effect(
            intent,
            operation="service.deferred_execution.dispatch",
            effect="host_execution",
        )
        if authoritative.execution_spec is None:
            raise ApplicationConflictError(
                "deferred_execution_missing",
                f"Tool Call {authoritative.id!r} has no persisted execution data",
            )
        if authoritative.tool_id is None:
            raise ApplicationConflictError(
                "invalid_tool_call_intent",
                f"Tool Call {authoritative.id!r} has no tool ID",
            )
        spec = DeferredExecutionSpec.model_validate(authoritative.execution_spec)
        return await self._executions.submit(
            SubmitExecutionRequest(
                run_id=authoritative.run_id,
                session_id=authoritative.session_id,
                tool_call_id=authoritative.id,
                attempt_group=spec.attempt_group,
                node_id=spec.node_id,
                executor_type=spec.executor_type,
                cwd=spec.cwd,
                argv=spec.argv,
                command_text=spec.command_text,
                tool_id=authoritative.tool_id,
                tool_version=spec.tool_version,
                shell=spec.shell,
                shell_path=spec.shell_path,
                environment_mode=spec.environment_mode,
                env=spec.env,
                timeout_seconds=spec.timeout_seconds,
            )
        )

    async def execute_approved_intent(self, intent_id: str) -> Execution:
        """Approve and execute a persisted snapshot without consulting the model again."""
        await self._require_intent_id_effect(
            intent_id,
            operation="service.deferred_execution.dispatch",
            effect="host_execution",
        )
        intent = await self.approve_intent(intent_id)
        return await self.execute_intent(intent)

    async def claim_intent_execution(
        self,
        intent: ToolCallIntent,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> ToolCallIntentExecutionClaim:
        """Acquire the exact durable claim used by a non-generic execution path."""

        authoritative = await self._require_intent_effect(
            intent,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        claim = await self._tool_calls.claim_execution(
            authoritative.id,
            execution_key=execution_key,
            attempt_group=attempt_group,
        )
        if claim.acquired:
            return claim
        raise ApplicationConflictError(
            "tool_call_not_ready",
            f"Tool Call intent cannot claim execution {execution_key!r} "
            f"from status {claim.intent.status.value!r}",
        )

    async def require_current_intent_execution_claim(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> None:
        if await self._tool_calls.execution_claim_is_current(
            intent_id,
            execution_key=execution_key,
            attempt_group=attempt_group,
        ):
            return
        raise ApplicationConflictError(
            "tool_call_execution_claim_lost",
            f"Tool Call {intent_id!r} no longer owns execution {execution_key!r}",
        )

    async def rollback_intent_execution_claim(
        self,
        claim: ToolCallIntentExecutionClaim,
        *,
        admission: ExecutionAdmissionIdentity,
    ) -> tuple[ToolCallIntent, bool]:
        authoritative = await self._require_durable_intent(claim.intent)
        _require_claim_admission_owners(authoritative, claim, admission)
        await self._require_resolved_intent_effect(
            authoritative,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        return await self._tool_calls.rollback_execution_claim(
            claim,
            admission=admission,
        )

    async def find_execution_admission(
        self,
        admission: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        return await self._executions.find_admission(admission)

    async def sync_intent_execution(
        self,
        intent: ToolCallIntent,
        execution: Execution,
    ) -> ToolCallIntent:
        authoritative = await self._require_durable_intent(intent)
        _require_intent_execution_owners(authoritative, execution)
        await self._require_resolved_intent_effect(
            authoritative,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        return await self._executions.sync_intent_execution(authoritative, execution)

    async def settle_failed_intent_execution_start(
        self,
        claim: ToolCallIntentExecutionClaim,
        *,
        launch_request: TerminalLaunchRequest,
    ) -> ToolCallIntent:
        """Keep any durable admission claimed; roll back only a row-less start."""

        authoritative = await self._require_durable_intent(claim.intent)
        if launch_request.execution_id is None or launch_request.session_id is None:
            raise ValueError("terminal settlement requires explicit session and execution IDs")
        execution_key = launch_request.execution_key or f"terminal:{launch_request.session_id}"
        if (
            execution_key != claim.execution_key
            or launch_request.run_id != authoritative.run_id
            or launch_request.agent_session_id != authoritative.session_id
            or launch_request.tool_call_id != authoritative.id
            or launch_request.attempt_group != claim.attempt_group
        ):
            raise ValueError("terminal launch request does not match the claimed Tool Call")
        await self._require_resolved_intent_effect(
            authoritative,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        admission = ExecutionAdmissionIdentity(
            execution_id=launch_request.execution_id,
            execution_key=execution_key,
            run_id=launch_request.run_id,
            session_id=launch_request.agent_session_id,
            tool_call_id=launch_request.tool_call_id,
            attempt_group=launch_request.attempt_group,
            executor_type=ExecutorType.PTY,
            node_id=launch_request.node_id,
            argv=tuple(launch_request.argv),
            command_text=None,
            tool_id=launch_request.tool_id,
            tool_version=launch_request.tool_version,
            cwd=str(launch_request.cwd),
            env=dict(launch_request.env),
            launch_fingerprint=launch_request.launch_fingerprint,
        )
        admitted = await self.find_execution_admission(admission)
        if admitted is not None:
            return await self.sync_intent_execution(authoritative, admitted)
        if not claim.newly_acquired:
            return authoritative
        authoritative, _ = await self.rollback_intent_execution_claim(
            claim,
            admission=admission,
        )
        # A same-identity starter may have registered its row while this
        # failure was settling. Never restore READY once that exact row is visible.
        admitted = await self.find_execution_admission(admission)
        if admitted is not None:
            return await self.sync_intent_execution(authoritative, admitted)
        return authoritative

    async def approve_intent(self, intent_id: str) -> ToolCallIntent:
        """Move an approved persisted intent to READY without launching it."""
        await self._require_intent_id_effect(
            intent_id,
            operation="service.deferred_execution.approve",
            effect="workflow_control",
        )
        intent, _ = await self._tool_calls.compare_and_set_status(
            intent_id,
            expected={
                ToolCallStatus.WAITING_APPROVAL,
                ToolCallStatus.READY,
            },
            target=ToolCallStatus.READY,
        )
        if intent.status is ToolCallStatus.REJECTED:
            raise ApplicationConflictError(
                "tool_call_rejected",
                f"Tool Call {intent.id!r} was rejected and cannot execute",
            )
        if intent.status is ToolCallStatus.PROPOSED:
            raise ApplicationConflictError(
                "tool_call_not_waiting_approval",
                f"Tool Call {intent.id!r} cannot be approved from {intent.status.value!r}",
            )
        return intent

    async def reject_intent(self, intent_id: str) -> ToolCallIntent:
        await self._require_intent_id_effect(
            intent_id,
            operation="service.deferred_execution.reject",
            effect="workflow_control",
        )
        intent, _ = await self._tool_calls.compare_and_set_status(
            intent_id,
            expected={
                ToolCallStatus.WAITING_APPROVAL,
                ToolCallStatus.REJECTED,
            },
            target=ToolCallStatus.REJECTED,
        )
        if intent.status is ToolCallStatus.REJECTED:
            return intent
        raise ApplicationConflictError(
            "tool_call_not_waiting_approval",
            f"Tool Call {intent.id!r} cannot be rejected from {intent.status.value!r}",
        )

    async def mark_intent_executing(self, intent: ToolCallIntent) -> ToolCallIntent:
        authoritative = await self._require_intent_effect(
            intent,
            operation="service.deferred_execution.mutation",
            effect="durable_write",
        )
        authoritative, _ = await self._tool_calls.compare_and_set_status(
            authoritative.id,
            expected={
                ToolCallStatus.READY,
                ToolCallStatus.EXECUTING,
            },
            target=ToolCallStatus.EXECUTING,
        )
        if authoritative.status is ToolCallStatus.EXECUTING:
            return authoritative
        raise ApplicationConflictError(
            "tool_call_not_ready",
            f"Tool Call {authoritative.id!r} cannot execute from {authoritative.status.value!r}",
        )

    async def _require_run(self, run_id: str) -> Run:
        if self._runs is None:
            raise ApplicationConflictError(
                "run_kind_effect_policy_denied",
                "Deferred execution effects require an authoritative Run repository",
            )
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        return run

    async def _require_durable_intent(self, intent: ToolCallIntent) -> ToolCallIntent:
        authoritative = await self._tool_calls.get(intent.id)
        if authoritative is None:
            raise EntityNotFoundError("ToolCallIntent", intent.id)
        _require_intent_owner_matches(authoritative, intent)
        return authoritative

    async def _require_intent_id_effect(
        self,
        intent_id: str,
        *,
        operation: str,
        effect: str,
    ) -> ToolCallIntent:
        intent = await self._tool_calls.get(intent_id)
        if intent is None:
            raise EntityNotFoundError("ToolCallIntent", intent_id)
        await self._require_resolved_intent_effect(
            intent,
            operation=operation,
            effect=effect,
        )
        return intent

    async def _require_intent_effect(
        self,
        intent: ToolCallIntent,
        *,
        operation: str,
        effect: str,
    ) -> ToolCallIntent:
        authoritative = await self._require_durable_intent(intent)
        await self._require_resolved_intent_effect(
            authoritative,
            operation=operation,
            effect=effect,
        )
        return authoritative

    async def _require_resolved_intent_effect(
        self,
        intent: ToolCallIntent,
        *,
        operation: str,
        effect: str,
    ) -> None:
        run = await self._require_run(intent.run_id)
        _require_deferred_effect_policy(
            run,
            operation=operation,
            effect=effect,
            intent_id=intent.id,
        )

    async def _persist_intent(
        self,
        *,
        session: AgentSession,
        cycle: AgentCycle,
        step: AgentStep,
        event: AgentEngineEvent,
        call_id: str,
        tool_id: str,
        spec: DeferredExecutionSpec | None,
        status: ToolCallStatus,
    ) -> ToolCallIntent:
        intent = ToolCallIntent(
            id=build_tool_call_intent_id(
                run_id=session.run_id,
                session_id=session.id,
                cycle_id=cycle.id,
                engine_call_id=call_id,
            ),
            run_id=session.run_id,
            session_id=session.id,
            cycle_id=cycle.id,
            step_id=step.id,
            tool_id=tool_id,
            arguments=_arguments(event.data),
            command_preview=(
                spec.command_text or shlex.join(spec.argv)
                if spec is not None
                else tool_id
            ),
            reason=str(event.data.get("reason") or ""),
            target_summary=(
                spec.target_summary
                if spec is not None and spec.target_summary is not None
                else _optional_string(event.data.get("target_summary"))
            ),
            approval_level=ApprovalLevel(
                str(event.data.get("approval_level") or ApprovalLevel.SENSITIVE.value)
            ),
            status=status,
            engine_call_id=call_id,
            execution_spec=(spec.model_dump(mode="json") if spec is not None else None),
        )
        existing = await self._tool_calls.get(intent.id)
        if existing is not None:
            _validate_existing_intent(existing, expected=intent)
            return existing

        legacy_id = _build_legacy_tool_call_intent_id(
            run_id=session.run_id,
            session_id=session.id,
            engine_call_id=call_id,
        )
        legacy = await self._tool_calls.get(legacy_id)
        if legacy is not None and legacy.cycle_id == cycle.id:
            _validate_existing_intent(legacy, expected=intent)
            return legacy

        try:
            return await self._tool_calls.create(intent)
        except RepositoryConflictError:
            raced = await self._tool_calls.get(intent.id)
            if raced is None:
                raise
            _validate_existing_intent(raced, expected=intent)
            return raced


def build_tool_call_intent_id(
    *,
    run_id: str,
    session_id: str,
    cycle_id: str,
    engine_call_id: str,
) -> str:
    identity = "\x1f".join((run_id, session_id, cycle_id, engine_call_id))
    return f"tool-call:v2:{hashlib.sha256(identity.encode()).hexdigest()}"


def _build_legacy_tool_call_intent_id(
    *,
    run_id: str,
    session_id: str,
    engine_call_id: str,
) -> str:
    identity = "\x1f".join((run_id, session_id, engine_call_id))
    return f"tool-call:v1:{hashlib.sha256(identity.encode()).hexdigest()}"


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ApplicationConflictError(
            "invalid_tool_call_event", f"Deferred Tool Call requires {key!r}"
        )
    return value


def _tool_id(data: dict[str, object]) -> str:
    value = data.get("tool_id") or data.get("name")
    if value == "run_registered_tool":
        requested = _arguments(data).get("tool_id")
        if isinstance(requested, str) and requested:
            value = requested
    if not isinstance(value, str) or not value:
        raise ApplicationConflictError(
            "invalid_tool_call_event", "Deferred Tool Call requires a tool ID"
        )
    return value


def _arguments(data: dict[str, object]) -> dict[str, object]:
    value = data.get("arguments")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApplicationConflictError(
                "invalid_tool_arguments", "Deferred Tool Call arguments are not valid JSON"
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    return {}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded_cwd(workspace_path: str, requested: object) -> Path:
    workspace = Path(workspace_path).expanduser().resolve()
    if requested is None or not str(requested).strip():
        return workspace
    candidate = Path(str(requested)).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(workspace):
        raise ApplicationConflictError(
            "execution_cwd_outside_workspace",
            "Deferred execution cwd must remain inside the Run workspace",
        )
    return candidate


def _environment(arguments: dict[str, object]) -> dict[str, str | None]:
    value = arguments.get("environment") or arguments.get("env")
    if not isinstance(value, dict):
        return {}
    return {str(key): None if item is None else str(item) for key, item in value.items()}


def _timeout(arguments: dict[str, object]) -> float | None:
    value = arguments.get("timeout_seconds")
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _port_scan_arguments(
    tool_id: str,
    *,
    target: str,
    ports: str | None,
    service_detection: bool,
) -> list[str]:
    normalized = tool_id.lower()
    if normalized == "nmap":
        arguments = ["-oX", "-"]
        if service_detection:
            arguments.append("-sV")
        if ports:
            arguments.extend(["-p", ports])
        arguments.append(target)
        return arguments
    if normalized == "masscan":
        arguments = [target, "-oJ", "-"]
        if ports:
            arguments.extend(["-p", ports])
        return arguments
    arguments = []
    if ports:
        arguments.extend(["--ports", ports])
    arguments.append(target)
    return arguments


def _default_shell_path(registry: ToolRegistry) -> str:
    shells = registry.config.shells.default
    system = platform.system().lower()
    if system == "darwin":
        return shells.macos
    if system == "windows":
        return shells.windows
    return shells.linux


def _shell_kind(path: Path) -> ShellKind:
    name = path.name.lower()
    if "powershell" in name or name == "pwsh.exe":
        return ShellKind.POWERSHELL
    if name in {"cmd", "cmd.exe"}:
        return ShellKind.CMD
    if "zsh" in name:
        return ShellKind.ZSH
    return ShellKind.BASH


def _validate_existing_intent(
    intent: ToolCallIntent,
    *,
    expected: ToolCallIntent,
) -> None:
    mismatched_fields = [
        field_name
        for field_name in (
            "run_id",
            "session_id",
            "cycle_id",
            "step_id",
            "tool_id",
            "skill_id",
            "command_preview",
            "reason",
            "target_summary",
            "approval_level",
            "engine_call_id",
        )
        if getattr(intent, field_name) != getattr(expected, field_name)
    ]
    if _canonical_json(intent.arguments) != _canonical_json(expected.arguments):
        mismatched_fields.append("arguments")
    if _canonical_json(intent.execution_spec) != _canonical_json(expected.execution_spec):
        mismatched_fields.append("execution_spec")
    if not mismatched_fields:
        return
    raise ApplicationConflictError(
        "tool_call_identity_mismatch",
        f"Persisted Tool Call {intent.id!r} does not match the deferred request",
        details={"mismatched_fields": sorted(mismatched_fields)},
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _require_deferred_parent_owners(
    session: AgentSession,
    cycle: AgentCycle,
    step: AgentStep,
) -> None:
    mismatched: list[str] = []
    if cycle.run_id != session.run_id:
        mismatched.append("cycle.run_id")
    if cycle.session_id != session.id:
        mismatched.append("cycle.session_id")
    if step.cycle_id != cycle.id:
        mismatched.append("step.cycle_id")
    if mismatched:
        raise ApplicationConflictError(
            "deferred_execution_identity_mismatch",
            "Session, Cycle, and Step do not share the same durable owner",
            details={"mismatched_fields": mismatched},
        )


def _require_intent_owner_matches(
    authoritative: ToolCallIntent,
    provided: ToolCallIntent,
) -> None:
    mismatched = [
        field_name
        for field_name in ("run_id", "session_id", "cycle_id", "step_id")
        if getattr(authoritative, field_name) != getattr(provided, field_name)
    ]
    if mismatched:
        raise ApplicationConflictError(
            "tool_call_identity_mismatch",
            "Tool Call intent does not match its durable owner",
            details={"mismatched_fields": mismatched},
        )


def _require_intent_execution_owners(
    intent: ToolCallIntent,
    execution: Execution,
) -> None:
    mismatched: list[str] = []
    if execution.run_id != intent.run_id:
        mismatched.append("execution.run_id")
    if execution.session_id != intent.session_id:
        mismatched.append("execution.session_id")
    if execution.tool_call_id != intent.id:
        mismatched.append("execution.tool_call_id")
    if mismatched:
        raise ApplicationConflictError(
            "execution_identity_mismatch",
            "Execution and Tool Call intent do not share the same durable owner",
            details={"mismatched_fields": mismatched},
        )


def _require_claim_admission_owners(
    intent: ToolCallIntent,
    claim: ToolCallIntentExecutionClaim,
    admission: ExecutionAdmissionIdentity,
) -> None:
    mismatched: list[str] = []
    if admission.run_id != intent.run_id:
        mismatched.append("admission.run_id")
    if admission.session_id != intent.session_id:
        mismatched.append("admission.session_id")
    if admission.tool_call_id != intent.id:
        mismatched.append("admission.tool_call_id")
    if admission.execution_key != claim.execution_key:
        mismatched.append("admission.execution_key")
    if admission.attempt_group != claim.attempt_group:
        mismatched.append("admission.attempt_group")
    if mismatched:
        raise ApplicationConflictError(
            "execution_identity_mismatch",
            "Execution admission does not match the claimed Tool Call owner",
            details={"mismatched_fields": mismatched},
        )


def _require_deferred_effect_policy(
    run: Run,
    *,
    operation: str,
    effect: str,
    intent_id: str | None = None,
) -> None:
    """Apply the catalog after durable parent and Run ownership are proven."""

    # Lazy imports avoid the catalog's managed-entrypoint validation cycle.
    from riftx.application.run_kind_effects import (
        EffectMode,
        EffectOrigin,
        PolicyDenialReason,
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    try:
        require_run_kind_effect_policy(
            operation,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=RunEffectOwnership(
                run_id=run.id,
                run_kind=run.kind,
                resource_kind=("tool_call_intent" if intent_id is not None else None),
                resource_id=intent_id,
            ),
            effect=effect,
            mode=EffectMode.NORMAL,
        )
    except RunKindEffectPolicyDenied as exc:
        code = (
            "run_kind_operation_unsupported"
            if exc.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED
            else "run_kind_effect_policy_denied"
        )
        raise ApplicationConflictError(
            code,
            "The requested deferred Execution effect is not admitted for this Run owner",
        ) from None
    except (TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested deferred Execution effect is not admitted for this Run owner",
        ) from None
