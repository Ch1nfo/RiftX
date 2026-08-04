"""Standalone outbound RiftX Runner daemon."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import platform as platform_module
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol, TypedDict, cast

import typer

from riftx import __version__
from riftx.application.ports import ExecutionRepository
from riftx.application.services import NodeRegistration
from riftx.browser import (
    BrowserOperation,
    BrowserSessionCommand,
)
from riftx.config import (
    AuditConfig,
    RiftXConfigError,
    audit_source_ingest_policy_digest,
    load_riftx_config,
)
from riftx.domain import (
    AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY,
    RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION,
    BrowserSessionStatus,
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerPrincipal,
    RunnerResourceKind,
    runner_command_payload_binding_invalid_fields,
    runner_command_protocol,
    runner_payload_digest,
)
from riftx.executors import (
    DirectProcessExecutor,
    LinuxCgroupV2Manager,
    PowerShellNotFoundError,
    PowerShellResolver,
)
from riftx.security import validate_runner_registration_credential
from riftx.target_http.models import (
    TargetHttpRequest,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
)

from .audit_preflight import AuditPreflightRunner
from .browser import BrowserRunner, RunnerBrowserManager, execute_browser_command
from .control_client import (
    LeasedRunnerCommand,
    OutputLimitExceeded,
    OutputOffsetMismatch,
    RunnerControlClient,
    RunnerControlClientError,
    RunnerCredentialStore,
)
from .models import ExecutionLaunchRequest, TerminalLaunchRequest
from .paths import RunnerPaths
from .protocols import EffectGuard, ExecutionRunner
from .state import FileExecutionRepository, FileTerminalRepository
from .supervisor import ProcessSupervisor
from .target_http import RunnerTargetHttpClient, TargetHttpRunner
from .terminal import TerminalSupervisor
from .terminal_manager import (
    NullRunEventRepository,
    OperationJournal,
    OperationJournalConflict,
    OperationJournalIdentity,
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
_AUDIT_READINESS_LABELS = frozenset(
    {
        "audit_source_ingest_available",
        "audit_source_ingest_backend_id",
        "audit_source_ingest_image_digest",
        "audit_source_ingest_policy_digest",
    }
)


class _ExecutionCallbackKwargs(TypedDict):
    runner_command_id: str
    runner_effect_binding_id: str
    runner_binding_digest: str
    runner_envelope_digest: str


class TerminalCommandHandler(Protocol):
    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity: OperationJournalIdentity | None = None,
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


def _default_audit_config() -> AuditConfig:
    # Re-validate explicit default values so platform path aliases (for
    # example macOS /var -> /private/var) match the shared config loader.
    return AuditConfig.model_validate(AuditConfig().model_dump(mode="python"))


@dataclass(frozen=True, slots=True)
class RunnerDaemonConfig:
    server_url: str
    node_id: str
    name: str
    state_path: Path
    credential_path: Path = Path(".riftx/secrets/runner-credentials.json")
    registration_token: str | None = field(default=None, repr=False)
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
    audit: AuditConfig = field(default_factory=_default_audit_config)
    audit_preflight_ready: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        validate_runner_registration_credential(self.registration_token)
        if self.command_lease_seconds <= 0:
            raise ValueError("Runner command lease duration must be positive")
        if self.max_concurrent_commands < 1:
            raise ValueError("Runner command concurrency must be positive")
        if self.resource_stop_timeout_seconds <= 0:
            raise ValueError("Runner resource stop timeout must be positive")
        if (self.payload_uid is None) != (self.payload_gid is None):
            raise ValueError("payload_uid and payload_gid must be configured together")
        if self.audit.enabled and self.node_id != "local":
            raise ValueError("RiftX Code Audit requires the local Runner node")
        if self.audit_preflight_ready and (
            not self.audit.enabled or self.audit.source_ingest.image_digest is None
        ):
            raise ValueError("Audit Preflight readiness requires enabled Audit and a pinned image")
        for field_name, value in (
            ("payload_uid", self.payload_uid),
            ("payload_gid", self.payload_gid),
        ):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{field_name} must be a positive integer")

    @property
    def registration(self) -> NodeRegistration:
        capabilities = set(self.capabilities)
        capabilities.discard(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY)
        configured_labels = {
            key: value
            for key, value in (self.labels or {}).items()
            if key not in _AUDIT_READINESS_LABELS
        }
        labels = {
            "mode": "remote",
            "shell": os.environ.get("SHELL") or os.environ.get("COMSPEC", "unknown"),
            "working_directory": str(Path.cwd()),
            "tool_count": "0",
            **configured_labels,
        }
        if self.audit_preflight_ready:
            image_digest = self.audit.source_ingest.image_digest
            assert image_digest is not None
            capabilities.add(AUDIT_PREFLIGHT_JOB_OWNER_CAPABILITY)
            labels.update(
                {
                    "audit_source_ingest_available": "true",
                    "audit_source_ingest_backend_id": (self.audit.source_ingest.backend_id),
                    "audit_source_ingest_image_digest": image_digest,
                    "audit_source_ingest_policy_digest": (
                        audit_source_ingest_policy_digest(self.audit.source_ingest)
                    ),
                }
            )
        return NodeRegistration(
            node_id=self.node_id,
            name=self.name,
            platform=self.platform,
            architecture=self.architecture,
            runner_version=self.runner_version,
            capabilities=tuple(sorted(capabilities)),
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
        audit_preflight_runner: AuditPreflightRunner | None = None,
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
        self._audit_preflight_runner = audit_preflight_runner
        self._execution_cancellations = execution_cancellation_journal or OperationJournal(
            config.state_path / "execution-cancellations.json",
            # Pre-ownership journals stored arbitrary execution_key strings.
            # Keep those tombstones in a structural legacy namespace so they
            # cannot collide with typed Execution-ID resources or command
            # attempt records during the in-place format upgrade.
            legacy_list_resources=True,
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
            tuple[str, str, str, int, str, str],
            asyncio.Task[dict[str, object]],
        ] = {}
        self._resource_stop_tasks: dict[
            tuple[str, str, str],
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
                if self._audit_preflight_runner is not None:
                    await self._audit_preflight_runner.start()
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

        def command_finished(
            completed: asyncio.Task[None],
            *,
            command_id: str = command.id,
        ) -> None:
            self._command_finished(command_id, completed)

        task.add_done_callback(command_finished)

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
                handler_done, _ = await asyncio.wait(
                    {handler},
                    timeout=min(renew_interval, current_remaining),
                )
                if handler_done:
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
                lease_tasks: set[asyncio.Task[object]] = {handler, renewal}
                lease_done, _ = await asyncio.wait(
                    lease_tasks,
                    timeout=current_remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if handler in lease_done:
                    break
                if renewal not in lease_done:
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
            owned_tasks: list[asyncio.Task[object]] = [handler]
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
            # Poll parsing proves envelope self-consistency, but handler
            # admission must also prove that the typed effect binding actually
            # names the operation and payload about to touch local resources.
            # Keep this pure validation before every repository, journal,
            # process, browser, terminal, or network access.
            self._require_command_effect_binding(command)
            if command.kind is RunnerCommandKind.EXECUTE:
                result = await self._handle_execute(command, owner)
            elif command.kind is RunnerCommandKind.CANCEL:
                result = await self._handle_cancel(command, owner)
            elif command.kind is RunnerCommandKind.TERMINAL_CLOSE:
                session_id = _required_string(command.payload, "session_id")
                execution_key = command.payload.get("execution_key")
                if not isinstance(execution_key, str) or not execution_key:
                    execution_key = f"terminal:{session_id}"
                result = await self._handle_cancel(
                    command,
                    owner,
                    execution_id=_required_string(command.payload, "execution_id"),
                    execution_key=execution_key,
                )
            elif command.kind is RunnerCommandKind.TARGET_HTTP:
                result = await self._handle_target_http(command)
            elif command.kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
                result = await self._handle_target_http_cancel(command)
            elif command.kind is RunnerCommandKind.BROWSER:
                if command.payload.get("operation") == BrowserOperation.CLOSE.value:
                    result = await self._handle_browser_close(command)
                else:
                    result = await self._handle_browser(command)
            elif command.kind is RunnerCommandKind.BROWSER_CLOSE:
                result = await self._handle_browser_close(command)
            elif command.kind in {
                RunnerCommandKind.TERMINAL_START,
                RunnerCommandKind.TERMINAL_WRITE,
                RunnerCommandKind.TERMINAL_RESIZE,
                RunnerCommandKind.TERMINAL_INTERRUPT,
            }:
                terminal_result = await self._handle_terminal(command, owner)
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
                await self._report_durable_terminal_start_status(command)
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
        command: LeasedRunnerCommand,
        owner: RunnerPrincipal,
    ) -> dict[str, object]:
        kind = command.kind
        payload = command.payload
        effect_binding = self._require_command_effect_binding(command)
        if self._terminal_handler is None:
            raise RuntimeError(f"Runner does not support command {kind.value!r}")
        if kind is RunnerCommandKind.TERMINAL_START:
            session_id = _required_string(payload, "session_id")
            execution_id = _required_string(payload, "execution_id")
            execution_key = f"terminal:{session_id}"
            raw_request = payload.get("request")
            if not isinstance(raw_request, dict):
                raise ValueError("terminal_start command is missing request")
            request = TerminalLaunchRequest.model_validate(raw_request)
            callback_binding = self._require_execution_launch_binding(
                command,
                execution_id=execution_id,
                expected_family=RunnerOperationFamily.TERMINAL,
                expected_resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                expected_resource_id=session_id,
            )
            self._require_request_callback_binding_compatible(request, callback_binding)
            bound_request = request.model_copy(update=callback_binding)
            bound_payload = {
                **payload,
                "request": bound_request.model_dump(mode="json"),
            }
            raw_execution_key = request.execution_key
            if raw_execution_key is None:
                execution_key = f"terminal:{session_id}"
            elif isinstance(raw_execution_key, str) and raw_execution_key:
                execution_key = raw_execution_key
            else:
                raise ValueError("terminal_start request has an invalid execution key")
            declared_owner = request.runner_principal
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
                if await self._execution_cancellation_exists(
                    execution_id,
                    execution_key,
                ):
                    raise RuntimeError(
                        f"Terminal execution {execution_id!r} was cancelled on this Runner"
                    )

            try:
                existing = await self._executions.get_by_key(execution_key)
                if existing is not None:
                    self._require_local_execution_effect_binding(
                        existing,
                        effect_binding,
                        execution_id=execution_id,
                        execution_key=execution_key,
                    )
                    self._require_local_execution_callback_binding(
                        existing,
                        callback_binding,
                    )
                if await self._execution_cancellation_exists(
                    execution_id,
                    execution_key,
                ):
                    return {
                        "execution_id": execution_id,
                        "status": "suppressed",
                        "suppressed_by_cancellation": True,
                        "physical_stop_confirmed": False,
                    }
                raw_result = await self._terminal_handler.handle(
                    kind,
                    bound_payload,
                    effect_guard=effect_guard,
                    on_admitted=on_admitted,
                )
                result = _require_command_result(raw_result, operation="terminal_start")
                local = await self._executions.get_by_key(execution_key)
                if local is None:
                    raise RuntimeError(
                        f"Terminal start {execution_id!r} did not persist local execution state"
                    )
                local = await self._bind_or_require_local_owner(
                    local,
                    owner,
                    execution_id=execution_id,
                    execution_key=execution_key,
                    allow_bind=existing is None,
                )
                self._require_local_execution_effect_binding(
                    local,
                    effect_binding,
                    execution_id=execution_id,
                    execution_key=execution_key,
                )
                self._require_local_execution_callback_binding(local, callback_binding)
                return result
            finally:
                if not admitted:
                    lock.release()
        execution_id = _required_string(payload, "execution_id")
        local = await self._require_local_execution(execution_id)
        self._require_local_execution_effect_binding(
            local,
            effect_binding,
            execution_id=execution_id,
            execution_key=local.execution_key,
        )
        raw_result = await self._terminal_handler.handle(
            kind,
            payload,
            journal_identity=self._operation_journal_identity(command),
        )
        return _require_command_result(raw_result, operation=kind.value)

    async def _report_durable_terminal_start_status(
        self,
        command: LeasedRunnerCommand,
    ) -> None:
        """Retry the real local status after a terminal-start command failure.

        A TERMINAL_START handler can fail after the PTY has been spawned, for
        example when its first status upload loses the connection. The command
        exception therefore says nothing about the native process lifecycle;
        only the Runner-local durable execution is safe to report.
        """

        payload = command.payload
        execution_id = payload.get("execution_id")
        session_id = payload.get("session_id")
        if not isinstance(execution_id, str) or not execution_id:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            raw_request = payload.get("request")
            if not isinstance(raw_request, dict):
                return
            raw_execution_key = raw_request.get("execution_key")
            if raw_execution_key is None:
                execution_key = f"terminal:{session_id}"
            elif isinstance(raw_execution_key, str) and raw_execution_key:
                execution_key = raw_execution_key
            else:
                return
            # The daemon-owned stop task is authoritative once the tombstone
            # exists. Do not race it by uploading a stale STARTING/RUNNING
            # snapshot from the failed start handler.
            if await self._execution_cancellation_exists(execution_id, execution_key):
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
        journal_identity = self._operation_journal_identity(command)

        async def effect_guard() -> None:
            if await self._target_http_cancellations.contains(cancellation_key):
                raise RuntimeError("Target HTTP request was cancelled on this Runner")

        await effect_guard()
        if not await self._target_http_deliveries.claim(
            cancellation_key,
            journal_identity,
            outcome={"state": "delivery_claimed"},
        ):
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
        command: LeasedRunnerCommand,
    ) -> dict[str, object]:
        journal_identity = self._operation_journal_identity(command)
        task = self._get_or_start_resource_stop(
            "target_http",
            journal_identity,
            self._stop_target_http_durably(command, journal_identity),
        )
        return await asyncio.shield(task)

    async def _stop_target_http_durably(
        self,
        command: LeasedRunnerCommand,
        journal_identity: OperationJournalIdentity,
    ) -> dict[str, object]:
        if self._target_http_handler is None:
            raise RuntimeError("Runner does not support Target HTTP")
        payload = command.payload
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
        journal_conflicts: set[str] = set()
        previously_confirmed: set[str] = set()
        for tool_call_id in tool_call_ids:
            cancellation_key = _target_http_cancellation_key(run_id, tool_call_id)
            try:
                resource_tombstone = None
                existing = await self._target_http_cancellations.get_resource_attempt_exact(
                    cancellation_key,
                    journal_identity,
                )
                if existing is None:
                    _, resource_tombstone = await self._target_http_cancellations.claim_resource(
                        cancellation_key,
                        journal_identity,
                        outcome={"state": "cancellation_requested"},
                    )
                else:
                    resource_tombstone = await self._target_http_cancellations.get_resource(
                        cancellation_key
                    )
                if (
                    existing is not None
                    and existing.outcome.get("state") == "physical_stop_confirmed"
                ):
                    previously_confirmed.add(tool_call_id)
                elif existing is not None and existing.outcome != {
                    "state": "cancellation_requested"
                }:
                    raise OperationJournalConflict(cancellation_key)
                elif (
                    resource_tombstone is not None
                    and resource_tombstone.outcome.get("state") == "physical_stop_confirmed"
                ):
                    durable_result = resource_tombstone.outcome.get("result")
                    if not isinstance(durable_result, dict):
                        raise RuntimeError(
                            "Target HTTP resource tombstone has an invalid durable outcome"
                        )
                    await self._target_http_cancellations.transition_resource(
                        cancellation_key,
                        journal_identity,
                        expected_outcome={"state": "cancellation_requested"},
                        outcome={
                            "state": "physical_stop_confirmed",
                            "result": durable_result,
                        },
                        resource_outcome=resource_tombstone.outcome,
                    )
                    previously_confirmed.add(tool_call_id)
            except OperationJournalConflict:
                journal_conflicts.add(tool_call_id)
            except Exception as exc:
                journal_errors[tool_call_id] = f"{type(exc).__name__}: {exc}"

        if journal_conflicts:
            # A copied command identity with drifted ownership is not a new
            # safety attempt. The pre-existing resource tombstone remains the
            # no-restart fence; reject before touching the physical resource.
            conflicting_id = next(iter(sorted(journal_conflicts)))
            raise OperationJournalConflict(_target_http_cancellation_key(run_id, conflicting_id))

        for tool_call_id in tool_call_ids:
            if (
                tool_call_id in journal_errors
                or tool_call_id in journal_conflicts
                or tool_call_id in previously_confirmed
            ):
                continue
            confirmation_key = _target_http_cancellation_key(run_id, tool_call_id)
            if await self._target_http_stop_was_confirmed(
                confirmation_key,
            ):
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
                    durable_outcome: dict[str, object] = {
                        "state": "physical_stop_confirmed",
                        "result": outcome.model_dump(mode="json"),
                    }
                    await self._target_http_cancellations.transition_resource(
                        confirmation_key,
                        journal_identity,
                        expected_outcome={"state": "cancellation_requested"},
                        outcome=durable_outcome,
                        resource_outcome=durable_outcome,
                    )
                    await self._target_http_stop_confirmations.claim_resource(
                        confirmation_key,
                        journal_identity,
                        outcome={"state": "cancellation_requested"},
                    )
                    await self._target_http_stop_confirmations.transition_resource(
                        confirmation_key,
                        journal_identity,
                        expected_outcome={"state": "cancellation_requested"},
                        outcome=durable_outcome,
                        resource_outcome=durable_outcome,
                    )
                except Exception as exc:
                    logger.warning(
                        "Unable to persist Target HTTP physical-stop confirmation for %s: %s",
                        confirmation_key,
                        exc,
                    )
                    outcomes.append(
                        TargetHttpRunnerStopOutcome(
                            tool_call_id=tool_call_id,
                            confirmed=False,
                            reason="target_http_stop_confirmation_persistence_failed",
                        )
                    )
                    continue
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
                if await self._target_http_stop_was_confirmed(
                    confirmation_key,
                ):
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

    async def _target_http_stop_was_confirmed(
        self,
        confirmation_key: str,
    ) -> bool:
        try:
            tombstone = await self._target_http_stop_confirmations.get_resource(confirmation_key)
            return tombstone is not None and tombstone.outcome.get("state") == (
                "physical_stop_confirmed"
            )
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

    async def _handle_browser_close(
        self,
        command: LeasedRunnerCommand,
    ) -> dict[str, object]:
        journal_identity = self._operation_journal_identity(command)
        task = self._get_or_start_resource_stop(
            "browser",
            journal_identity,
            self._close_browser_durably(command, journal_identity),
        )
        return await asyncio.shield(task)

    async def _close_browser_durably(
        self,
        command: LeasedRunnerCommand,
        journal_identity: OperationJournalIdentity,
    ) -> dict[str, object]:
        if self._browser_handler is None:
            raise RuntimeError("Runner does not support managed browser commands")
        payload = command.payload
        raw_command = payload.get("command")
        if not isinstance(raw_command, dict):
            raise ValueError("Browser close command is missing session data")
        close_command = BrowserSessionCommand.model_validate(raw_command)
        cancellation_key = _browser_cancellation_key(close_command.session_id)
        journal_error: Exception | None = None
        try:
            resource_tombstone = None
            existing = await self._browser_cancellations.get_resource_attempt_exact(
                cancellation_key, journal_identity
            )
            if existing is None:
                _, resource_tombstone = await self._browser_cancellations.claim_resource(
                    cancellation_key, journal_identity, outcome={"state": "cancellation_requested"}
                )
            else:
                resource_tombstone = await self._browser_cancellations.get_resource(
                    cancellation_key
                )
            if existing is not None and existing.outcome.get("state") == "physical_stop_confirmed":
                durable_result = existing.outcome.get("result")
                if not isinstance(durable_result, dict):
                    raise RuntimeError("Browser cancellation journal has an invalid outcome")
                return _require_command_result(
                    durable_result,
                    operation="browser_close_replay",
                )
            if existing is not None and existing.outcome != {"state": "cancellation_requested"}:
                raise OperationJournalConflict(cancellation_key)
            if (
                resource_tombstone is not None
                and resource_tombstone.outcome.get("state") == "physical_stop_confirmed"
            ):
                durable_result = resource_tombstone.outcome.get("result")
                if not isinstance(durable_result, dict):
                    raise RuntimeError("Browser resource tombstone has an invalid outcome")
                await self._browser_cancellations.transition_resource(
                    cancellation_key,
                    journal_identity,
                    expected_outcome={"state": "cancellation_requested"},
                    outcome={
                        "state": "physical_stop_confirmed",
                        "result": durable_result,
                    },
                    resource_outcome=resource_tombstone.outcome,
                )
                return _require_command_result(
                    durable_result,
                    operation="browser_close_resource_replay",
                )
        except OperationJournalConflict:
            raise
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
        result: dict[str, object] = {"result": exchange.result.model_dump(mode="json")}
        if journal_error is not None:
            raise RuntimeError(
                "Browser closed locally, but its cancellation tombstone could not be "
                "persisted; a delayed open may restart it"
            ) from journal_error
        durable_outcome: dict[str, object] = {
            "state": "physical_stop_confirmed",
            "result": result,
        }
        await self._browser_cancellations.transition_resource(
            cancellation_key,
            journal_identity,
            expected_outcome={"state": "cancellation_requested"},
            outcome=durable_outcome,
            resource_outcome=durable_outcome,
        )
        return result

    def _get_or_start_resource_stop(
        self,
        family: str,
        journal_identity: OperationJournalIdentity,
        operation: Coroutine[object, object, dict[str, object]],
    ) -> asyncio.Task[dict[str, object]]:
        task_key = (
            family,
            journal_identity.binding_digest,
            journal_identity.envelope_digest,
        )
        existing = self._resource_stop_tasks.get(task_key)
        if existing is not None and not existing.done():
            # The caller created the coroutine before discovering the existing
            # task. Close it explicitly so exact concurrent re-leases do not
            # leak an un-awaited coroutine.
            operation.close()
            return existing
        task = asyncio.create_task(
            operation,
            name=f"riftx-runner-{family}-stop",
        )
        self._resource_stop_tasks[task_key] = task

        def resource_stop_finished(
            completed: asyncio.Task[dict[str, object]],
            *,
            key: tuple[str, str, str] = task_key,
        ) -> None:
            self._resource_stop_finished(key, completed)

        task.add_done_callback(resource_stop_finished)
        return task

    def _resource_stop_finished(
        self,
        task_key: tuple[str, str, str],
        task: asyncio.Task[dict[str, object]],
    ) -> None:
        if self._resource_stop_tasks.get(task_key) is task:
            self._resource_stop_tasks.pop(task_key, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def close(self) -> None:
        self._closed = True
        command_tasks = list(self._command_tasks.values())
        for task in command_tasks:
            task.cancel()
        await asyncio.gather(*command_tasks, return_exceptions=True)
        # Do not cancel safety cleanup with the command handlers. In
        # particular, a CANCEL lease can expire immediately after its durable
        # tombstone is written; the physical stop still has to finish.
        stop_tasks = [
            *self._execution_stop_tasks.values(),
            *self._resource_stop_tasks.values(),
        ]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        audit_results: list[object] = []
        if self._audit_preflight_runner is not None:
            audit_results = list(
                await asyncio.gather(
                    self._audit_preflight_runner.close(),
                    return_exceptions=True,
                )
            )

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
            for result in (*audit_results, *stop_results, *client_results)
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
        callback_binding = self._require_execution_launch_binding(
            command,
            execution_id=server_execution_id,
            expected_family=RunnerOperationFamily.EXECUTION,
            expected_resource_kind=RunnerResourceKind.EXECUTION,
            expected_resource_id=server_execution_id,
        )
        effect_binding = self._require_command_effect_binding(command)
        self._require_request_callback_binding_compatible(request, callback_binding)
        request = request.model_copy(
            update={"execution_id": server_execution_id, **callback_binding}
        )
        if request.node_id != self.config.node_id:
            raise ValueError("execute command targets a different Runner node")

        async def effect_guard() -> None:
            if await self._execution_cancellation_exists(
                server_execution_id,
                request.execution_key,
            ):
                raise RuntimeError(
                    f"Execution {server_execution_id!r} was cancelled on this Runner"
                )

        async with self._execution_control_lock(request.execution_key):
            existing = await self._executions.get_by_key(request.execution_key)
            if existing is not None:
                self._require_local_execution_effect_binding(
                    existing,
                    effect_binding,
                    execution_id=server_execution_id,
                    execution_key=request.execution_key,
                )
                self._require_local_execution_callback_binding(existing, callback_binding)
            if await self._execution_cancellation_exists(
                server_execution_id,
                request.execution_key,
            ):
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
            self._require_local_execution_effect_binding(
                execution,
                effect_binding,
                execution_id=server_execution_id,
                execution_key=request.execution_key,
            )
            self._require_local_execution_callback_binding(execution, callback_binding)
        # Admission is complete. Never keep the execution-control lock across
        # Control Plane I/O: a blocked status upload must not delay a tombstoned
        # safety stop.
        if execution.status in _TERMINAL_STATUSES:
            if existing is None:
                await self._client.report_status(
                    server_execution_id,
                    ExecutionStatus.RUNNING,
                    **self._execution_callback_kwargs(execution),
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
        command: LeasedRunnerCommand,
        owner: RunnerPrincipal,
        *,
        execution_id: str | None = None,
        execution_key: str | None = None,
    ) -> dict[str, object]:
        payload = command.payload
        server_execution_id = execution_id or _required_string(payload, "execution_id")
        resolved_execution_key = execution_key or _required_string(payload, "execution_key")
        journal_identity = self._operation_journal_identity(command)
        effect_binding = self._require_command_effect_binding(command)
        task = self._get_or_start_execution_stop(
            server_execution_id,
            resolved_execution_key,
            owner,
            journal_identity=journal_identity,
            effect_binding=effect_binding,
        )
        return await asyncio.shield(task)

    def _get_or_start_execution_stop(
        self,
        server_execution_id: str,
        execution_key: str,
        owner: RunnerPrincipal,
        *,
        journal_identity: OperationJournalIdentity | None = None,
        effect_binding: RunnerEffectBinding | None = None,
    ) -> asyncio.Task[dict[str, object]]:
        task_key = (
            server_execution_id,
            execution_key,
            owner.instance_id,
            owner.epoch,
            journal_identity.binding_digest if journal_identity is not None else "",
            journal_identity.envelope_digest if journal_identity is not None else "",
        )
        existing = self._execution_stop_tasks.get(task_key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._stop_execution_durably(
                server_execution_id,
                execution_key,
                owner,
                journal_identity=journal_identity,
                effect_binding=effect_binding,
            ),
            name=f"riftx-runner-stop-{server_execution_id}",
        )
        self._execution_stop_tasks[task_key] = task

        def execution_stop_finished(
            completed: asyncio.Task[dict[str, object]],
            *,
            key: tuple[str, str, str, int, str, str] = task_key,
        ) -> None:
            self._execution_stop_finished(key, completed)

        task.add_done_callback(execution_stop_finished)
        return task

    def _execution_stop_finished(
        self,
        task_key: tuple[str, str, str, int, str, str],
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
        *,
        journal_identity: OperationJournalIdentity | None,
        effect_binding: RunnerEffectBinding | None,
    ) -> dict[str, object]:
        journal_key = execution_key
        if effect_binding is not None:
            if effect_binding.execution_id is None:  # pragma: no cover - admission guard
                raise RuntimeError("Execution stop binding omitted its execution identity")
            journal_key = _execution_cancellation_key(effect_binding.execution_id)
        # Fence obvious stale/copied state before writing a tombstone.  The
        # read is intentionally outside the execution lock: an in-flight
        # start holds that lock while its final effect guard runs, and the
        # tombstone must be able to reach that guard before cancellation waits
        # for admission to finish.
        local_by_id = await self._executions.get(server_execution_id)
        local = await self._executions.get_by_key(execution_key)
        if local_by_id is not None:
            if effect_binding is None:
                self._require_local_execution_owner(
                    local_by_id,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            else:
                self._require_local_execution_effect_binding(
                    local_by_id,
                    effect_binding,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            if local is None or local.id != local_by_id.id:
                raise RuntimeError(
                    f"Runner execution {server_execution_id!r} does not own payload "
                    f"execution key {execution_key!r}"
                )
        if local is not None:
            if effect_binding is None:
                self._require_local_execution_owner(
                    local,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            else:
                self._require_local_execution_effect_binding(
                    local,
                    effect_binding,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )

        cancellation_journal_error: Exception | None = None
        if journal_identity is not None:
            try:
                resource_tombstone = None
                existing_attempt = await self._execution_cancellations.get_resource_attempt_exact(
                    journal_key,
                    journal_identity,
                )
                if existing_attempt is None:
                    _, resource_tombstone = await self._execution_cancellations.claim_resource(
                        journal_key,
                        journal_identity,
                        outcome={"state": "cancellation_requested"},
                    )
                else:
                    resource_tombstone = await self._execution_cancellations.get_resource(
                        journal_key
                    )
                if (
                    existing_attempt is not None
                    and existing_attempt.outcome.get("state") == "physical_stop_confirmed"
                ):
                    durable_result = existing_attempt.outcome.get("result")
                    if not isinstance(durable_result, dict):
                        raise RuntimeError(
                            "Execution cancellation journal has an invalid durable outcome"
                        )
                    return self._bind_execution_stop_result_resource(
                        durable_result,
                        effect_binding,
                    )
                if existing_attempt is not None and existing_attempt.outcome != {
                    "state": "cancellation_requested"
                }:
                    raise OperationJournalConflict(journal_key)
                if (
                    resource_tombstone is not None
                    and resource_tombstone.outcome.get("state") == "physical_stop_confirmed"
                ):
                    durable_result = resource_tombstone.outcome.get("result")
                    if not isinstance(durable_result, dict):
                        raise RuntimeError(
                            "Execution resource tombstone has an invalid durable outcome"
                        )
                    await self._execution_cancellations.transition_resource(
                        journal_key,
                        journal_identity,
                        expected_outcome={"state": "cancellation_requested"},
                        outcome={
                            "state": "physical_stop_confirmed",
                            "result": durable_result,
                        },
                        resource_outcome=resource_tombstone.outcome,
                    )
                    return self._bind_execution_stop_result_resource(
                        durable_result,
                        effect_binding,
                    )
            except OperationJournalConflict:
                raise
            except Exception as exc:
                cancellation_journal_error = exc
                logger.exception(
                    "Unable to persist cancellation tombstone for execution key %s; "
                    "continuing with local process termination",
                    journal_key,
                )
        elif not await self._execution_cancellation_exists(
            server_execution_id,
            execution_key,
        ):
            raise RuntimeError(
                f"Execution {server_execution_id!r} has no durable cancellation tombstone"
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
            if effect_binding is None:
                self._require_local_execution_owner(
                    local,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            else:
                self._require_local_execution_effect_binding(
                    local,
                    effect_binding,
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
            if effect_binding is None:
                self._require_local_execution_owner(
                    execution,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            else:
                self._require_local_execution_effect_binding(
                    execution,
                    effect_binding,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            durable_execution = await self._executions.get_by_key(execution_key)
            if durable_execution is None or durable_execution.id != server_execution_id:
                raise RuntimeError(
                    f"Cancellation for execution {server_execution_id!r} is unconfirmed: "
                    "the stopped outcome was not found in durable Runner state"
                )
            if effect_binding is None:
                self._require_local_execution_owner(
                    durable_execution,
                    owner,
                    execution_id=server_execution_id,
                    execution_key=execution_key,
                )
            else:
                self._require_local_execution_effect_binding(
                    durable_execution,
                    effect_binding,
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
        # safety attempt or startup guard. Pre-fencing legacy rows deliberately
        # have no launch callback binding: their fresh verified replacement
        # safety command must still be able to return a typed stop ACK. The
        # Control Plane projects that ACK through the non-emitting stop-receipt
        # path instead of accepting an unbound execution-status callback.
        if self._execution_has_verified_callback_binding(execution):
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
            "execution_key": execution.execution_key,
            "owner": owner.model_dump(mode="json"),
            # CANCEL is a stop-disposition protocol ACK. The actual execution
            # outcome was uploaded immediately above and remains unchanged.
            "status": ExecutionStatus.CANCELLED.value,
            "physical_stop_confirmed": True,
        }
        result = self._bind_execution_stop_result_resource(result, effect_binding)
        if cancellation_journal_error is not None:
            result["cancellation_tombstone_persisted"] = False
        elif journal_identity is not None:
            resource_outcome: dict[str, object] = {
                "state": "physical_stop_confirmed",
                "result": result,
            }
            await self._execution_cancellations.transition_resource(
                journal_key,
                journal_identity,
                expected_outcome={"state": "cancellation_requested"},
                outcome=resource_outcome,
                resource_outcome=resource_outcome,
            )
        return result

    async def _execution_cancellation_exists(
        self,
        execution_id: str,
        legacy_execution_key: str,
    ) -> bool:
        typed_key = _execution_cancellation_key(execution_id)
        if await self._execution_cancellations.get_resource(typed_key) is not None:
            return True
        return (
            await self._execution_cancellations.get_legacy_resource(legacy_execution_key)
            is not None
        )

    def _execution_control_lock(self, execution_key: str) -> asyncio.Lock:
        return self._execution_control_locks.setdefault(execution_key, asyncio.Lock())

    def _require_command_owner(self, command: LeasedRunnerCommand) -> RunnerPrincipal:
        owner = self._client_principal()
        if command.target != owner:
            raise RuntimeError(
                f"Runner command {command.id!r} targets a different Runner principal"
            )
        return owner

    def _operation_journal_identity(
        self,
        command: LeasedRunnerCommand,
    ) -> OperationJournalIdentity:
        binding = self._require_command_effect_binding(command)
        ownership = command.ownership
        assert ownership is not None
        return OperationJournalIdentity(
            command_id=command.id,
            binding_digest=binding.binding_digest,
            envelope_digest=ownership.envelope_digest,
        )

    def _require_command_effect_binding(
        self,
        command: LeasedRunnerCommand,
    ) -> RunnerEffectBinding:
        ownership = command.ownership
        if ownership is None:
            raise RuntimeError(
                f"Runner command {command.id!r} omitted its verified ownership envelope"
            )
        binding = ownership.effect_binding
        if (
            command.ownership_schema_version != RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION
            or ownership.command_id != command.id
            or ownership.operation is not command.kind
            or ownership.operation_family is not binding.operation_family
            or command.operation_family is not ownership.operation_family
            or binding.node_id != self.config.node_id
            or binding.target != command.target
            or command.effect_binding_id != binding.id
            or command.binding_digest != binding.binding_digest
            or command.envelope_digest != ownership.envelope_digest
            or command.output_contract != ownership.output_contract
            or ownership.payload_digest != runner_payload_digest(command.payload)
        ):
            raise RuntimeError(f"Runner command {command.id!r} has an inconsistent effect binding")
        protocol = runner_command_protocol(command.kind)
        if ownership.operation_family is not protocol.operation_family:
            raise RuntimeError(f"Runner command {command.id!r} uses an invalid operation family")
        if binding.resource_kind is not protocol.resource_kind:
            raise RuntimeError(f"Runner command {command.id!r} uses an invalid resource kind")
        if (
            binding.run_kind is not RunKind.GENERAL
            or binding.audit_id is not None
            or binding.plan_digest is not None
        ):
            raise RuntimeError(
                f"Runner command {command.id!r} requests a Code Audit host effect "
                "before Audit Runner admission is enabled"
            )
        allowed_origins = (
            frozenset(
                {
                    RunnerCommandOrigin.APPLICATION_SERVICE,
                    RunnerCommandOrigin.SAFETY_RECONCILER,
                }
            )
            if ownership.operation_family is RunnerOperationFamily.SAFETY_STOP
            else frozenset({RunnerCommandOrigin.APPLICATION_SERVICE})
        )
        if binding.origin not in allowed_origins:
            raise RuntimeError(f"Runner command {command.id!r} uses an invalid effect origin")
        contract = ownership.output_contract
        if ownership.operation_family is RunnerOperationFamily.SAFETY_STOP:
            if contract.stop_ack_schema != protocol.stop_ack_schema:
                raise RuntimeError(
                    f"Runner command {command.id!r} has an invalid stop ACK contract"
                )
        elif contract.stop_ack_schema is not None:
            raise RuntimeError(
                f"Runner command {command.id!r} attaches a stop ACK to a non-safety effect"
            )
        if contract.result_schema != protocol.result_schema:
            raise RuntimeError(f"Runner command {command.id!r} has an invalid result schema")
        if protocol.output_mode == "command":
            valid_output_contract = contract.max_output_bytes > 0 and contract.allowed_streams == (
                "command",
            )
        elif protocol.output_mode == "execution":
            valid_output_contract = contract.max_output_bytes > 0 and contract.allowed_streams == (
                "stderr",
                "stdout",
            )
        else:
            valid_output_contract = (
                contract.max_output_bytes == 0 and contract.allowed_streams == ()
            )
        if not valid_output_contract:
            raise RuntimeError(f"Runner command {command.id!r} has an invalid output contract")
        invalid_fields = runner_command_payload_binding_invalid_fields(
            command.kind,
            binding,
            command.payload,
        )
        if invalid_fields:
            raise RuntimeError(
                f"Runner command {command.id!r} payload conflicts with its effect binding: "
                + ", ".join(invalid_fields)
            )
        return binding

    def _require_execution_launch_binding(
        self,
        command: LeasedRunnerCommand,
        *,
        execution_id: str,
        expected_family: RunnerOperationFamily,
        expected_resource_kind: RunnerResourceKind,
        expected_resource_id: str,
    ) -> dict[str, str]:
        ownership = command.ownership
        if ownership is None:
            raise RuntimeError(
                f"Runner command {command.id!r} omitted its verified ownership envelope"
            )
        binding = ownership.effect_binding
        if (
            ownership.operation is not command.kind
            or ownership.operation_family is not expected_family
            or binding.operation_family is not expected_family
            or binding.execution_id != execution_id
            or binding.resource_kind is not expected_resource_kind
            or binding.resource_id != expected_resource_id
            or binding.node_id != self.config.node_id
            or binding.target != command.target
            or binding.id != command.effect_binding_id
            or binding.binding_digest != command.binding_digest
            or ownership.envelope_digest != command.envelope_digest
        ):
            raise RuntimeError(f"Runner command {command.id!r} has an inconsistent launch binding")
        if (
            not {"stdout", "stderr"}.issubset(ownership.output_contract.allowed_streams)
            or ownership.output_contract.max_output_bytes <= 0
        ):
            raise RuntimeError(
                f"Runner command {command.id!r} omitted its execution output contract"
            )
        return {
            "runner_command_id": command.id,
            "runner_effect_binding_id": binding.id,
            "runner_binding_digest": binding.binding_digest,
            "runner_envelope_digest": ownership.envelope_digest,
        }

    @staticmethod
    def _require_request_callback_binding_compatible(
        request: ExecutionLaunchRequest | TerminalLaunchRequest,
        expected: dict[str, str],
    ) -> None:
        mismatched = [
            field_name
            for field_name, expected_value in expected.items()
            if (provided := getattr(request, field_name)) is not None and provided != expected_value
        ]
        if mismatched:
            raise RuntimeError(
                "Runner launch request conflicts with its command binding: "
                + ", ".join(sorted(mismatched))
            )

    @staticmethod
    def _require_local_execution_callback_binding(
        execution: Execution,
        expected: dict[str, str],
    ) -> None:
        mismatched = [
            field_name
            for field_name, expected_value in expected.items()
            if getattr(execution, field_name) != expected_value
        ]
        if mismatched:
            raise RuntimeError(
                f"Runner execution {execution.id!r} conflicts with its launch binding: "
                + ", ".join(sorted(mismatched))
            )

    @staticmethod
    def _bind_execution_stop_result_resource(
        result: dict[str, object],
        binding: RunnerEffectBinding | None,
    ) -> dict[str, object]:
        bound = dict(result)
        if binding is None or binding.resource_kind is RunnerResourceKind.EXECUTION:
            return bound
        if binding.resource_kind is not RunnerResourceKind.TERMINAL_SESSION:
            raise RuntimeError("Execution stop result uses an unsupported resource binding")
        existing = bound.get("session_id")
        if existing is not None and existing != binding.resource_id:
            raise RuntimeError("Execution stop result conflicts with its Terminal binding")
        bound["session_id"] = binding.resource_id
        return bound

    @staticmethod
    def _execution_has_verified_callback_binding(execution: Execution) -> bool:
        values = (
            execution.runner_command_id,
            execution.runner_effect_binding_id,
            execution.runner_binding_digest,
            execution.runner_envelope_digest,
        )
        if all(value is None for value in values):
            return False
        if any(value is None for value in values):
            raise RuntimeError(
                f"Runner execution {execution.id!r} has a partial launch callback binding"
            )
        return True

    @staticmethod
    def _execution_callback_kwargs(execution: Execution) -> _ExecutionCallbackKwargs:
        command_id = execution.runner_command_id
        effect_binding_id = execution.runner_effect_binding_id
        binding_digest = execution.runner_binding_digest
        envelope_digest = execution.runner_envelope_digest
        if not RunnerDaemon._execution_has_verified_callback_binding(execution):
            raise RuntimeError(
                f"Runner execution {execution.id!r} has no verified launch callback binding"
            )
        assert command_id is not None
        assert effect_binding_id is not None
        assert binding_digest is not None
        assert envelope_digest is not None
        return _ExecutionCallbackKwargs(
            runner_command_id=command_id,
            runner_effect_binding_id=effect_binding_id,
            runner_binding_digest=binding_digest,
            runner_envelope_digest=envelope_digest,
        )

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
                execution.owner.model_dump(mode="json") if execution.owner is not None else None
            )
            raise RuntimeError(
                f"Runner execution owner mismatch for {execution_id!r}: "
                f"expected {owner.model_dump(mode='json')!r}, found {actual!r}"
            )

    @staticmethod
    def _require_local_execution_effect_binding(
        execution: Execution,
        binding: RunnerEffectBinding,
        *,
        execution_id: str,
        execution_key: str,
    ) -> None:
        RunnerDaemon._require_local_execution_owner(
            execution,
            binding.target,
            execution_id=execution_id,
            execution_key=execution_key,
        )
        invalid_fields: list[str] = []
        if execution.run_id != binding.run_id:
            invalid_fields.append("run_id")
        if execution.node_id != binding.node_id:
            invalid_fields.append("node_id")
        if execution.audit_id != binding.audit_id:
            invalid_fields.append("audit_id")
        if execution.plan_digest != binding.plan_digest:
            invalid_fields.append("plan_digest")
        if binding.execution_id != execution.id:
            invalid_fields.append("binding.execution_id")
        if binding.resource_kind is RunnerResourceKind.EXECUTION:
            if binding.resource_id != execution.id:
                invalid_fields.append("binding.resource_id")
        elif binding.resource_kind is RunnerResourceKind.TERMINAL_SESSION:
            if execution.executor_type is not ExecutorType.PTY:
                invalid_fields.append("executor_type")
            if execution.session_id is not None:
                if execution.session_id != binding.resource_id:
                    invalid_fields.append("session_id")
            elif execution.execution_key != f"terminal:{binding.resource_id}":
                invalid_fields.append("execution_key")
        else:
            invalid_fields.append("binding.resource_kind")
        if invalid_fields:
            raise RuntimeError(
                f"Runner execution {execution.id!r} conflicts with its effect binding: "
                + ", ".join(sorted(set(invalid_fields)))
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
        capped_streams: set[str] = set()
        while not self._closed:
            try:
                cursors = await self._forward_output(
                    server_execution_id,
                    local_execution_id,
                    cursors,
                    capped_streams=capped_streams,
                )
            except RunnerControlClientError:
                logger.warning(
                    "Output forwarding failed for execution %s; retrying",
                    server_execution_id,
                )
            execution = await self._refresh_local_execution(local_execution_id)
            if execution.status in _TERMINAL_STATUSES:
                try:
                    cursors = await self._forward_output(
                        server_execution_id,
                        local_execution_id,
                        cursors,
                        capped_streams=capped_streams,
                    )
                except RunnerControlClientError:
                    logger.warning(
                        "Final output forwarding failed for execution %s; "
                        "reporting terminal status",
                        server_execution_id,
                    )
                await self._report_with_retry(server_execution_id, execution)
                return
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
            if await self._execution_cancellation_exists(
                execution.id,
                execution.execution_key,
            ):
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
        *,
        capped_streams: set[str] | None = None,
    ) -> dict[str, int]:
        capped_streams = capped_streams if capped_streams is not None else set()
        if capped_streams == {"stdout", "stderr"}:
            return cursors
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
            if stream in capped_streams or not item.data:
                continue
            cursors[stream] = await self._forward_output_slice(
                server_execution_id,
                local,
                stream=stream,
                cursor=item.cursor,
                data=item.data,
                capped_streams=capped_streams,
            )
        return cursors

    async def _forward_output_slice(
        self,
        server_execution_id: str,
        local: Execution,
        *,
        stream: str,
        cursor: int,
        data: bytes,
        capped_streams: set[str],
    ) -> int:
        """Forward one slice, preserving its authorized prefix at a hard cap."""

        send_offset = cursor
        output_limit: int | None = None
        slice_end = cursor + len(data)
        for _ in range(4):
            if output_limit is not None:
                if send_offset >= output_limit:
                    capped_streams.add(stream)
                    logger.warning(
                        "Execution %s %s output reached immutable cap %s; "
                        "remaining bytes are truncated",
                        server_execution_id,
                        stream,
                        output_limit,
                    )
                    return output_limit
                chunk_end = min(slice_end, output_limit)
            else:
                chunk_end = slice_end
            chunk = data[send_offset - cursor : chunk_end - cursor]
            if not chunk:
                return send_offset
            try:
                next_offset = await self._client.report_output(
                    server_execution_id,
                    **self._execution_callback_kwargs(local),
                    stream=stream,
                    offset=send_offset,
                    data=chunk,
                )
            except OutputOffsetMismatch as exc:
                expected_offset = exc.expected_offset
                if output_limit is None:
                    return expected_offset
                if expected_offset < cursor or expected_offset > output_limit:
                    raise RuntimeError(
                        "Control Plane output cursor conflicts with its immutable cap"
                    ) from exc
                send_offset = expected_offset
                continue
            except OutputLimitExceeded as exc:
                output_limit = exc.max_output_bytes
                if output_limit <= cursor:
                    capped_streams.add(stream)
                    return output_limit
                if output_limit >= slice_end:
                    raise RuntimeError(
                        "Control Plane returned an inconsistent immutable output cap"
                    ) from exc
                continue
            if next_offset < send_offset or next_offset > chunk_end:
                raise RuntimeError("Control Plane returned an invalid output cursor")
            send_offset = next_offset
            if output_limit is None:
                return send_offset
        raise RuntimeError("Control Plane output reconciliation did not converge")

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
            **self._execution_callback_kwargs(execution),
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


def _require_command_result(value: object, *, operation: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeError(f"Runner {operation} handler returned an invalid result")
    return cast(dict[str, object], value)


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
    if isinstance(value, LeasedRunnerCommand):
        return value.lease_duration_seconds or _lease_remaining_seconds(
            value.lease_expires_at,
            fallback=fallback,
        )
    raise RuntimeError("Control Plane returned an invalid command lease")


def _target_http_cancellation_key(run_id: str, tool_call_id: str) -> str:
    return f"target-http:{run_id}:{tool_call_id}"


def _execution_cancellation_key(execution_id: str) -> str:
    return f"execution:{execution_id}"


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
    config, audit_preflight_runner = await _configure_audit_preflight(
        config,
        client,
    )
    await _run_configured_runner_daemon(
        config,
        client=client,
        executions=executions,
        terminals=terminals,
        runner_paths=runner_paths,
        process_executor=process_executor,
        supervisor=supervisor,
        audit_preflight_runner=audit_preflight_runner,
    )


async def _configure_audit_preflight(
    config: RunnerDaemonConfig,
    _client: RunnerControlClient,
) -> tuple[RunnerDaemonConfig, AuditPreflightRunner | None]:
    # The v3 local-static product does not use the historical Docker SourceIngest
    # capsule. Keep the old wire/domain types readable for compatibility, but never
    # probe Docker or advertise its Runner capability from the supported daemon.
    return replace(config, audit_preflight_ready=False), None


async def _run_configured_runner_daemon(
    config: RunnerDaemonConfig,
    *,
    client: RunnerControlClient,
    executions: FileExecutionRepository,
    terminals: FileTerminalRepository,
    runner_paths: RunnerPaths,
    process_executor: DirectProcessExecutor,
    supervisor: ProcessSupervisor,
    audit_preflight_runner: AuditPreflightRunner | None,
) -> None:
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
        audit_preflight_runner=audit_preflight_runner,
    )
    try:
        await daemon.run_forever()
    finally:
        await daemon.close()


@app.command()
def serve(
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar="RIFTX_CONFIG",
            help="Shared RiftX config used to enable Code Audit on this Runner.",
        ),
    ] = None,
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

    audit = AuditConfig()
    if config_path is not None:
        try:
            audit = load_riftx_config(explicit_path=config_path).audit
        except RiftXConfigError as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from exc
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        run_runner_daemon(
            RunnerDaemonConfig(
                server_url=server_url,
                node_id=node_id,
                name=name,
                state_path=state_path,
                credential_path=credential_path,
                registration_token=os.environ.get("RIFTX_RUNNER_REGISTRATION_TOKEN"),
                require_containment=require_containment,
                payload_uid=payload_uid,
                payload_gid=payload_gid,
                audit=audit,
            )
        )
    )


if __name__ == "__main__":
    app()
