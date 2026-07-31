"""Standalone outbound RiftX Runner daemon."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import platform as platform_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import typer

from riftx import __version__
from riftx.application.ports import ExecutionRepository
from riftx.application.services import NodeRegistration
from riftx.browser import (
    BrowserOperation,
    BrowserSessionCommand,
)
from riftx.domain import (
    BrowserSessionStatus,
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerCommandKind,
    RunnerPrincipal,
)
from riftx.executors import (
    DirectProcessExecutor,
    LinuxCgroupV2Manager,
    PowerShellNotFoundError,
    PowerShellResolver,
)
from riftx.target_http.models import (
    TargetHttpRequest,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
)

from .browser import BrowserRunner, RunnerBrowserManager, execute_browser_command
from .control_client import (
    LeasedRunnerCommand,
    OutputOffsetMismatch,
    RunnerControlClient,
    RunnerControlClientError,
    RunnerCredentialStore,
)
from .models import ExecutionLaunchRequest
from .paths import RunnerPaths
from .protocols import EffectGuard, ExecutionRunner
from .state import FileExecutionRepository, FileTerminalRepository
from .supervisor import ProcessSupervisor
from .target_http import RunnerTargetHttpClient, TargetHttpRunner
from .terminal import TerminalSupervisor
from .terminal_manager import (
    NullRunEventRepository,
    OperationJournal,
    RemoteTerminalManager,
)

logger = logging.getLogger(__name__)
app = typer.Typer(help="Run an outbound RiftX execution node.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Manage the standalone outbound Runner daemon."""


_TERMINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}
_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}

_SAFETY_COMMAND_KINDS = frozenset(
    {
        RunnerCommandKind.CANCEL,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER_CLOSE,
        RunnerCommandKind.TERMINAL_CLOSE,
    }
)


class TerminalCommandHandler(Protocol):
    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        effect_guard: EffectGuard | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> object: ...

    async def cancel_execution(self, execution_id: str) -> Execution: ...


def _default_capabilities() -> tuple[str, ...]:
    capabilities = ["process", "shell", "target_http"]
    if importlib.util.find_spec("playwright"):
        capabilities.append("browser_playwright")
    try:
        PowerShellResolver().resolve()
    except PowerShellNotFoundError:
        pass
    else:
        capabilities.append("powershell")
    if platform_module.system().lower() == "windows" and importlib.util.find_spec("winpty"):
        capabilities.append("conpty")
    return tuple(capabilities)


@dataclass(frozen=True, slots=True)
class RunnerDaemonConfig:
    server_url: str
    node_id: str
    name: str
    state_path: Path
    credential_path: Path = Path(".riftx/secrets/runner-credentials.json")
    registration_token: str | None = None
    platform: str = platform_module.system().lower()
    architecture: str = platform_module.machine().lower()
    runner_version: str = __version__
    capabilities: tuple[str, ...] = field(default_factory=_default_capabilities)
    labels: dict[str, str] | None = None
    poll_wait_seconds: float = 30.0
    reconnect_initial_seconds: float = 0.25
    reconnect_max_seconds: float = 10.0
    output_poll_seconds: float = 0.1
    command_lease_seconds: float = 30.0
    max_concurrent_commands: int = 16
    resource_stop_timeout_seconds: float = 5.0
    require_containment: bool = True
    payload_uid: int | None = None
    payload_gid: int | None = None

    def __post_init__(self) -> None:
        if self.command_lease_seconds <= 0:
            raise ValueError("Runner command lease duration must be positive")
        if self.max_concurrent_commands < 1:
            raise ValueError("Runner command concurrency must be positive")
        if self.resource_stop_timeout_seconds <= 0:
            raise ValueError("Runner resource stop timeout must be positive")
        if (self.payload_uid is None) != (self.payload_gid is None):
            raise ValueError("payload_uid and payload_gid must be configured together")
        for field_name, value in (
            ("payload_uid", self.payload_uid),
            ("payload_gid", self.payload_gid),
        ):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{field_name} must be a positive integer")

    @property
    def registration(self) -> NodeRegistration:
        labels = {
            "mode": "remote",
            "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC", "unknown"),
            "working_directory": str(Path.cwd()),
            "tool_count": "0",
            **(self.labels or {}),
        }
        return NodeRegistration(
            node_id=self.node_id,
            name=self.name,
            platform=self.platform,
            architecture=self.architecture,
            runner_version=self.runner_version,
            capabilities=self.capabilities,
            labels=labels,
        )


class RunnerDaemon:
    """Long-polls commands and bridges them to a local ProcessSupervisor."""

    def __init__(
        self,
        *,
        config: RunnerDaemonConfig,
        client: RunnerControlClient,
        supervisor: ExecutionRunner,
        executions: ExecutionRepository,
        terminal_handler: TerminalCommandHandler | None = None,
        target_http_handler: TargetHttpRunner | None = None,
        browser_handler: BrowserRunner | None = None,
        execution_cancellation_journal: OperationJournal | None = None,
        target_http_cancellation_journal: OperationJournal | None = None,
        target_http_delivery_journal: OperationJournal | None = None,
        target_http_stop_confirmation_journal: OperationJournal | None = None,
        browser_cancellation_journal: OperationJournal | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._supervisor = supervisor
        self._executions = executions
        self._terminal_handler = terminal_handler
        self._target_http_handler = target_http_handler
        self._browser_handler = browser_handler
        self._execution_cancellations = execution_cancellation_journal or OperationJournal(
            config.state_path / "execution-cancellations.json"
        )
        self._target_http_cancellations = target_http_cancellation_journal or OperationJournal(
            config.state_path / "target-http-cancellations.json"
        )
        self._target_http_deliveries = target_http_delivery_journal or OperationJournal(
            config.state_path / "target-http-deliveries.json"
        )
        self._target_http_stop_confirmations = (
            target_http_stop_confirmation_journal
            or OperationJournal(config.state_path / "target-http-stop-confirmations.json")
        )
        self._browser_cancellations = browser_cancellation_journal or OperationJournal(
            config.state_path / "browser-cancellations.json"
        )
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._command_tasks: dict[str, asyncio.Task[None]] = {}
        self._regular_command_tasks: set[asyncio.Task[None]] = set()
        # Cancellation is a safety effect, not a command-lease effect. Once a
        # daemon-owned task has been created it survives cancellation of the
        # leased handler and remains tracked until physical stop and reporting
        # have either completed or failed explicitly.
        self._execution_stop_tasks: dict[
            tuple[str, str, str, int],
            asyncio.Task[dict[str, object]],
        ] = {}
        self._active_browser_tasks: dict[
            str,
            dict[asyncio.Task[object], asyncio.Event],
        ] = {}
        # Starting an execution and acknowledging its cancellation must share
        # one Runner-local serialization boundary.  Without it, CANCEL could
        # persist a tombstone, observe no local row, and report CANCELLED while
        # an EXECUTE/TERMINAL_START handler that had already passed its
        # tombstone check went on to spawn a process.
        self._execution_control_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def run_forever(self) -> None:
        delay = self.config.reconnect_initial_seconds
        while not self._closed:
            try:
                await self._client.connect(self.config.registration)
                await self.resume_active()
                delay = self.config.reconnect_initial_seconds
                safety_only = (
                    len(self._regular_command_tasks) >= self.config.max_concurrent_commands
                )
                command = await self._client.poll(
                    wait_seconds=self.config.poll_wait_seconds,
                    safety_only=safety_only,
                )
                if command is not None:
                    self._start_command(command)
            except asyncio.CancelledError:
                raise
            except RunnerControlClientError as exc:
                if exc.status_code == 401:
                    self._client.invalidate_credentials()
                logger.warning("Runner control connection failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)
            except Exception:
                logger.exception("Runner command processing failed")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)

    def _start_command(self, command: LeasedRunnerCommand) -> None:
        existing = self._command_tasks.get(command.id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_leased_command(command),
            name=f"riftx-runner-command-{command.id}",
        )
        self._command_tasks[command.id] = task
        if command.kind not in _SAFETY_COMMAND_KINDS:
            self._regular_command_tasks.add(task)
        task.add_done_callback(
            lambda completed, command_id=command.id: self._command_finished(
                command_id,
                completed,
            )
        )

    def _command_finished(self, command_id: str, task: asyncio.Task[None]) -> None:
        if self._command_tasks.get(command_id) is task:
            self._command_tasks.pop(command_id, None)
        self._regular_command_tasks.discard(task)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _run_leased_command(self, command: LeasedRunnerCommand) -> None:
        lease_lost = asyncio.Event()
        handler = asyncio.create_task(
            self.handle_command(command, _lease_lost=lease_lost),
            name=f"riftx-runner-handler-{command.id}",
        )
        renewal: asyncio.Task[object] | None = None
        try:
            renew = getattr(self._client, "renew", None)
            if renew is None:
                await handler
                return

            loop = asyncio.get_running_loop()
            lease_remaining = command.lease_duration_seconds or _lease_remaining_seconds(
                command.lease_expires_at,
                fallback=self.config.command_lease_seconds,
            )
            lease_deadline = loop.time() + lease_remaining
            while not handler.done():
                current_remaining = lease_deadline - loop.time()
                if current_remaining <= 0:
                    lease_lost.set()
                    logger.error(
                        "Runner command %s lease expired; cancelling local handler",
                        command.id,
                    )
                    return
                renew_interval = max(0.01, min(lease_remaining / 3, 10.0))
                done, _ = await asyncio.wait(
                    {handler},
                    timeout=min(renew_interval, current_remaining),
                )
                if done:
                    break

                current_remaining = lease_deadline - loop.time()
                if current_remaining <= 0:
                    lease_lost.set()
                    logger.error(
                        "Runner command %s lease expired; cancelling local handler",
                        command.id,
                    )
                    return
                renewal = asyncio.create_task(
                    renew(command),
                    name=f"riftx-runner-renew-{command.id}",
                )
                done, _ = await asyncio.wait(
                    {handler, renewal},
                    timeout=current_remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if handler in done:
                    break
                if renewal not in done:
                    lease_lost.set()
                    logger.error(
                        "Runner command %s lease renewal exceeded its deadline; "
                        "cancelling local handler",
                        command.id,
                    )
                    return
                try:
                    renewed_lease = renewal.result()
                    renewed_seconds = _renewed_lease_seconds(
                        renewed_lease,
                        fallback=self.config.command_lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except RunnerControlClientError as exc:
                    logger.warning(
                        "Unable to renew Runner command %s lease: %s",
                        command.id,
                        exc,
                    )
                    # Network failures do not prove that the Control Plane
                    # rejected this lease, and 5xx responses may recover while
                    # the current lease remains valid. Any other protocol
                    # response is definitive: retrying a reclaimed, missing,
                    # or unauthorized lease would let the old owner continue
                    # alongside a newly leased Runner.
                    if not 500 <= exc.status_code < 600:
                        lease_lost.set()
                        logger.error(
                            "Runner command %s lease renewal was rejected "
                            "(%s %s); cancelling local handler",
                            command.id,
                            exc.status_code,
                            exc.code,
                        )
                        return
                    if loop.time() >= lease_deadline:
                        lease_lost.set()
                        logger.error(
                            "Runner command %s lease expired; cancelling local handler",
                            command.id,
                        )
                        return
                except Exception as exc:
                    logger.warning(
                        "Unable to renew Runner command %s lease: %s",
                        command.id,
                        exc,
                    )
                    if loop.time() >= lease_deadline:
                        lease_lost.set()
                        logger.error(
                            "Runner command %s lease expired; cancelling local handler",
                            command.id,
                        )
                        return
                else:
                    lease_remaining = renewed_seconds
                    lease_deadline = loop.time() + lease_remaining
                finally:
                    renewal = None
            await handler
        finally:
            # asyncio.wait() does not transfer cancellation to the task it is
            # observing. Without this ownership cleanup, daemon shutdown can
            # leave an EXECUTE/TERMINAL_START handler alive after the client
            # and supervisors have closed, allowing a late native spawn.
            if not handler.done():
                handler.cancel()
            if renewal is not None and not renewal.done():
                renewal.cancel()
            owned_tasks = [handler]
            if renewal is not None:
                owned_tasks.append(renewal)
            await asyncio.gather(*owned_tasks, return_exceptions=True)

    async def handle_command(
        self,
        command: LeasedRunnerCommand,
        *,
        _lease_lost: asyncio.Event | None = None,
    ) -> None:
        try:
            owner = self._require_command_owner(command)
            if command.kind is RunnerCommandKind.EXECUTE:
                result = await self._handle_execute(command, owner)
            elif command.kind is RunnerCommandKind.CANCEL:
                result = await self._handle_cancel(command.payload, owner)
            elif command.kind is RunnerCommandKind.TERMINAL_CLOSE:
                session_id = _required_string(command.payload, "session_id")
                result = await self._handle_cancel(
                    {
                        "execution_id": _required_string(command.payload, "execution_id"),
                        "execution_key": f"terminal:{session_id}",
                    },
                    owner,
                )
            elif command.kind is RunnerCommandKind.TARGET_HTTP:
                result = await self._handle_target_http(command)
            elif command.kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
                result = await self._handle_target_http_cancel(command.payload)
            elif command.kind is RunnerCommandKind.BROWSER:
                if command.payload.get("operation") == BrowserOperation.CLOSE.value:
                    result = await self._handle_browser_close(command.payload)
                else:
                    result = await self._handle_browser(command)
            elif command.kind is RunnerCommandKind.BROWSER_CLOSE:
                result = await self._handle_browser_close(command.payload)
            elif command.kind in {
                RunnerCommandKind.TERMINAL_START,
                RunnerCommandKind.TERMINAL_WRITE,
                RunnerCommandKind.TERMINAL_RESIZE,
                RunnerCommandKind.TERMINAL_INTERRUPT,
            }:
                terminal_result = await self._handle_terminal(
                    command.kind,
                    command.payload,
                    owner,
                )
                result = {"result": terminal_result}
            else:
                raise RuntimeError(f"Unsupported Runner command {command.kind.value!r}")
        except asyncio.CancelledError:
            if not self._closed:
                try:
                    await asyncio.wait_for(
                        self._finish_command_if_lease_valid(
                            command,
                            _lease_lost,
                            succeeded=False,
                            error="Runner command was preempted by a safety stop",
                        ),
                        timeout=self.config.resource_stop_timeout_seconds,
                    )
                except Exception:
                    logger.warning(
                        "Unable to record preemption for Runner command %s",
                        command.id,
                    )
            raise
        except Exception as exc:
            logger.exception("Runner command %s failed", command.id)
            if command.kind is RunnerCommandKind.TERMINAL_START:
                await self._report_durable_terminal_start_status(command.payload)
            await self._finish_command_if_lease_valid(
                command,
                _lease_lost,
                succeeded=False,
                error=str(exc),
            )
            return
        await self._finish_command_if_lease_valid(
            command,
            _lease_lost,
            succeeded=True,
            result=result,
        )

    async def _finish_command_if_lease_valid(
        self,
        command: LeasedRunnerCommand,
        lease_lost: asyncio.Event | None,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        if lease_lost is not None and lease_lost.is_set():
            logger.warning(
                "Runner command %s lease was lost; suppressing command completion",
                command.id,
            )
            return
        await self._client.finish(
            command,
            succeeded=succeeded,
            result=result,
            error=error,
        )

    async def _handle_terminal(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        owner: RunnerPrincipal,
    ) -> dict[str, object]:
        if self._terminal_handler is None:
            raise RuntimeError(f"Runner does not support command {kind.value!r}")
        if kind is RunnerCommandKind.TERMINAL_START:
            session_id = _required_string(payload, "session_id")
            execution_id = _required_string(payload, "execution_id")
            execution_key = f"terminal:{session_id}"
            raw_request = payload.get("request")
            if not isinstance(raw_request, dict):
                raise ValueError("terminal_start command is missing request")
            declared_owner = _runner_principal(raw_request.get("runner_principal"))
            if declared_owner != owner:
                raise RuntimeError("Terminal start request owner does not match command owner")
            lock = self._execution_control_lock(execution_key)
            await lock.acquire()
            admitted = False

            def on_admitted() -> None:
                nonlocal admitted
                if admitted:
                    return
                admitted = True
                lock.release()

            async def effect_guard() -> None:
                if await self._execution_cancellations.contains(execution_key):
                    raise RuntimeError(
                        f"Terminal execution {execution_id!r} was cancelled on this Runner"
                    )

            try:
                existing = await self._executions.get_by_key(execution_key)
                if existing is not None:
                    self._require_local_execution_owner(
                        existing,
                        owner,
                        execution_id=execution_id,
                        execution_key=execution_key,
                    )
                if await self._execution_cancellations.contains(execution_key):
                    return {
                        "execution_id": execution_id,
                        "status": "suppressed",
                        "suppressed_by_cancellation": True,
                        "physical_stop_confirmed": False,
                    }
                result = await self._terminal_handler.handle(
                    kind,
                    payload,
                    effect_guard=effect_guard,
                    on_admitted=on_admitted,
                )
                local = await self._executions.get_by_key(execution_key)
                if local is None:
                    raise RuntimeError(
                        f"Terminal start {execution_id!r} did not persist local execution state"
                    )
                await self._bind_or_require_local_owner(
                    local,
                    owner,
                    execution_id=execution_id,
                    execution_key=execution_key,
                    allow_bind=existing is None,
                )
                return result
            finally:
                if not admitted:
                    lock.release()
        execution_id = _required_string(payload, "execution_id")
        local = await self._require_local_execution(execution_id)
        self._require_local_execution_owner(
            local,
            owner,
            execution_id=execution_id,
            execution_key=local.execution_key,
        )
        return await self._terminal_handler.handle(kind, payload)

    async def _report_durable_terminal_start_status(
        self,
        payload: dict[str, object],
    ) -> None:
        """Retry the real local status after a terminal-start command failure.

        A TERMINAL_START handler can fail after the PTY has been spawned, for
        example when its first status upload loses the connection. The command
        exception therefore says nothing about the native process lifecycle;
        only the Runner-local durable execution is safe to report.
        """

        execution_id = payload.get("execution_id")
        session_id = payload.get("session_id")
        if not isinstance(execution_id, str) or not execution_id:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            execution_key = f"terminal:{session_id}"
            # The daemon-owned stop task is authoritative once the tombstone
            # exists. Do not race it by uploading a stale STARTING/RUNNING
            # snapshot from the failed start handler.
            if await self._execution_cancellations.contains(execution_key):
                return
            execution = await self._executions.get_by_key(execution_key)
            if execution is None or execution.id != execution_id:
                return
            await self._report_execution(execution_id, execution)
        except Exception as status_exc:
            logger.warning(
                "Unable to report durable terminal status for %s after command failure: %s",
                execution_id,
                status_exc,
            )

    async def _handle_target_http(self, command: LeasedRunnerCommand) -> dict[str, object]:
        if self._target_http_handler is None:
            raise RuntimeError("Runner does not support Target HTTP")
        raw_launch = command.payload.get("launch")
        if not isinstance(raw_launch, dict):
            raise ValueError("Target HTTP command is missing launch data")
        raw_request = raw_launch.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("Target HTTP command is missing request data")
        launch = TargetHttpRunnerRequest.model_validate(
            {
                **raw_launch,
                "request": TargetHttpRequest.from_runner_payload(raw_request),
            }
        )
        if launch.node_id != self.config.node_id:
            raise ValueError("Target HTTP command targets a different Runner node")
        cancellation_key = _target_http_cancellation_key(
            launch.run_id,
            launch.tool_call_id,
        )

        async def effect_guard() -> None:
            if await self._target_http_cancellations.contains(cancellation_key):
                raise RuntimeError("Target HTTP request was cancelled on this Runner")

        await effect_guard()
        if not await self._target_http_deliveries.claim(cancellation_key):
            raise RuntimeError(
                "Target HTTP request replay was suppressed because delivery was already "
                "claimed on this Runner; its physical outcome is unconfirmed"
            )
        # Re-check after claiming. A cancellation may have won between the
        # initial tombstone read and the durable delivery claim.
        await effect_guard()
        exchange = await self._target_http_handler.execute(
            launch,
            effect_guard=effect_guard,
        )
        offset = 0
        while offset < len(exchange.response_body):
            chunk = exchange.response_body[offset : offset + 256 * 1024]
            try:
                offset = await self._client.report_command_output(
                    command,
                    offset=offset,
                    data=chunk,
                )
            except OutputOffsetMismatch as exc:
                offset = exc.expected_offset
                if offset > len(exchange.response_body):
                    raise RuntimeError(
                        "Control Plane response output exceeds the Runner response"
                    ) from exc
        return {"result": exchange.result.model_dump(mode="json")}

    async def _handle_target_http_cancel(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if self._target_http_handler is None:
            raise RuntimeError("Runner does not support Target HTTP")
        run_id = _required_string(payload, "run_id")
        raw_ids = payload.get("tool_call_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("Target HTTP cancellation is missing Tool Call ids")
        tool_call_ids = tuple(
            dict.fromkeys(item for item in raw_ids if isinstance(item, str) and item)
        )
        if len(tool_call_ids) != len(raw_ids):
            raise ValueError("Target HTTP cancellation contains invalid Tool Call ids")

        journal_errors: dict[str, str] = {}
        for tool_call_id in tool_call_ids:
            try:
                await self._target_http_cancellations.add(
                    _target_http_cancellation_key(run_id, tool_call_id)
                )
            except Exception as exc:
                journal_errors[tool_call_id] = f"{type(exc).__name__}: {exc}"

        previously_confirmed: set[str] = set()
        for tool_call_id in tool_call_ids:
            if tool_call_id in journal_errors:
                continue
            confirmation_key = _target_http_cancellation_key(run_id, tool_call_id)
            if await self._target_http_stop_was_confirmed(confirmation_key):
                previously_confirmed.add(tool_call_id)

        pending_ids = tuple(
            tool_call_id
            for tool_call_id in tool_call_ids
            if tool_call_id not in previously_confirmed
        )
        if pending_ids:
            try:
                stopped = await self._target_http_handler.stop_run(
                    run_id,
                    node_id=self.config.node_id,
                    tool_call_ids=pending_ids,
                )
            except Exception as exc:
                stopped = [
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=tool_call_id,
                        confirmed=False,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    for tool_call_id in pending_ids
                ]
        else:
            stopped = []
        by_id = {item.tool_call_id: item for item in stopped}
        outcomes: list[TargetHttpRunnerStopOutcome] = []
        for tool_call_id in tool_call_ids:
            if tool_call_id in journal_errors:
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=tool_call_id,
                        confirmed=False,
                        reason=(
                            "Target HTTP may restart because its cancellation tombstone "
                            f"could not be persisted: {journal_errors[tool_call_id]}"
                        ),
                    )
                )
                continue
            if tool_call_id in previously_confirmed:
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=tool_call_id,
                        confirmed=True,
                        reason="target_http_local_task_termination_previously_confirmed",
                    )
                )
                continue
            outcome = by_id.get(tool_call_id)
            if outcome is None:
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=tool_call_id,
                        confirmed=False,
                        reason="Runner omitted the local Target HTTP stop outcome",
                    )
                )
                continue
            if outcome.confirmed:
                confirmation_key = _target_http_cancellation_key(run_id, tool_call_id)
                try:
                    await self._target_http_stop_confirmations.add(confirmation_key)
                except Exception as exc:
                    logger.warning(
                        "Unable to persist Target HTTP physical-stop confirmation for %s: %s",
                        confirmation_key,
                        exc,
                    )
                outcomes.append(
                    TargetHttpRunnerStopOutcome(
                        tool_call_id=tool_call_id,
                        confirmed=True,
                        reason=(
                            outcome.reason or "Target HTTP cancellation tombstone acknowledged"
                        ),
                    )
                )
            else:
                # A concurrent or earlier cancel handler may have persisted
                # confirmation after this handler observed no local task.
                confirmation_key = _target_http_cancellation_key(run_id, tool_call_id)
                if await self._target_http_stop_was_confirmed(confirmation_key):
                    outcomes.append(
                        TargetHttpRunnerStopOutcome(
                            tool_call_id=tool_call_id,
                            confirmed=True,
                            reason=("target_http_local_task_termination_previously_confirmed"),
                        )
                    )
                else:
                    outcomes.append(outcome)
        return {"outcomes": [item.model_dump(mode="json") for item in outcomes]}

    async def _target_http_stop_was_confirmed(self, confirmation_key: str) -> bool:
        try:
            return await self._target_http_stop_confirmations.contains(confirmation_key)
        except Exception as exc:
            # Reconciliation state is evidence, not the stop mechanism. A
            # corrupt/unreadable confirmation journal must never prevent a
            # fresh attempt to stop the local network operation.
            logger.warning(
                "Unable to read Target HTTP physical-stop confirmation for %s: %s",
                confirmation_key,
                exc,
            )
            return False

    async def _handle_browser(self, command: LeasedRunnerCommand) -> dict[str, object]:
        if self._browser_handler is None:
            raise RuntimeError("Runner does not support managed browser commands")
        operation = BrowserOperation(str(command.payload.get("operation", "")))
        raw_command = command.payload.get("command")
        if not isinstance(raw_command, dict):
            raise ValueError("Browser command is missing operation data")
        session_id = _required_string(raw_command, "session_id")
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("Browser command is not running in an asyncio Task")
        active = self._active_browser_tasks.setdefault(session_id, {})
        resource_done = asyncio.Event()
        active[current_task] = resource_done
        try:
            if await self._browser_cancellations.contains(_browser_cancellation_key(session_id)):
                raise RuntimeError("Browser session was cancelled on this Runner")
            exchange = await execute_browser_command(
                self._browser_handler,
                operation=operation,
                payload=raw_command,
            )
        finally:
            resource_done.set()
            active.pop(current_task, None)
            if not active:
                self._active_browser_tasks.pop(session_id, None)
        offset = 0
        while offset < len(exchange.attachment_content):
            chunk = exchange.attachment_content[offset : offset + 256 * 1024]
            try:
                offset = await self._client.report_command_output(
                    command, offset=offset, data=chunk
                )
            except OutputOffsetMismatch as exc:
                offset = exc.expected_offset
                if offset > len(exchange.attachment_content):
                    raise RuntimeError(
                        "Control Plane browser output exceeds the Runner attachment"
                    ) from exc
        return {"result": exchange.result.model_dump(mode="json")}

    async def _handle_browser_close(self, payload: dict[str, object]) -> dict[str, object]:
        if self._browser_handler is None:
            raise RuntimeError("Runner does not support managed browser commands")
        raw_command = payload.get("command")
        if not isinstance(raw_command, dict):
            raise ValueError("Browser close command is missing session data")
        close_command = BrowserSessionCommand.model_validate(raw_command)
        journal_error: Exception | None = None
        try:
            await self._browser_cancellations.add(
                _browser_cancellation_key(close_command.session_id)
            )
        except Exception as exc:
            journal_error = exc

        active = tuple(self._active_browser_tasks.get(close_command.session_id, {}).items())
        for task, _ in active:
            task.cancel()
        if active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(done.wait() for _, done in active)),
                    timeout=self.config.resource_stop_timeout_seconds,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    "Browser operation did not stop before the safety deadline"
                ) from exc

        try:
            exchange = await asyncio.wait_for(
                self._browser_handler.close(close_command),
                timeout=self.config.resource_stop_timeout_seconds,
            )
        except KeyError as exc:
            # The Control Plane snapshot proves identity, not physical closure.
            # A restarted Runner has no in-memory Browser manager state and may
            # still have an orphaned browser/attached session.  The tombstone
            # safely suppresses a delayed OPEN, but absence from the manager is
            # not sufficient evidence to acknowledge CLOSED.
            raise RuntimeError(
                "Browser session is not registered on this Runner; physical close "
                "could not be confirmed"
            ) from exc
        if exchange.result.session.status is not BrowserSessionStatus.CLOSED:
            raise RuntimeError("Browser Runner did not confirm a closed session")
        if journal_error is not None:
            raise RuntimeError(
                "Browser closed locally, but its cancellation tombstone could not be "
                "persisted; a delayed open may restart it"
            ) from journal_error
        return {"result": exchange.result.model_dump(mode="json")}

    async def close(self) -> None:
        self._closed = True
        command_tasks = list(self._command_tasks.values())
        for task in command_tasks:
            task.cancel()
        await asyncio.gather(*command_tasks, return_exceptions=True)
        # Do not cancel safety cleanup with the command handlers. In
        # particular, a CANCEL lease can expire immediately after its durable
        # tombstone is written; the physical stop still has to finish.
        stop_tasks = list(self._execution_stop_tasks.values())
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Start independent resource-family shutdowns together. A failed
        # process stop must not starve terminal or browser cleanup.
        resource_stops: list[asyncio.Task[object]] = []
        close = getattr(self._supervisor, "close", None)
        if close is not None:
            resource_stops.append(
                asyncio.create_task(
                    close(cancel_running=True),
                    name="riftx-runner-close-processes",
                )
            )
        terminal_close = getattr(self._terminal_handler, "close", None)
        if terminal_close is not None:
            resource_stops.append(
                asyncio.create_task(
                    terminal_close(),
                    name="riftx-runner-close-terminals",
                )
            )
        browser_close = getattr(self._browser_handler, "close_all", None)
        if browser_close is not None:
            resource_stops.append(
                asyncio.create_task(
                    browser_close(),
                    name="riftx-runner-close-browsers",
                )
            )
        stop_results = await asyncio.gather(*resource_stops, return_exceptions=True)
        client_results = await asyncio.gather(self._client.close(), return_exceptions=True)
        errors = [
            result
            for result in (*stop_results, *client_results)
            if isinstance(result, BaseException)
        ]
        if errors:
            for error in errors[1:]:
                logger.error("Additional Runner shutdown failure: %r", error)
            raise errors[0]

    async def _handle_execute(
        self,
        command: LeasedRunnerCommand,
        owner: RunnerPrincipal,
    ) -> dict[str, object]:
        payload = command.payload
        server_execution_id = _required_string(payload, "execution_id")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("execute command is missing request")
        request = ExecutionLaunchRequest.model_validate(raw_request)
        if request.runner_principal != owner:
            raise RuntimeError("Execute request owner does not match command owner")
        if request.execution_id not in {None, server_execution_id}:
            raise ValueError("execute command execution IDs do not match")
        request = request.model_copy(update={"execution_id": server_execution_id})
        if request.node_id != self.config.node_id:
            raise ValueError("execute command targets a different Runner node")

        async def effect_guard() -> None:
            if await self._execution_cancellations.contains(request.execution_key):
                raise RuntimeError(
                    f"Execution {server_execution_id!r} was cancelled on this Runner"
                )

        async with self._execution_control_lock(request.execution_key):
            existing = await self._executions.get_by_key(request.execution_key)
            if existing is not None:
                self._require_local_execution_owner(
                    existing,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=request.execution_key,
                )
            if await self._execution_cancellations.contains(request.execution_key):
                return {
                    "execution_id": server_execution_id,
                    "status": "suppressed",
                    "suppressed_by_cancellation": True,
                    "physical_stop_confirmed": False,
                }
            execution = await self._supervisor.start(
                request,
                effect_guard=effect_guard,
            )
            if execution.id != server_execution_id:
                raise RuntimeError(
                    f"Runner execution identity mismatch: command requested "
                    f"{server_execution_id!r}, local state returned {execution.id!r}"
                )
            execution = await self._bind_or_require_local_owner(
                execution,
                owner,
                execution_id=server_execution_id,
                execution_key=request.execution_key,
                allow_bind=existing is None,
            )
        # Admission is complete. Never keep the execution-control lock across
        # Control Plane I/O: a blocked status upload must not delay a tombstoned
        # safety stop.
        if execution.status in _TERMINAL_STATUSES:
            if existing is None:
                await self._client.report_status(
                    server_execution_id,
                    ExecutionStatus.RUNNING,
                    pid=execution.pid,
                    process_group_id=execution.process_group_id,
                )
        else:
            self._start_monitor(server_execution_id, execution.id)
        await self._report_execution(server_execution_id, execution)
        return {
            "execution_id": server_execution_id,
            "local_execution_id": execution.id,
            "execution_key": execution.execution_key,
            "owner": owner.model_dump(mode="json"),
            "status": execution.status.value,
        }

    async def _handle_cancel(
        self,
        payload: dict[str, object],
        owner: RunnerPrincipal,
    ) -> dict[str, object]:
        server_execution_id = _required_string(payload, "execution_id")
        execution_key = _required_string(payload, "execution_key")
        task = self._get_or_start_execution_stop(
            server_execution_id,
            execution_key,
            owner,
        )
        return await asyncio.shield(task)

    def _get_or_start_execution_stop(
        self,
        server_execution_id: str,
        execution_key: str,
        owner: RunnerPrincipal,
    ) -> asyncio.Task[dict[str, object]]:
        task_key = (
            server_execution_id,
            execution_key,
            owner.instance_id,
            owner.epoch,
        )
        existing = self._execution_stop_tasks.get(task_key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._stop_execution_durably(server_execution_id, execution_key, owner),
            name=f"riftx-runner-stop-{server_execution_id}",
        )
        self._execution_stop_tasks[task_key] = task
        task.add_done_callback(
            lambda completed, key=task_key: self._execution_stop_finished(key, completed)
        )
        return task

    def _execution_stop_finished(
        self,
        task_key: tuple[str, str, str, int],
        task: asyncio.Task[dict[str, object]],
    ) -> None:
        if self._execution_stop_tasks.get(task_key) is task:
            self._execution_stop_tasks.pop(task_key, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _stop_execution_durably(
        self,
        server_execution_id: str,
        execution_key: str,
        owner: RunnerPrincipal,
    ) -> dict[str, object]:
        # Fence obvious stale/copied state before writing a tombstone.  The
        # read is intentionally outside the execution lock: an in-flight
        # start holds that lock while its final effect guard runs, and the
        # tombstone must be able to reach that guard before cancellation waits
        # for admission to finish.
        local = await self._executions.get_by_key(execution_key)
        if local is not None:
            self._require_local_execution_owner(
                local,
                owner,
                execution_id=server_execution_id,
                execution_key=execution_key,
            )

        cancellation_journal_error: Exception | None = None
        try:
            await self._execution_cancellations.add(execution_key)
        except Exception as exc:
            cancellation_journal_error = exc
            logger.exception(
                "Unable to persist cancellation tombstone for execution key %s; "
                "continuing with local process termination",
                execution_key,
            )

        async with self._execution_control_lock(execution_key):
            # State can be registered or replaced while cancellation is
            # waiting for an in-flight start. Re-read and re-fence under the
            # lock before invoking any physical stop effect.
            local = await self._executions.get_by_key(execution_key)
            if local is None:
                if cancellation_journal_error is not None:
                    raise RuntimeError(
                        f"Cancellation for execution {server_execution_id!r} cannot be "
                        "guaranteed: no local execution was found and its cancellation "
                        "tombstone could not be persisted"
                    ) from cancellation_journal_error
                # A tombstone proves only that this Runner will suppress a
                # delayed start.  It is not ownership or process-termination
                # evidence: another Runner instance using the same node_id but
                # a different state path may still own a live process/PTY.
                # Keep the command failed and the Control Plane execution
                # non-terminal until an owning Runner confirms the stop.
                raise RuntimeError(
                    f"Cancellation for execution {server_execution_id!r} is unconfirmed: "
                    "the execution is not registered on this Runner, so physical "
                    "termination could not be confirmed. Its cancellation tombstone "
                    "was persisted only to suppress delayed starts"
                )
            # This second check deliberately precedes supervisor.cancel(). A
            # copied executions.json with the same node/execution IDs cannot
            # make a new Runner generation terminate a process it does not own.
            self._require_local_execution_owner(
                local,
                owner,
                execution_id=server_execution_id,
                execution_key=execution_key,
            )
            try:
                if local.executor_type is ExecutorType.PTY:
                    if self._terminal_handler is None:
                        raise RuntimeError("Runner does not support PTY execution cancellation")
                    execution = await self._terminal_handler.cancel_execution(local.id)
                else:
                    execution = await self._supervisor.cancel(local.id)
            except Exception as exc:
                if cancellation_journal_error is not None:
                    raise RuntimeError(
                        f"Cancellation for execution {server_execution_id!r} failed: its "
                        "cancellation tombstone could not be persisted and local process "
                        "termination also failed"
                    ) from exc
                raise
            if execution.id != server_execution_id:
                raise RuntimeError(
                    f"Cancellation for execution {server_execution_id!r} returned "
                    f"mismatched local execution {execution.id!r}"
                )
            self._require_local_execution_owner(
                execution,
                owner,
                execution_id=server_execution_id,
                execution_key=execution_key,
            )
            durable_execution = await self._executions.get_by_key(execution_key)
            if durable_execution is None or durable_execution.id != server_execution_id:
                raise RuntimeError(
                    f"Cancellation for execution {server_execution_id!r} is unconfirmed: "
                    "the stopped outcome was not found in durable Runner state"
                )
            self._require_local_execution_owner(
                durable_execution,
                owner,
                execution_id=server_execution_id,
                execution_key=execution_key,
            )
            if (
                durable_execution.status not in _PHYSICAL_STOP_PROOF_STATUSES
                or durable_execution.physical_stop_confirmed_at is None
            ):
                raise RuntimeError(
                    f"Cancellation for execution {server_execution_id!r} is unconfirmed: "
                    "the local stop did not persist a terminal status with durable "
                    "physical-stop proof"
                )
            execution = durable_execution
        # Physical stop has been confirmed and the lock can be released before
        # status reporting. A stalled Control Plane cannot hold up another
        # safety attempt or startup guard.
        await self._report_execution(server_execution_id, execution)
        if cancellation_journal_error is not None:
            # The owner-fenced Execution row and its durable stop proof are a
            # no-restart barrier for this already-admitted execution key:
            # duplicate Process starts return the terminal row, while PTY
            # starts return the existing terminal or reject a missing one.
            # The tombstone remains mandatory only when no owned row exists,
            # because it must then suppress a future first admission.
            logger.warning(
                "Execution %s stopped with durable row proof, but cancellation "
                "tombstone persistence is degraded: %s",
                server_execution_id,
                cancellation_journal_error,
            )
        result: dict[str, object] = {
            "execution_id": server_execution_id,
            "local_execution_id": execution.id,
            "execution_key": execution_key,
            "owner": owner.model_dump(mode="json"),
            # CANCEL is a stop-disposition protocol ACK. The actual execution
            # outcome was uploaded immediately above and remains unchanged.
            "status": ExecutionStatus.CANCELLED.value,
            "physical_stop_confirmed": True,
        }
        if cancellation_journal_error is not None:
            result["cancellation_tombstone_persisted"] = False
        return result

    def _execution_control_lock(self, execution_key: str) -> asyncio.Lock:
        return self._execution_control_locks.setdefault(execution_key, asyncio.Lock())

    def _require_command_owner(self, command: LeasedRunnerCommand) -> RunnerPrincipal:
        owner = self._client_principal()
        if command.target != owner:
            raise RuntimeError(
                f"Runner command {command.id!r} targets a different Runner principal"
            )
        return owner

    def _client_principal(self) -> RunnerPrincipal:
        principal = getattr(self._client, "principal", None)
        if not isinstance(principal, RunnerPrincipal):
            raise RuntimeError("Runner client has no authenticated principal")
        return principal

    @staticmethod
    def _require_local_execution_owner(
        execution: Execution,
        owner: RunnerPrincipal,
        *,
        execution_id: str,
        execution_key: str,
    ) -> None:
        if execution.id != execution_id or execution.execution_key != execution_key:
            raise RuntimeError(
                f"Runner execution identity mismatch: execution key {execution_key!r} "
                f"belongs to local execution {execution.id!r} with key "
                f"{execution.execution_key!r}, not expected execution {execution_id!r}"
            )
        if execution.owner != owner:
            actual = (
                execution.owner.model_dump(mode="json")
                if execution.owner is not None
                else None
            )
            raise RuntimeError(
                f"Runner execution owner mismatch for {execution_id!r}: "
                f"expected {owner.model_dump(mode='json')!r}, found {actual!r}"
            )

    async def _bind_or_require_local_owner(
        self,
        execution: Execution,
        owner: RunnerPrincipal,
        *,
        execution_id: str,
        execution_key: str,
        allow_bind: bool,
    ) -> Execution:
        if execution.id != execution_id or execution.execution_key != execution_key:
            self._require_local_execution_owner(
                execution,
                owner,
                execution_id=execution_id,
                execution_key=execution_key,
            )
        if execution.owner is None:
            if not allow_bind:
                self._require_local_execution_owner(
                    execution,
                    owner,
                    execution_id=execution_id,
                    execution_key=execution_key,
                )
            execution.owner = owner
            execution = await self._executions.save(execution)
        self._require_local_execution_owner(
            execution,
            owner,
            execution_id=execution_id,
            execution_key=execution_key,
        )
        return execution

    async def _require_local_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise RuntimeError(f"Execution {execution_id!r} is not registered on this Runner")
        return execution

    def _start_monitor(self, server_execution_id: str, local_execution_id: str) -> None:
        current = self._monitors.get(server_execution_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._monitor_execution(server_execution_id, local_execution_id),
            name=f"riftx-remote-monitor-{server_execution_id}",
        )
        self._monitors[server_execution_id] = task
        task.add_done_callback(lambda _: self._monitors.pop(server_execution_id, None))

    async def _monitor_execution(
        self,
        server_execution_id: str,
        local_execution_id: str,
    ) -> None:
        cursors = {"stdout": 0, "stderr": 0}
        while not self._closed:
            try:
                cursors = await self._forward_output(
                    server_execution_id,
                    local_execution_id,
                    cursors,
                )
                execution = await self._refresh_local_execution(local_execution_id)
                if execution.status in _TERMINAL_STATUSES:
                    cursors = await self._forward_output(
                        server_execution_id,
                        local_execution_id,
                        cursors,
                    )
                    await self._report_with_retry(server_execution_id, execution)
                    return
            except RunnerControlClientError:
                logger.warning(
                    "Output forwarding failed for execution %s; retrying",
                    server_execution_id,
                )
            await asyncio.sleep(self.config.output_poll_seconds)

    async def resume_active(self) -> None:
        active = list(await self._executions.list_active())
        owner = self._client_principal()
        tombstoned: set[str] = set()
        pending_stops: list[tuple[Execution, asyncio.Task[dict[str, object]]]] = []
        for execution in active:
            if execution.owner != owner:
                logger.error(
                    "Refusing to recover execution %s owned by another Runner principal",
                    execution.id,
                )
                continue
            if await self._execution_cancellations.contains(execution.execution_key):
                tombstoned.add(execution.id)
                stop_task = self._get_or_start_execution_stop(
                    execution.id,
                    execution.execution_key,
                    owner,
                )
                pending_stops.append((execution, stop_task))

        if pending_stops:
            # Start every known safety cleanup before awaiting any one of
            # them. A lost PTY handle must not starve a later live process, and
            # one fail-closed result must not prevent the Runner from polling
            # new safety commands that can resolve the incident.
            outcomes = await asyncio.gather(
                *(asyncio.shield(task) for _, task in pending_stops),
                return_exceptions=True,
            )
            for (execution, _), outcome in zip(pending_stops, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error(
                        "Unable to confirm tombstoned execution %s stopped during "
                        "Runner recovery: %s",
                        execution.id,
                        outcome,
                    )

        for execution in active:
            if execution.owner != owner:
                continue
            if execution.id in tombstoned:
                # Never resume output/activity monitoring for a tombstoned
                # execution, whether its stop succeeded or remains explicitly
                # unconfirmed.
                continue
            if execution.executor_type is not ExecutorType.PTY:
                self._start_monitor(execution.id, execution.id)
        terminal_resume = getattr(self._terminal_handler, "resume_active", None)
        if terminal_resume is not None:
            await terminal_resume()

    async def _refresh_local_execution(self, execution_id: str) -> Execution:
        reconcile = getattr(self._supervisor, "reconcile", None)
        if reconcile is not None:
            return await reconcile(execution_id)
        return await self._supervisor.get(execution_id)

    async def _forward_output(
        self,
        server_execution_id: str,
        local_execution_id: str,
        cursors: dict[str, int],
    ) -> dict[str, int]:
        local = await self._require_local_execution(local_execution_id)
        self._require_local_execution_owner(
            local,
            self._client_principal(),
            execution_id=server_execution_id,
            execution_key=local.execution_key,
        )
        output = await self._supervisor.read_output(
            local_execution_id,
            stdout_cursor=cursors["stdout"],
            stderr_cursor=cursors["stderr"],
            max_bytes=256 * 1024,
        )
        for stream, item in (("stdout", output.stdout), ("stderr", output.stderr)):
            if not item.data:
                continue
            try:
                cursors[stream] = await self._client.report_output(
                    server_execution_id,
                    stream=stream,
                    offset=item.cursor,
                    data=item.data,
                )
            except OutputOffsetMismatch as exc:
                cursors[stream] = exc.expected_offset
        return cursors

    async def _report_with_retry(
        self,
        server_execution_id: str,
        execution: Execution,
    ) -> None:
        while not self._closed:
            try:
                await self._report_execution(server_execution_id, execution)
                return
            except RunnerControlClientError:
                await asyncio.sleep(self.config.reconnect_initial_seconds)

    async def _report_execution(
        self,
        server_execution_id: str,
        execution: Execution,
    ) -> None:
        self._require_local_execution_owner(
            execution,
            self._client_principal(),
            execution_id=server_execution_id,
            execution_key=execution.execution_key,
        )
        await self._client.report_status(
            server_execution_id,
            execution.status,
            pid=execution.pid if execution.status is ExecutionStatus.RUNNING else None,
            process_group_id=(
                execution.process_group_id if execution.status is ExecutionStatus.RUNNING else None
            ),
            exit_code=(execution.exit_code if execution.status in _TERMINAL_STATUSES else None),
            executable_path=execution.executable_path,
            tool_id=execution.tool_id,
            tool_version=execution.tool_version,
            platform_system=execution.platform_system,
            platform_release=execution.platform_release,
            platform_architecture=execution.platform_architecture,
            process_created_at=(
                execution.process_created_at.isoformat()
                if execution.process_created_at is not None
                else None
            ),
            physical_stop_confirmed=(execution.physical_stop_confirmed_at is not None),
        )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Runner command is missing {key}")
    return value


def _runner_principal(value: object) -> RunnerPrincipal:
    try:
        return RunnerPrincipal.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Runner command request is missing a valid runner_principal") from exc


def _lease_remaining_seconds(
    lease_expires_at: datetime | None,
    *,
    fallback: float,
) -> float:
    if lease_expires_at is None:
        return fallback
    expiry = lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return max(0.001, (expiry - datetime.now(UTC)).total_seconds())


def _renewed_lease_seconds(value: object, *, fallback: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
        if seconds <= 0:
            raise RuntimeError("Control Plane returned a non-positive command lease")
        return seconds
    if isinstance(value, datetime):
        return _lease_remaining_seconds(value, fallback=fallback)
    raise RuntimeError("Control Plane returned an invalid command lease")


def _target_http_cancellation_key(run_id: str, tool_call_id: str) -> str:
    return f"target-http:{run_id}:{tool_call_id}"


def _browser_cancellation_key(session_id: str) -> str:
    return f"browser:{session_id}"


async def run_runner_daemon(config: RunnerDaemonConfig) -> None:
    config.state_path.mkdir(parents=True, exist_ok=True)
    executions = FileExecutionRepository(config.state_path / "executions.json")
    terminals = FileTerminalRepository(config.state_path / "terminals.json")
    runner_paths = RunnerPaths(config.state_path)
    # Process and PTY work must resolve durable containment identities in the
    # same delegated kernel namespace. Production defaults fail closed when a
    # trustworthy containment backend is unavailable.
    containment_manager = LinuxCgroupV2Manager.autodetect(
        payload_uid=config.payload_uid,
        payload_gid=config.payload_gid,
    )
    process_executor = DirectProcessExecutor(
        containment_manager=containment_manager,
        autodetect_containment=False,
        require_containment=config.require_containment,
        defer_activation=True,
    )
    supervisor = ProcessSupervisor(
        executions,
        runner_paths,
        process_executor=process_executor,
    )
    client = RunnerControlClient(
        server_url=config.server_url,
        node_id=config.node_id,
        credentials=RunnerCredentialStore(config.credential_path),
        registration_token=config.registration_token,
    )
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminals,
        execution_repository=executions,
        event_repository=NullRunEventRepository(),
        paths=runner_paths,
        containment_manager=process_executor.containment_manager,
        autodetect_containment=False,
        require_containment=config.require_containment,
    )
    terminal_manager = RemoteTerminalManager(
        node_id=config.node_id,
        supervisor=terminal_supervisor,
        terminals=terminals,
        executions=executions,
        client=client,
        operation_journal=OperationJournal(config.state_path / "terminal-operations.json"),
        output_poll_seconds=config.output_poll_seconds,
    )
    browser_manager = RunnerBrowserManager(
        node_id=config.node_id,
        paths=runner_paths,
    )
    daemon = RunnerDaemon(
        config=config,
        client=client,
        supervisor=supervisor,
        executions=executions,
        terminal_handler=terminal_manager,
        target_http_handler=RunnerTargetHttpClient(node_id=config.node_id),
        browser_handler=browser_manager,
    )
    try:
        await daemon.run_forever()
    finally:
        await daemon.close()


@app.command()
def serve(
    server_url: Annotated[
        str, typer.Option(envvar="RIFTX_SERVER_URL", help="RiftX Control Plane URL.")
    ] = "http://127.0.0.1:8787",
    node_id: Annotated[
        str, typer.Option(envvar="RIFTX_NODE_ID", help="Stable Runner node ID.")
    ] = platform_module.node() or "runner",
    name: Annotated[
        str, typer.Option(envvar="RIFTX_NODE_NAME", help="Display name.")
    ] = platform_module.node() or "RiftX Runner",
    state_path: Annotated[
        Path, typer.Option(envvar="RIFTX_RUNNER_STATE", help="Runner state directory.")
    ] = Path(".riftx/remote-runner"),
    credential_path: Annotated[
        Path,
        typer.Option(
            envvar="RIFTX_RUNNER_CREDENTIALS",
            help="Runner credential file, kept outside execution state.",
        ),
    ] = Path(".riftx/secrets/runner-credentials.json"),
    registration_token: Annotated[
        str | None,
        typer.Option(
            envvar="RIFTX_RUNNER_REGISTRATION_TOKEN",
            help="Bootstrap registration token.",
        ),
    ] = None,
    require_containment: Annotated[
        bool,
        typer.Option(
            envvar="RIFTX_REQUIRE_CONTAINMENT",
            help="Require kernel-backed containment for process and terminal work.",
        ),
    ] = True,
    payload_uid: Annotated[
        int | None,
        typer.Option(
            envvar="RIFTX_PAYLOAD_UID",
            min=1,
            help="UID used for isolated execution payloads; configure with payload GID.",
        ),
    ] = None,
    payload_gid: Annotated[
        int | None,
        typer.Option(
            envvar="RIFTX_PAYLOAD_GID",
            min=1,
            help="GID used for isolated execution payloads; configure with payload UID.",
        ),
    ] = None,
) -> None:
    """Connect to a Control Plane and execute commands on this host."""

    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_runner_daemon(
            RunnerDaemonConfig(
                server_url=server_url,
                node_id=node_id,
                name=name,
                state_path=state_path,
                credential_path=credential_path,
                registration_token=registration_token,
                require_containment=require_containment,
                payload_uid=payload_uid,
                payload_gid=payload_gid,
            )
        )
    )


if __name__ == "__main__":
    app()
