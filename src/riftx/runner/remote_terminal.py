"""Central routing and durable state for terminals hosted by remote Runners."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
)
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    TerminalRepository,
)
from riftx.application.services.runner_control import RunnerControlService
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerCommandKind,
    RunnerPrincipal,
    TerminalOwner,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id, utc_now

from .models import OutputSlice, TerminalLaunchRequest
from .paths import RunnerPaths
from .protocols import EffectGuard
from .supervisor import ProcessTerminationError
from .terminal import TerminalController
from .terminal_identity import require_terminal_start_replay_matches


class RemoteTerminalSupervisor:
    """Persist terminal state centrally and dispatch operations to a remote node."""

    def __init__(
        self,
        *,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        event_repository: RunEventRepository,
        control: RunnerControlService,
        paths: RunnerPaths,
    ) -> None:
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._events = event_repository
        self._control = control
        self._paths = paths

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TerminalSession:
        runner_principal = await self._control.current_principal(request.node_id)
        explicit_identity = request.session_id is not None
        session_id = request.session_id or new_id()
        execution_id = request.execution_id or new_id()
        request = request.model_copy(
            update={
                "session_id": session_id,
                "execution_id": execution_id,
                "runner_principal": runner_principal,
            }
        )
        self._paths.ensure_run_layout(request.run_id)
        terminal_paths = self._paths.terminal(request.run_id, session_id)
        terminal_paths.directory.mkdir(parents=True, exist_ok=True)
        terminal_paths.transcript.touch(exist_ok=True)
        execution = Execution(
            id=execution_id,
            execution_key=request.execution_key or f"terminal:{session_id}",
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=request.agent_session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            owner=runner_principal,
            executor_type=ExecutorType.PTY,
            argv=request.argv,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
        )
        # CREATED is explicit durable pre-dispatch proof.  The projection is
        # persisted first, then CREATED -> STARTING is the execution-ID CAS
        # token selecting the only caller allowed to enqueue the command.
        try:
            execution, created = await self._executions.create_if_absent(execution)
        except RepositoryConflictError as exc:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                str(exc),
                details={
                    "execution_id": execution.id,
                    "execution_key": execution.execution_key,
                },
            ) from exc
        terminal: TerminalSession | None = None
        if not created:
            _require_remote_owner(execution)
            if explicit_identity and execution.id != execution_id:
                raise ApplicationConflictError(
                    "execution_idempotency_conflict",
                    f"Terminal execution key {execution.execution_key!r} is already bound "
                    f"to execution ID {execution.id!r}",
                )
            self._require_exact_execution_replay(execution, request)
            existing_terminal = await self._terminals.get_by_execution(execution.id)
            if execution.status is ExecutionStatus.CREATED and execution.launch_fingerprint is None:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    existing_terminal,
                    expected={ExecutionStatus.CREATED},
                )
                raise ApplicationConflictError(
                    "execution_idempotency_conflict",
                    f"Legacy remote terminal execution {execution.id!r} has no durable "
                    "launch fingerprint for CREATED admission",
                    details={"execution_id": execution.id},
                )
            if existing_terminal is not None:
                self._require_exact_terminal_replay(
                    existing_terminal,
                    execution,
                    request,
                )
                if execution.status is ExecutionStatus.CREATED:
                    terminal = existing_terminal
                elif existing_terminal.status is TerminalStatus.CREATED:
                    if execution.physical_stop_confirmed_at is not None:
                        return await self._close_confirmed_terminal_projection(existing_terminal)
                    raise ApplicationConflictError(
                        "terminal_start_in_progress",
                        f"Terminal {existing_terminal.id!r} has a durable admission "
                        "whose dispatch outcome is not yet confirmed",
                        details={
                            "session_id": existing_terminal.id,
                            "execution_id": execution.id,
                            "execution_status": execution.status.value,
                        },
                    )
                if terminal is None:
                    if effect_guard is not None:
                        await effect_guard()
                    return existing_terminal
            if terminal is None and execution.status is ExecutionStatus.STARTING:
                # Rows written by the legacy STARTING -> Terminal ordering can
                # only lack a projection before enqueue.  Converge them to a
                # proven cancellation; never replay an ambiguous command.
                await self._shield_predispatch_cleanup(
                    execution.id,
                    None,
                    expected={ExecutionStatus.STARTING},
                )
                if execution.launch_fingerprint is None:
                    raise ApplicationConflictError(
                        "execution_idempotency_conflict",
                        f"Legacy remote terminal execution {execution.id!r} has no "
                        "durable session launch fingerprint",
                        details={"execution_id": execution.id},
                    )
                return await self._create_closed_terminal_projection(
                    terminal=TerminalSession(
                        id=session_id,
                        run_id=request.run_id,
                        execution_id=execution.id,
                        runner_id=request.node_id,
                        shell=request.argv[0],
                        cwd=str(request.cwd),
                        owner=request.owner,
                        cols=request.cols,
                        rows=request.rows,
                    ),
                    execution=await self._require_execution(execution.id),
                    request=request,
                )
            if terminal is None and execution.status is not ExecutionStatus.CREATED:
                raise ApplicationConflictError(
                    "terminal_execution_exists",
                    f"Terminal execution {execution.execution_key!r} has no session projection",
                )
        if terminal is None:
            terminal = TerminalSession(
                id=session_id,
                run_id=request.run_id,
                execution_id=execution.id,
                runner_id=request.node_id,
                shell=request.argv[0],
                cwd=str(request.cwd),
                owner=request.owner,
                cols=request.cols,
                rows=request.rows,
            )
            create_task = asyncio.create_task(
                self._terminals.create(terminal),
                name=f"riftx-remote-terminal-projection-create-{session_id}",
            )
            create_interrupted = await _wait_for_shielded_task(create_task)
            try:
                terminal = create_task.result()
            except BaseException as create_error:
                durable_terminal = await self._terminals.get_by_execution(execution.id)
                if durable_terminal is not None:
                    self._require_exact_terminal_replay(
                        durable_terminal,
                        execution,
                        request,
                    )
                    if not _is_terminal_projection_conflict(create_error):
                        latest = await self._require_execution(execution.id)
                        if latest.status is ExecutionStatus.CREATED:
                            await self._shield_predispatch_cleanup(
                                latest.id,
                                durable_terminal,
                                expected={ExecutionStatus.CREATED},
                            )
                    if create_interrupted:
                        raise asyncio.CancelledError from None
                    if _is_terminal_projection_conflict(create_error):
                        terminal = durable_terminal
                    else:
                        raise
                else:
                    await self._shield_predispatch_cleanup(
                        execution.id,
                        None,
                        expected={ExecutionStatus.CREATED},
                    )
                    if create_interrupted:
                        raise asyncio.CancelledError from None
                    raise

            if create_interrupted:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    terminal,
                    expected={ExecutionStatus.CREATED},
                )
                raise asyncio.CancelledError

        starting = execution.model_copy(deep=True)
        starting.transition_to(ExecutionStatus.STARTING)
        claim_task = asyncio.create_task(
            self._executions.save_if_status(
                starting,
                expected={ExecutionStatus.CREATED},
            ),
            name=f"riftx-remote-terminal-admission-claim-{execution.id}",
        )
        claim_interrupted = await _wait_for_shielded_task(claim_task)
        try:
            execution, admitted = claim_task.result()
        except BaseException:
            latest = await self._require_execution(execution.id)
            if latest.status is ExecutionStatus.CREATED:
                await self._shield_predispatch_cleanup(
                    latest.id,
                    terminal,
                    expected={ExecutionStatus.CREATED},
                )
            raise
        if claim_interrupted:
            if admitted:
                await self._shield_predispatch_cleanup(
                    execution.id,
                    terminal,
                    expected={ExecutionStatus.STARTING},
                )
            raise asyncio.CancelledError
        if not admitted:
            if execution.physical_stop_confirmed_at is not None:
                await self._close_confirmed_terminal_projection(terminal)
            raise ApplicationConflictError(
                "terminal_start_cancelled",
                f"Terminal {terminal.id!r} was cancelled before it could start",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )

        try:
            if effect_guard is not None:
                await effect_guard()
        except BaseException:
            await self._shield_predispatch_cleanup(
                execution.id,
                terminal,
                expected={ExecutionStatus.STARTING},
            )
            raise
        execution = await self._require_execution(execution.id)
        if execution.status is not ExecutionStatus.STARTING:
            if execution.physical_stop_confirmed_at is not None:
                await self._close_confirmed_terminal_projection(terminal)
            raise ApplicationConflictError(
                "terminal_start_cancelled",
                f"Terminal {terminal.id!r} was cancelled before remote dispatch",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )
        try:
            await self._control.enqueue(
                request.node_id,
                kind=RunnerCommandKind.TERMINAL_START,
                idempotency_key=f"terminal-start:{session_id}",
                target=runner_principal,
                payload={
                    "session_id": session_id,
                    "execution_id": execution.id,
                    "request": request.model_copy(
                        update={
                            "session_id": session_id,
                            "execution_id": execution.id,
                            "runner_principal": runner_principal,
                        }
                    ).model_dump(mode="json"),
                },
            )
        except Exception:
            current = await self.get_execution(session_id)
            if current.status is ExecutionStatus.STARTING:
                current.transition_to(ExecutionStatus.LOST)
                await self._executions.save_if_status(
                    current,
                    expected={ExecutionStatus.STARTING},
                )
            terminal.transition_to(TerminalStatus.LOST)
            await self._terminals.save(terminal)
            raise

        try:
            if effect_guard is not None:
                await effect_guard()
        except BaseException:
            # TERMINAL_START may already be in flight.  A CANCEL tombstone is
            # required; do not report CLOSED until the Runner acknowledges it.
            try:
                await self.close(terminal.id)
            except Exception:
                current = await self.get_execution(session_id)
                if current.status is ExecutionStatus.STARTING:
                    current.transition_to(ExecutionStatus.LOST)
                    await self._executions.save_if_status(
                        current,
                        expected={ExecutionStatus.STARTING},
                    )
                terminal.transition_to(TerminalStatus.LOST)
                await self._terminals.save(terminal)
            raise

        execution.transition_to(ExecutionStatus.RUNNING)
        execution, started = await self._executions.save_if_status(
            execution,
            expected={ExecutionStatus.STARTING},
        )
        if not started:
            durable_terminal = await self.get(session_id)
            if execution.status is ExecutionStatus.RUNNING:
                if durable_terminal.status is TerminalStatus.CREATED:
                    candidate = durable_terminal.model_copy(deep=True)
                    candidate.transition_to(TerminalStatus.OPEN)
                    durable_terminal, opened = await self._terminals.save_if_status(
                        candidate,
                        expected={TerminalStatus.CREATED},
                    )
                    if opened:
                        await self._append_opened_event(
                            request,
                            durable_terminal,
                            execution,
                        )
                if durable_terminal.status is TerminalStatus.OPEN:
                    return durable_terminal
                # A CLOSED/LOST projection cannot prove the RUNNING process
                # stopped. Request cancellation instead of treating it as a
                # safe start failure.
                await self.close(durable_terminal.id)
                raise ApplicationConflictError(
                    "terminal_start_projection_conflict",
                    f"Terminal {terminal.id!r} is running with a "
                    f"{durable_terminal.status.value} projection",
                    details={"session_id": terminal.id, "run_id": terminal.run_id},
                )
            if _has_durable_stop_proof(execution) or _is_strict_predispatch_absence(execution):
                await self._close_confirmed_terminal_projection(durable_terminal)
            else:
                # The command may have started, but central state does not yet
                # contain physical-stop proof. Leave a durable cancellation
                # tombstone with the owning Runner before surfacing failure.
                await self.close(durable_terminal.id)
                if durable_terminal.status in {
                    TerminalStatus.CREATED,
                    TerminalStatus.OPEN,
                }:
                    durable_terminal.transition_to(TerminalStatus.LOST)
                    await self._terminals.save(durable_terminal)
                raise ApplicationConflictError(
                    "terminal_start_state_unconfirmed",
                    f"Terminal {terminal.id!r} start outcome is not confirmed",
                    details={
                        "session_id": terminal.id,
                        "run_id": terminal.run_id,
                        "execution_status": execution.status.value,
                    },
                )
            raise ApplicationConflictError(
                "terminal_start_cancelled",
                f"Terminal {terminal.id!r} was cancelled before it could open",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )
        candidate = terminal.model_copy(deep=True)
        candidate.transition_to(TerminalStatus.OPEN)
        terminal, opened = await self._terminals.save_if_status(
            candidate,
            expected={TerminalStatus.CREATED},
        )
        if not opened:
            latest_execution = await self.get_execution(session_id)
            if terminal.status is TerminalStatus.OPEN:
                return terminal
            if _has_durable_stop_proof(latest_execution) or _is_strict_predispatch_absence(
                latest_execution
            ):
                raise ApplicationConflictError(
                    "terminal_start_cancelled",
                    f"Terminal {terminal.id!r} was cancelled before it could open",
                    details={"session_id": terminal.id, "run_id": terminal.run_id},
                )
            await self.close(terminal.id)
            raise ApplicationConflictError(
                "terminal_start_projection_conflict",
                f"Terminal {terminal.id!r} is running with a {terminal.status.value} projection",
                details={"session_id": terminal.id, "run_id": terminal.run_id},
            )
        await self._append_opened_event(request, terminal, execution)
        return terminal

    async def _append_opened_event(
        self,
        request: TerminalLaunchRequest,
        terminal: TerminalSession,
        execution: Execution,
    ) -> None:
        await self._events.append(
            request.run_id,
            "terminal.opened",
            {
                "session_id": terminal.id,
                "execution_id": execution.id,
                "node_id": request.node_id,
                "backend": "remote",
                "owner": terminal.owner.value,
                "cols": terminal.cols,
                "rows": terminal.rows,
            },
        )

    async def _require_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise ProcessTerminationError(
                f"Remote terminal execution {execution_id!r} disappeared during admission"
            )
        return execution

    @staticmethod
    def _require_exact_execution_replay(
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> None:
        expected_key = request.execution_key or f"terminal:{request.session_id}"
        fields: tuple[tuple[str, object, object], ...] = (
            ("execution_id", execution.id, request.execution_id),
            ("execution_key", execution.execution_key, expected_key),
            ("run_id", execution.run_id, request.run_id),
            ("agent_session_id", execution.session_id, request.agent_session_id),
            ("tool_call_id", execution.tool_call_id, request.tool_call_id),
            ("attempt_group", execution.attempt_group, request.attempt_group),
            ("node_id", execution.node_id, request.node_id),
            ("runner_principal", execution.owner, request.runner_principal),
            ("executor_type", execution.executor_type, ExecutorType.PTY),
            ("argv", execution.argv, request.argv),
            ("tool_id", execution.tool_id, request.tool_id),
            ("tool_version", execution.tool_version, request.tool_version),
            ("cwd", execution.cwd, str(request.cwd)),
            ("env", execution.env_diff, request.env),
        )
        mismatched = [name for name, persisted, requested in fields if persisted != requested]
        if (
            execution.launch_fingerprint is not None
            and execution.launch_fingerprint != request.launch_fingerprint
        ):
            mismatched.append("launch_fingerprint")
        if mismatched:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                f"Remote terminal execution {execution.id!r} conflicts with requested "
                f"launch fields: {', '.join(sorted(set(mismatched)))}",
                details={
                    "execution_id": execution.id,
                    "mismatched_fields": sorted(set(mismatched)),
                },
            )

    @staticmethod
    def _require_exact_terminal_replay(
        terminal: TerminalSession,
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> None:
        try:
            require_terminal_start_replay_matches(terminal, execution, request)
        except ValueError as exc:
            raise ApplicationConflictError(
                "execution_idempotency_conflict",
                str(exc),
                details={
                    "execution_id": execution.id,
                    "session_id": terminal.id,
                },
            ) from exc

    async def _cancel_predispatch_execution(
        self,
        execution_id: str,
        *,
        expected: set[ExecutionStatus],
    ) -> tuple[Execution, bool]:
        execution = await self._require_execution(execution_id)
        if execution.physical_stop_confirmed_at is not None:
            return execution, False
        if execution.status not in expected:
            return execution, False
        if not _has_no_terminal_dispatch_identity(execution):
            raise ProcessTerminationError(
                f"Refusing pre-dispatch cleanup for remote terminal execution "
                f"{execution.id!r} because durable dispatch identity already exists"
            )
        candidate = execution.model_copy(deep=True)
        candidate.transition_to(ExecutionStatus.CANCELLED, exit_code=0)
        candidate.physical_stop_confirmed_at = utc_now()
        execution, saved = await self._executions.save_if_status(
            candidate,
            expected={execution.status},
        )
        if saved:
            return execution, True
        if execution.physical_stop_confirmed_at is not None:
            return execution, False
        return execution, False

    async def _shield_predispatch_cleanup(
        self,
        execution_id: str,
        terminal: TerminalSession | None,
        *,
        expected: set[ExecutionStatus],
    ) -> None:
        cleanup_task = asyncio.create_task(
            self._cancel_predispatch_execution(
                execution_id,
                expected=expected,
            ),
            name=f"riftx-remote-terminal-predispatch-cleanup-{execution_id}",
        )
        interrupted = await _wait_for_shielded_task(cleanup_task)
        execution, _ = cleanup_task.result()
        if execution.physical_stop_confirmed_at is None:
            raise ProcessTerminationError(
                f"Could not persist pre-dispatch physical-stop proof for remote "
                f"terminal execution {execution_id!r}; durable status is "
                f"{execution.status.value!r}"
            )
        if terminal is not None:
            await self._close_confirmed_terminal_projection(terminal)
        if interrupted:
            raise asyncio.CancelledError

    async def _create_closed_terminal_projection(
        self,
        *,
        terminal: TerminalSession,
        execution: Execution,
        request: TerminalLaunchRequest,
    ) -> TerminalSession:
        closed = terminal.model_copy(deep=True)
        closed.transition_to(TerminalStatus.CLOSED)
        create_task = asyncio.create_task(
            self._terminals.create(closed),
            name=f"riftx-remote-terminal-closed-projection-create-{closed.id}",
        )
        interrupted = await _wait_for_shielded_task(create_task)
        try:
            durable = create_task.result()
        except BaseException:
            durable = await self._terminals.get_by_execution(execution.id)
            if durable is None:
                raise
            self._require_exact_terminal_replay(durable, execution, request)
        if interrupted:
            raise asyncio.CancelledError
        return durable

    async def get(self, session_id: str) -> TerminalSession:
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        return terminal

    async def resolve_run_id(self, session_id: str) -> str:
        run_id = await self._terminals.get_run_id(session_id)
        if run_id is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        return run_id

    async def get_execution(self, session_id: str) -> Execution:
        terminal = await self.get(session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", terminal.execution_id)
        return execution

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        execution = await self.get_execution(session_id)
        return await asyncio.to_thread(
            _read_output_slice,
            Path(execution.stdout_path),
            cursor,
            max_bytes,
        )

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        if not data:
            return
        if len(data) > 64 * 1024:
            raise ValueError("terminal input exceeds 65536 bytes")
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_WRITE,
            {"session_id": session_id, "data": base64.b64encode(data).decode("ascii")},
        )

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        terminal = await self.get(session_id)
        terminal.resize(cols, rows)
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_RESIZE,
            {"session_id": session_id, "cols": cols, "rows": rows},
        )
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.resized",
            {"session_id": terminal.id, "cols": cols, "rows": rows},
        )
        return terminal

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        terminal = await self.get(session_id)
        self._require_writer(terminal, actor)
        execution = await self.get_execution(session_id)
        await self._enqueue_operation(
            execution,
            RunnerCommandKind.TERMINAL_INTERRUPT,
            {"session_id": session_id},
        )
        await self._events.append(
            terminal.run_id,
            "terminal.interrupted",
            {"session_id": terminal.id, "actor": actor.value},
        )

    async def take_over(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.take_over(cursor=cursor)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.taken_over",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def release(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        cursor = await asyncio.to_thread(_output_size, Path(execution.stdout_path))
        terminal.release(cursor=cursor)
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.released",
            {"session_id": terminal.id, "owner": terminal.owner.value},
        )
        return terminal

    async def attach_transcript_artifact(
        self,
        session_id: str,
        artifact_id: str,
    ) -> TerminalSession:
        terminal = await self.get(session_id)
        if terminal.transcript_artifact_id is not None:
            return terminal
        terminal.transcript_artifact_id = artifact_id
        await self._terminals.save(terminal)
        await self._events.append(
            terminal.run_id,
            "terminal.transcript_archived",
            {"session_id": terminal.id, "artifact_id": artifact_id},
        )
        return terminal

    async def close(self, session_id: str) -> TerminalSession:
        terminal = await self.get(session_id)
        execution = await self.get_execution(session_id)
        if execution.status is ExecutionStatus.CREATED and _has_no_terminal_dispatch_identity(
            execution
        ):
            await self._shield_predispatch_cleanup(
                execution.id,
                terminal,
                expected={ExecutionStatus.CREATED},
            )
            return await self.get(session_id)
        if _has_durable_stop_proof(execution) or _is_strict_predispatch_absence(execution):
            return await self._close_confirmed_terminal_projection(terminal)
        runner_principal = _require_remote_owner(execution)
        operation_id = f"cancel:{execution.id}:{new_id()}"
        await self._control.enqueue(
            execution.node_id,
            kind=RunnerCommandKind.CANCEL,
            idempotency_key=operation_id,
            target=runner_principal,
            payload={
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
            },
        )
        await self._events.append(
            terminal.run_id,
            "terminal.close_requested",
            {
                "session_id": terminal.id,
                "execution_id": terminal.execution_id,
                "execution_key": execution.execution_key,
                "operation_id": operation_id,
                "node_id": execution.node_id,
                "terminal_status": terminal.status.value,
                "execution_status": execution.status.value,
            },
        )
        return terminal

    async def _close_confirmed_terminal_projection(
        self,
        terminal: TerminalSession,
    ) -> TerminalSession:
        for _ in range(8):
            if terminal.status is TerminalStatus.CLOSED:
                return terminal
            expected_status = terminal.status
            candidate = terminal.model_copy(deep=True)
            candidate.transition_to(TerminalStatus.CLOSED)
            terminal, saved = await self._terminals.save_if_status(
                candidate,
                expected={expected_status},
            )
            if saved:
                return terminal
        raise ApplicationConflictError(
            "terminal_projection_update_conflict",
            f"Terminal {terminal.id!r} changed repeatedly while confirming closure",
            details={"session_id": terminal.id, "run_id": terminal.run_id},
        )

    async def recover(self) -> list[TerminalSession]:
        return []

    async def close_all(self) -> None:
        return None

    async def _enqueue_operation(
        self,
        execution: Execution,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> None:
        runner_principal = _require_remote_owner(execution)
        operation_id = f"{kind.value}:{execution.id}:{new_id()}"
        await self._control.enqueue(
            execution.node_id,
            kind=kind,
            idempotency_key=operation_id,
            target=runner_principal,
            payload={
                **payload,
                "execution_id": execution.id,
                "operation_id": operation_id,
            },
        )

    @staticmethod
    def _require_writer(terminal: TerminalSession, actor: TerminalOwner) -> None:
        if terminal.status is not TerminalStatus.OPEN:
            raise ApplicationConflictError(
                "terminal_not_open",
                f"Terminal {terminal.id!r} is {terminal.status.value}",
            )
        if terminal.owner is not actor:
            raise ApplicationConflictError(
                "terminal_not_owned",
                f"Terminal input belongs to {terminal.owner.value!r}, not {actor.value!r}",
            )


class NodeTerminalRouter:
    """Route terminal operations by the execution node persisted for the session."""

    def __init__(
        self,
        *,
        local_node_id: str,
        terminal_repository: TerminalRepository,
        execution_repository: ExecutionRepository,
        local: TerminalController,
        remote: TerminalController,
    ) -> None:
        self._local_node_id = local_node_id
        self._terminals = terminal_repository
        self._executions = execution_repository
        self._local = local
        self._remote = remote

    async def start(
        self,
        request: TerminalLaunchRequest,
        *,
        effect_guard: EffectGuard | None = None,
    ) -> TerminalSession:
        return await self._for_node(request.node_id).start(
            request,
            effect_guard=effect_guard,
        )

    async def get(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.get(session_id)

    async def resolve_run_id(self, session_id: str) -> str:
        run_id = await self._terminals.get_run_id(session_id)
        if run_id is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        return run_id

    async def get_execution(self, session_id: str) -> Execution:
        controller = await self._for_session(session_id)
        return await controller.get_execution(session_id)

    async def read(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> OutputSlice:
        controller = await self._for_session(session_id)
        return await controller.read(session_id, cursor=cursor, max_bytes=max_bytes)

    async def write(
        self,
        session_id: str,
        data: bytes,
        *,
        actor: TerminalOwner,
    ) -> None:
        controller = await self._for_session(session_id)
        await controller.write(session_id, data, actor=actor)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.resize(session_id, cols=cols, rows=rows)

    async def interrupt(self, session_id: str, *, actor: TerminalOwner) -> None:
        controller = await self._for_session(session_id)
        await controller.interrupt(session_id, actor=actor)

    async def take_over(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.take_over(session_id)

    async def release(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.release(session_id)

    async def attach_transcript_artifact(
        self,
        session_id: str,
        artifact_id: str,
    ) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.attach_transcript_artifact(session_id, artifact_id)

    async def close(self, session_id: str) -> TerminalSession:
        controller = await self._for_session(session_id)
        return await controller.close(session_id)

    async def _for_session(self, session_id: str) -> TerminalController:
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        execution = await self._executions.get(terminal.execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", terminal.execution_id)
        return self._for_node(execution.node_id)

    def _for_node(self, node_id: str) -> TerminalController:
        return self._local if node_id == self._local_node_id else self._remote


def _read_output_slice(path: Path, cursor: int, max_bytes: int) -> OutputSlice:
    if cursor < 0:
        raise ValueError("terminal cursor must not be negative")
    if not path.exists():
        return OutputSlice(data=b"", cursor=cursor, next_cursor=cursor, eof=True)
    size = path.stat().st_size
    if cursor > size:
        raise ValueError(f"terminal cursor {cursor} is beyond transcript size {size}")
    with path.open("rb") as stream:
        stream.seek(cursor)
        data = stream.read(max_bytes)
    next_cursor = cursor + len(data)
    return OutputSlice(data=data, cursor=cursor, next_cursor=next_cursor, eof=next_cursor >= size)


def _output_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _require_remote_owner(execution: Execution) -> RunnerPrincipal:
    if execution.owner is None:
        raise ApplicationConflictError(
            "remote_execution_owner_missing",
            f"Remote terminal execution {execution.id!r} has no bound Runner owner",
            details={
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
                "node_id": execution.node_id,
            },
        )
    return execution.owner


def _has_durable_stop_proof(execution: Execution) -> bool:
    return (
        execution.status
        in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.EXITED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.HARD_TIMEOUT,
        }
        and execution.physical_stop_confirmed_at is not None
    )


def _is_strict_predispatch_absence(execution: Execution) -> bool:
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


def _has_no_terminal_dispatch_identity(execution: Execution) -> bool:
    return all(
        value is None
        for value in (
            execution.started_at,
            execution.process_created_at,
            execution.pid,
            execution.process_group_id,
            execution.containment_id,
        )
    )


def _is_terminal_projection_conflict(error: BaseException) -> bool:
    return isinstance(error, RepositoryConflictError) or (
        isinstance(error, RuntimeError) and "terminal session already exists" in str(error)
    )


async def _wait_for_shielded_task(task: asyncio.Task[object]) -> bool:
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    return interrupted
