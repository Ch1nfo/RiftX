"""End-to-end tests for the shared FastAPI control plane."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import socket
import stat
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from agents import Model, ModelProvider, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from sqlalchemy import delete, select, update
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from riftx.agent import (
    AgentCycle,
    AgentCycleOutput,
    AgentRuntimeServices,
    SQLAlchemyCheckpointStore,
)
from riftx.api import APISettings, ControlPlane, build_control_plane, create_app
from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    ServiceUnavailableError,
)
from riftx.application.services import (
    ActionApplicationService,
    ApprovalApplicationService,
    ApprovalRequestRecorder,
    ArtifactApplicationService,
    AuditApplicationService,
    AuditPreflightApplicationService,
    ClosureVerifierApplicationService,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    ModelProfileApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    ResourceStopDisposition,
    RunApplicationService,
    RunnerControlService,
    RunSafetyStopService,
    TerminalApplicationService,
    ToolApplicationService,
    WorkflowSignalDispatcher,
    WorkflowSignalObservation,
    WorkflowSignalObservationState,
    WorkflowSignalReconciler,
)
from riftx.application.traffic import TrafficExchangeDetail, TrafficExchangePage
from riftx.application.workflow_router import RunWorkflowControlRouter
from riftx.browser.service import BrowserApplicationService
from riftx.context import ContextApplicationService
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    RUNNER_STOP_ACK_TERMINAL_SCHEMA,
    Approval,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Finding,
    FindingSeverity,
    Objective,
    Run,
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    RunStatus,
    TerminalSession,
    TerminalStatus,
    ToolCall,
    TrustProfile,
    runner_stop_ack_digest,
)
from riftx.domain.workflow_signal import WorkflowSignalIntent, WorkflowSignalKind
from riftx.executors import LinuxCgroupV2Manager
from riftx.memory import MemoryService, MemoryWriter
from riftx.models import ModelProfile, ModelProfileRegistry, ModelsConfig
from riftx.observability import RuntimeMetricName, RuntimeObservabilityService
from riftx.persistence import (
    Database,
    SQLAlchemyActionReadRepository,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditAggregateReadRepository,
    SQLAlchemyAuditCreationUnitOfWork,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyEvidenceLedgerRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyGraphReadRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyReasoningGraphRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyRuntimeApprovalRepository,
    SQLAlchemyTaskGraphRepository,
    SQLAlchemyTerminalRepository,
    SQLAlchemyToolCallIntentRepository,
    SQLAlchemyWorkflowSignalIntentRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.observability_repository import (
    SQLAlchemyRuntimeObservabilityRepository,
)
from riftx.persistence.orm import (
    ArtifactRecord,
    RunnerCommandOwnershipRecord,
    RunnerCommandRecord,
    RunnerStopProjectionRecord,
    TargetHttpRequestRecord,
)
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
    SQLAlchemyTrafficMetadataReadRepository,
)
from riftx.persistence.workflow_signals import WorkflowSignalIntentRecord
from riftx.runner import (
    ExecutionLaunchRequest,
    ProcessSupervisor,
    RunnerBrowserManager,
    RunnerPaths,
    RunnerTargetHttpClient,
    TerminalSupervisor,
)
from riftx.runner.control_client import RunnerControlClient, RunnerCredentialStore
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.runner.state import FileExecutionRepository, FileTerminalRepository
from riftx.runner.terminal_manager import OperationJournal
from riftx.runtime import AgentCycle as RuntimeAgentCycle
from riftx.runtime import (
    AgentSession,
    AgentStep,
    AgentStepType,
    RuntimeApprovalRequest,
    ToolCallIntent,
)
from riftx.security import DeploymentProfileError, LocalObjectAuthorizer
from riftx.skills import create_default_skill_registry
from riftx.target_http.models import (
    TargetHttpRequest,
    TargetHttpResult,
    TargetHttpSubmission,
)
from riftx.target_http.service import TargetHttpApplicationService
from riftx.temporal import RiftXRunWorkflow, WorkflowPhase
from riftx.temporal.activities import RiftXActivities
from riftx.temporal.runtime import TemporalRunClient, TemporalRuntimeConfig
from riftx.temporal.workflow_signal_transport import RoutedWorkflowSignalTransport
from riftx.tools import ToolRegistry

FAKE_TOOL_FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"
PROCESS_TREE_FIXTURE = Path(__file__).parents[2] / "runner" / "fixtures" / "fake_process.py"
RUNNER_BOOTSTRAP_TOKEN = "test-only-runner-bootstrap-token-0003"


def _runner_execution_callback_fields(command: dict[str, object]) -> dict[str, object]:
    effect_binding = command["effect_binding"]
    assert isinstance(effect_binding, dict)
    return {
        "command_id": command["id"],
        "effect_binding_id": effect_binding["id"],
        "envelope_digest": command["envelope_digest"],
        "binding_digest": effect_binding["binding_digest"],
    }


def _runner_command_callback_fields(command: dict[str, object]) -> dict[str, object]:
    effect_binding = command["effect_binding"]
    assert isinstance(effect_binding, dict)
    return {
        "state_version": command["state_version"],
        "envelope_digest": command["envelope_digest"],
        "binding_digest": effect_binding["binding_digest"],
    }


async def _lease_and_finish_runner_command(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    command_id: str,
) -> dict[str, object]:
    leased = await client.get("/api/v1/runner/commands/next", headers=headers)
    assert leased.status_code == 200, leased.text
    command = leased.json()["command"]
    assert command is not None
    assert command["id"] == command_id
    payload = command["payload"]
    if command["kind"] == RunnerCommandKind.EXECUTE.value:
        request = payload["request"]
        result: dict[str, object] = {
            "execution_id": payload["execution_id"],
            "local_execution_id": payload["execution_id"],
            "execution_key": request["execution_key"],
            "owner": command["target"],
            "status": "running",
        }
    elif command["kind"] == RunnerCommandKind.TERMINAL_START.value:
        result = {
            "result": {
                "session_id": payload["session_id"],
                "execution_id": payload["execution_id"],
                "status": "open",
                "duplicate": False,
            }
        }
    else:  # pragma: no cover - helper is intentionally launch-only
        raise AssertionError(f"unsupported launch helper command kind: {command['kind']!r}")
    finished = await client.post(
        f"/api/v1/runner/commands/{command_id}/finish-owned",
        headers=headers,
        json={
            "lease_id": command["lease_id"],
            **_runner_command_callback_fields(command),
            "succeeded": True,
            "result": result,
        },
    )
    assert finished.status_code == 200, finished.text
    return command


async def _bind_test_terminal_launch(
    runtime: RuntimeFixture,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    run_id: str,
    node_id: str,
    terminal_id: str,
    execution_id: str,
) -> dict[str, object]:
    execution = await runtime.execution_repository.get(execution_id)
    assert execution is not None
    principal = {
        "instance_id": headers["X-RiftX-Runner-Instance-ID"],
        "epoch": int(headers["X-RiftX-Runner-Epoch"]),
    }
    command, created = await runtime.control_plane.runner_control_service.enqueue(
        node_id,
        kind=RunnerCommandKind.TERMINAL_START,
        idempotency_key=f"terminal-start:{terminal_id}",
        run_id=run_id,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=RunnerOperationFamily.TERMINAL,
        resource_kind=RunnerResourceKind.TERMINAL_SESSION,
        resource_id=terminal_id,
        execution_id=execution_id,
        output_contract=RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("stderr", "stdout"),
            result_schema="riftx.runner-result/terminal-start/v1",
        ),
        payload={
            "session_id": terminal_id,
            "execution_id": execution_id,
            "request": {
                "run_id": run_id,
                "node_id": node_id,
                "session_id": terminal_id,
                "execution_id": execution_id,
                "execution_key": execution.execution_key,
                "runner_principal": principal,
            },
        },
    )
    assert created is True
    return await _lease_and_finish_runner_command(client, headers, command.id)


class FullRunModel(Model):
    """Deterministic model that drives the complete host-native E2E lifecycle."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        outputs = [
            _e2e_tool_call(
                "run_registered_tool",
                {
                    "tool_id": "fake-success",
                    "args": ["e2e"],
                    "timeout_seconds": None,
                    "reason": "Run the deterministic authorized verification fixture.",
                },
                call_id="tool-call-e2e",
            ),
            _e2e_tool_call(
                "create_finding",
                {
                    "title": "Deterministic test service exposure",
                    "severity": "high",
                    "affected_assets": ["127.0.0.1"],
                    "description": "The authorized fixture returned a successful service response.",
                    "evidence": [
                        {
                            "artifact_id": None,
                            "execution_id": None,
                            "description": "Observed fake-success output in the host execution.",
                            "location": "agent.tool_completed",
                        }
                    ],
                    "reproduction_steps": ["Run fake-success with argument e2e"],
                    "impact": "Confirms the end-to-end finding pipeline.",
                    "recommendation": "Retain this fixture only for automated tests.",
                },
                call_id="finding-call-e2e",
            ),
            _e2e_tool_call(
                "complete_run",
                {"run_summary": "Verification executed and one supported finding was recorded."},
                call_id="complete-call-e2e",
            ),
            _e2e_message(
                AgentCycleOutput(
                    assistant_message="The deterministic verification run completed.",
                    plan_summary="Execute the fixture, record the finding, and generate reports.",
                    run_summary="Verification executed and one supported finding was recorded.",
                )
            ),
        ]
        output = outputs[self.calls]
        self.calls += 1
        return ModelResponse(
            output=output,
            usage=Usage(),
            response_id=f"e2e-response-{self.calls}",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def generate() -> AsyncIterator[Any]:
            if False:
                yield None

        return generate()


class FixedModelProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self._model = model

    def get_model(self, model_name: str | None) -> Model:
        del model_name
        return self._model


def _e2e_tool_call(
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> list[Any]:
    return [
        ResponseFunctionToolCall(
            arguments=json.dumps(arguments),
            call_id=call_id,
            name=name,
            type="function_call",
            status="completed",
        )
    ]


def _e2e_message(output: AgentCycleOutput) -> list[Any]:
    return [
        ResponseOutputMessage(
            id="e2e-message",
            role="assistant",
            status="completed",
            type="message",
            content=[
                ResponseOutputText(
                    annotations=[],
                    text=output.model_dump_json(),
                    type="output_text",
                )
            ],
        )
    ]


@dataclass
class FakeWorkflowClient:
    fail: bool = False
    error: Exception | None = None
    workflow_id_prefix: str = "test-workflow"
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    workflow_ids: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def start_run(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> object:
        self._record("start", run_id, workflow_id=workflow_id)
        return object()

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self._record("pause", run_id, workflow_id=workflow_id)

    async def resume(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self._record("resume", run_id, workflow_id=workflow_id)

    async def approve(
        self,
        run_id: str,
        call_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record("approve", run_id, call_id, workflow_id=workflow_id)

    async def reject(
        self,
        run_id: str,
        call_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record("reject", run_id, call_id, workflow_id=workflow_id)

    async def execution_completed(
        self,
        run_id: str,
        execution_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record(
            "execution_completed",
            run_id,
            execution_id,
            workflow_id=workflow_id,
        )

    async def cancel_current_execution(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record("cancel_current_execution", run_id, workflow_id=workflow_id)

    async def cancel(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self._record("cancel", run_id, workflow_id=workflow_id)

    async def compact(
        self,
        run_id: str,
        max_history_items: int = 100,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record(
            "compact",
            run_id,
            str(max_history_items),
            workflow_id=workflow_id,
        )

    async def switch_model(
        self,
        run_id: str,
        model_profile: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record(
            "switch_model",
            run_id,
            model_profile,
            workflow_id=workflow_id,
        )

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self._record("message", run_id, message, workflow_id=workflow_id)

    def workflow_id(self, run_id: str) -> str:
        return f"{self.workflow_id_prefix}-{run_id}"

    def _record(
        self,
        action: str,
        run_id: str,
        detail: str | None = None,
        *,
        workflow_id: str | None = None,
    ) -> None:
        if self.error is not None:
            raise self.error
        if self.fail:
            raise RuntimeError("Temporal test outage")
        self.calls.append((action, run_id, detail))
        self.workflow_ids.append((action, run_id, workflow_id))


class FakeWorkflowSignalOutcomeProbe:
    """The Fake client fails before recording a send, so its outage is definitive."""

    async def observe(
        self,
        intent: WorkflowSignalIntent,
    ) -> WorkflowSignalObservation:
        return WorkflowSignalObservation(
            state=WorkflowSignalObservationState.NOT_DELIVERED,
            owner_kind=intent.owner_kind,
            workflow_protocol_version=intent.workflow_protocol_version,
            workflow_id=intent.workflow_id,
            signal_kind=intent.signal_kind,
            identity_digest=intent.identity_digest,
            payload_digest=intent.payload_digest,
            observation_receipt=f"test-not-delivered:{intent.identity_digest}",
        )


class RecordingSignalWithStartClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return object()


@dataclass
class BlockingResourceStopper:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def stop_run(self, run_id: str) -> ResourceStopDisposition:
        del run_id
        self.entered.set()
        await self.release.wait()
        return ResourceStopDisposition((), {}, {}, {}, {})


@dataclass
class RuntimeFixture:
    control_plane: ControlPlane
    workflow: FakeWorkflowClient | TemporalRunClient
    finding_repository: SQLAlchemyFindingRepository
    approval_repository: SQLAlchemyApprovalRepository
    runtime_approval_repository: SQLAlchemyRuntimeApprovalRepository
    artifact_repository: SQLAlchemyArtifactRepository
    report_repository: SQLAlchemyReportRepository
    run_repository: SQLAlchemyRunRepository
    execution_repository: SQLAlchemyExecutionRepository
    event_repository: SQLAlchemyRunEventRepository
    terminal_repository: SQLAlchemyTerminalRepository
    workflow_signal_repository: SQLAlchemyWorkflowSignalIntentRepository


async def _build_runtime(
    tmp_path: Path,
    *,
    database_path: Path | None = None,
    workflow: FakeWorkflowClient | TemporalRunClient | None = None,
    model_profile_override: str | None = None,
    model_environment: dict[str, str] | None = None,
    admin_token: str | None = "test-only-local-operator-token-0001",
    runner_command_lease_seconds: float = 0.05,
) -> RuntimeFixture:
    db_path = database_path or (tmp_path / "riftx.db")
    database = Database(f"sqlite+aiosqlite:///{db_path}")
    await database.create_schema()

    tools_path = tmp_path / "tools.yaml"
    if not tools_path.exists():
        tools_path.write_text(
            """\
version: 1
execution_policy: registered_only
tools:
  python:
    command: [python]
    capabilities: [scripting]
"""
        )
    registry = ToolRegistry(tools_path, node_id="local")
    await registry.refresh()

    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    audit_aggregate_repository = SQLAlchemyAuditAggregateReadRepository(database.session_factory)
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    node_repository = SQLAlchemyNodeRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    report_repository = SQLAlchemyReportRepository(database.session_factory)
    approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
    runtime_approval_repository = SQLAlchemyRuntimeApprovalRepository(database.session_factory)
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    workflow_execution_repository = SQLAlchemyExecutionRepository(
        database.session_factory,
        emit_workflow_signal_intents=True,
    )
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
    agent_session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
    browser_repository = SQLAlchemyBrowserRepository(database.session_factory)
    tool_call_intent_repository = SQLAlchemyToolCallIntentRepository(database.session_factory)
    runner_credential_repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
    memory_repository = SQLAlchemyMemoryRepository(database.session_factory)
    workflow_signal_repository = SQLAlchemyWorkflowSignalIntentRepository(database.session_factory)
    node_service = NodeApplicationService(node_repository)
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.1,
    )
    workflow_client = workflow or FakeWorkflowClient()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "secrets" / "local-principal.json",
        database_url=database.url,
        tools_config_path=tools_path,
        models_config_path=tmp_path / "models.yaml",
        model_secrets_path=tmp_path / "secrets" / "models.json",
        model_profile_override=model_profile_override,
        workspace_root=tmp_path / "workspaces",
        runner_state_path=tmp_path / "runner",
        sse_poll_interval_seconds=0.001,
        sse_heartbeat_seconds=0.005,
        runner_registration_token=RUNNER_BOOTSTRAP_TOKEN,
        runner_command_lease_seconds=runner_command_lease_seconds,
        admin_token=admin_token,
    )
    runner_paths = RunnerPaths(settings.runner_state_path)
    model_registry = ModelProfileRegistry(
        settings.models_config_path,
        settings.model_secrets_path,
        initial_config=ModelsConfig(
            default_profile="primary",
            models={
                name: ModelProfile(
                    model=f"{name}-model",
                    base_url="http://127.0.0.1:8000/v1",
                    requires_api_key=False,
                    api_key_env=None,
                )
                for name in ("primary", "fast", "deep")
            },
        ),
    )
    model_profile_service = ModelProfileApplicationService(
        model_registry,
        run_repository=run_repository,
        session_repository=agent_session_repository,
        profile_override=model_profile_override,
        environment=model_environment or {},
    )
    process_supervisor = ProcessSupervisor(execution_repository, runner_paths)
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=runner_paths,
    )
    browser_manager = RunnerBrowserManager(
        node_id=settings.node_id,
        paths=runner_paths,
    )
    browser_service = BrowserApplicationService(
        runs=run_repository,
        agent_sessions=agent_session_repository,
        repository=browser_repository,
        runner=browser_manager,
        artifacts=artifact_service,
        events=event_repository,
    )
    target_http_service = TargetHttpApplicationService(
        runs=run_repository,
        tool_calls=tool_call_intent_repository,
        requests=SQLAlchemyTargetHttpRequestRepository(database.session_factory),
        runner=RunnerTargetHttpClient(node_id=settings.node_id),
        artifacts=artifact_service,
        events=event_repository,
    )
    runner_control_service = RunnerControlService(
        credentials=runner_credential_repository,
        commands=runner_command_repository,
        nodes=node_service,
        executions=workflow_execution_repository,
        stop_projection_executions=execution_repository,
        runs=run_repository,
        paths=runner_paths,
        registration_token=settings.runner_registration_token,
        terminals=terminal_repository,
        browser_sessions=browser_repository,
        tool_call_intents=tool_call_intent_repository,
        events=event_repository,
        lease_duration=timedelta(seconds=settings.runner_command_lease_seconds),
    )
    remote_terminal_supervisor = RemoteTerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        control=runner_control_service,
        paths=runner_paths,
    )
    terminal_controller = NodeTerminalRouter(
        local_node_id=settings.node_id,
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        local=terminal_supervisor,
        remote=remote_terminal_supervisor,
    )
    workflow_router = RunWorkflowControlRouter(
        runs=run_repository,
        audits=audit_aggregate_repository,
        general=workflow_client,
    )
    workflow_signal_transport = RoutedWorkflowSignalTransport(
        workflow_router,
        runs=run_repository,
        sources=workflow_signal_repository,
    )
    workflow_signal_dispatcher = WorkflowSignalDispatcher(
        repository=workflow_signal_repository,
        transport=workflow_signal_transport,
        lease_owner=f"control-plane-test-dispatch:{uuid4()}",
        backoff=lambda attempt: timedelta(0),
    )
    workflow_signal_reconciler = WorkflowSignalReconciler(
        repository=workflow_signal_repository,
        probe=FakeWorkflowSignalOutcomeProbe(),
        lease_owner=f"control-plane-test-probe:{uuid4()}",
        backoff=lambda attempt: timedelta(0),
    )
    return RuntimeFixture(
        control_plane=ControlPlane(
            settings=settings,
            database=database,
            run_service=RunApplicationService(
                engagement_repository=engagement_repository,
                run_repository=run_repository,
                event_repository=event_repository,
                workflow_client=workflow_router,
                execution_repository=execution_repository,
                execution_runner=process_supervisor,
                workspace_root=settings.workspace_root,
                model_profiles=model_profile_service,
                resource_stoppers={
                    "browser_sessions": browser_service,
                    "target_http_requests": target_http_service,
                },
                execution_cancel_timeout_seconds=0.2,
                execution_cancel_poll_seconds=0.01,
            ),
            audit_service=AuditApplicationService(
                creation_uow=SQLAlchemyAuditCreationUnitOfWork(database.session_factory),
                aggregate_repository=audit_aggregate_repository,
                feature_enabled=settings.audit.enabled,
                workspace_root=settings.audit.temp_root,
                legacy_draft_api_enabled=True,
            ),
            action_service=ActionApplicationService(
                SQLAlchemyActionReadRepository(database.session_factory),
                authorizer=LocalObjectAuthorizer(settings.create_local_operator_security()),
            ),
            event_service=EventApplicationService(
                run_repository=run_repository,
                event_repository=event_repository,
                artifact_associations=artifact_repository,
            ),
            execution_service=ExecutionApplicationService(
                run_repository=run_repository,
                execution_repository=execution_repository,
                event_repository=event_repository,
                runner=process_supervisor,
            ),
            node_service=node_service,
            runner_control_service=runner_control_service,
            finding_service=FindingApplicationService(
                run_repository=run_repository,
                finding_repository=finding_repository,
                artifact_repository=artifact_repository,
                execution_repository=execution_repository,
                event_repository=event_repository,
                memory_writer=MemoryWriter(memory_repository),
            ),
            report_service=ReportApplicationService(
                run_repository=run_repository,
                finding_repository=finding_repository,
                artifact_repository=artifact_repository,
                report_repository=report_repository,
                event_repository=event_repository,
                artifact_service=artifact_service,
            ),
            tool_service=ToolApplicationService(registry),
            model_profile_service=model_profile_service,
            approval_service=ApprovalApplicationService(
                approval_repository=approval_repository,
                run_repository=run_repository,
                event_repository=event_repository,
                runtime_approval_repository=runtime_approval_repository,
            ),
            artifact_service=artifact_service,
            context_service=ContextApplicationService(context_repository),
            memory_service=MemoryService(
                memory_repository,
                run_repository=run_repository,
            ),
            runtime_observability_service=RuntimeObservabilityService(
                SQLAlchemyRuntimeObservabilityRepository(database.session_factory)
            ),
            terminal_service=TerminalApplicationService(
                run_repository=run_repository,
                supervisor=terminal_controller,
                artifact_service=artifact_service,
                event_repository=event_repository,
            ),
            terminal_supervisor=terminal_supervisor,
            graph_repository=SQLAlchemyGraphReadRepository(database.session_factory),
            traffic_repository=SQLAlchemyTrafficMetadataReadRepository(
                database.session_factory,
                digest_key=b"test-traffic-digest-key-0000000001",
                artifact_reference_key=b"test-traffic-artifact-key-000000001",
            ),
            workflow_signal_dispatcher=workflow_signal_dispatcher,
            workflow_signal_reconciler=workflow_signal_reconciler,
            browser_service=browser_service,
            browser_manager=browser_manager,
            process_supervisor=process_supervisor,
            target_http_service=target_http_service,
        ),
        workflow=workflow_client,
        finding_repository=finding_repository,
        approval_repository=approval_repository,
        runtime_approval_repository=runtime_approval_repository,
        artifact_repository=artifact_repository,
        report_repository=report_repository,
        run_repository=run_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        terminal_repository=terminal_repository,
        workflow_signal_repository=workflow_signal_repository,
    )


async def _client(control_plane: ControlPlane) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(control_plane=control_plane)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=(
                {"Authorization": f"Bearer {control_plane.settings.admin_token}"}
                if control_plane.settings.admin_token
                else None
            ),
        ) as client:
            yield client


async def _reconcile_workflow_signals_once(runtime: RuntimeFixture) -> None:
    dispatcher = runtime.control_plane.workflow_signal_dispatcher
    reconciler = runtime.control_plane.workflow_signal_reconciler
    assert dispatcher is not None
    assert reconciler is not None
    await dispatcher.dispatch_batch()
    await reconciler.reconcile_batch()


async def _workflow_signal_records(
    runtime: RuntimeFixture,
    run_id: str,
) -> list[WorkflowSignalIntentRecord]:
    async with runtime.control_plane.database.session_factory() as session:
        records = (
            await session.scalars(
                select(WorkflowSignalIntentRecord)
                .where(WorkflowSignalIntentRecord.run_id == run_id)
                .order_by(
                    WorkflowSignalIntentRecord.created_at,
                    WorkflowSignalIntentRecord.id,
                )
            )
        ).all()
    return list(records)


async def _create_run(client: httpx.AsyncClient) -> dict[str, object]:
    response = await client.post(
        "/api/v1/runs",
        json={
            "objective": "Inspect the local service",
            "model_profile": "fast",
            "engagement": {"name": "Local authorized test"},
            "success_criteria": [{"description": "Identify the exposed service", "required": True}],
            "entry_points": [{"kind": "url", "value": "http://127.0.0.1"}],
            "scope": {"ips": ["127.0.0.1"]},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_run_crud_control_and_message_timeline(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            assert created["kind"] == "general"
            assert created["node_id"] == "local"
            assert created["status"] == "waiting_user"
            assert created["model_profile"] == "fast"
            assert created["temporal_workflow_id"] == f"test-workflow-{run_id}"
            assert runtime.workflow.calls == []

            injected_kind = await client.post(
                "/api/v1/runs",
                json={"objective": "Bypass Audit admission", "kind": "code_audit"},
            )
            assert injected_kind.status_code == 422

            audit_run = Run(
                kind=RunKind.CODE_AUDIT,
                id="audit-run-filter-canary",
                engagement_id=str(created["engagement_id"]),
                node_id="local",
                objective=Objective(description="Audit filter canary"),
                workspace_path=str(tmp_path / "audit-run-filter-canary"),
            )
            await runtime.run_repository.create(audit_run)

            initial_events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert initial_events.status_code == 200
            assert [item["event_type"] for item in initial_events.json()["items"]] == [
                "run.created",
                "conversation.context_ready",
            ]
            context = initial_events.json()["items"][1]["payload"]
            assert context == {
                "session_id": f"{run_id}:primary",
                "status": "waiting_user",
                "objective": "Inspect the local service",
                "success_criteria": [
                    {"description": "Identify the exposed service", "required": True}
                ],
                "entry_points": [
                    {
                        "kind": "url",
                        "value": "http://127.0.0.1",
                        "metadata": {},
                    }
                ],
                "scope": {
                    "cidrs": [],
                    "ips": ["127.0.0.1"],
                    "domains": [],
                    "url_prefixes": [],
                    "asset_tags": [],
                    "exclusions": [],
                    "starts_at": None,
                    "ends_at": None,
                },
                "approval_mode": "balanced",
                "model_profile": "fast",
                "agent_started": False,
            }

            listed = await client.get("/api/v1/runs")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [run_id]
            assert listed.json()["items"][0]["kind"] == "general"

            listed_audits = await client.get(
                "/api/v1/runs",
                params={"kind": "code_audit"},
            )
            assert listed_audits.status_code == 200
            # A bare code_audit Run is not an authorized Audit aggregate and
            # must never become visible through the generic Run projection.
            assert listed_audits.json()["items"] == []

            combined_filter = await client.get(
                "/api/v1/runs",
                params={"kind": "code_audit", "status": "waiting_user"},
            )
            assert combined_filter.status_code == 200
            assert combined_filter.json()["items"] == []

            invalid_kind = await client.get(
                "/api/v1/runs",
                params={"kind": "unknown"},
            )
            assert invalid_kind.status_code == 422

            shown = await client.get(f"/api/v1/runs/{run_id}")
            assert shown.status_code == 200
            assert shown.json()["kind"] == "general"
            assert shown.json()["objective"]["description"] == "Inspect the local service"

            metrics = await client.get(f"/api/v1/runs/{run_id}/metrics")
            assert metrics.status_code == 200
            metric_payload = metrics.json()
            assert set(metric_payload["metrics"]) == {metric.value for metric in RuntimeMetricName}
            assert metric_payload["metrics"]["task_completion_rate"] == {
                "name": "task_completion_rate",
                "numerator": 0,
                "denominator": 1,
                "value": 0.0,
                "available": True,
                "direction": "higher_is_better",
                "description": "Completed tasks divided by observed tasks.",
            }

            assert (await client.post(f"/api/v1/runs/{run_id}/pause")).status_code == 202
            assert (await client.post(f"/api/v1/runs/{run_id}/resume")).status_code == 202
            assert (
                await client.post(f"/api/v1/runs/{run_id}/cancel-current-execution")
            ).status_code == 202
            message = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "Focus on the HTTP endpoint"},
            )
            assert message.status_code == 202
            compact = await client.post(
                f"/api/v1/runs/{run_id}/compact",
                json={"max_history_items": 25},
            )
            assert compact.status_code == 202
            assert ("compact", run_id, "25") in runtime.workflow.calls
            switched = await client.post(
                f"/api/v1/runs/{run_id}/model",
                json={"model_profile": "deep"},
            )
            assert switched.status_code == 202
            assert ("switch_model", run_id, "deep") in runtime.workflow.calls
            assert (await client.post(f"/api/v1/runs/{run_id}/cancel")).status_code == 202

            assert [call[0] for call in runtime.workflow.calls] == [
                "message",
                "compact",
                "switch_model",
            ]
            events = await client.get(f"/api/v1/runs/{run_id}/events", params={"limit": 20})
            assert events.status_code == 200
            event_types = [item["event_type"] for item in events.json()["items"]]
            assert event_types == [
                "run.created",
                "conversation.context_ready",
                "run.status_changed",
                "run.pause_requested",
                "run.status_changed",
                "run.status_changed",
                "run.resume_requested",
                "execution.cancel_requested",
                "user.message_queued",
                "workflow.started",
                "agent.context_compaction_requested",
                "agent.model_switch_requested",
                "run.status_changed",
                "run.status_changed",
                "run.cancel_requested",
            ]
            assert [item["payload"] for item in events.json()["items"][-3:-1]] == [
                {"from": "waiting_user", "to": "cancelling"},
                {"from": "cancelling", "to": "cancelled"},
            ]
            message_event = next(
                item
                for item in events.json()["items"]
                if item["event_type"] == "user.message_queued"
            )
            assert message_event["payload"]["message"] == "Focus on the HTTP endpoint"
            assert ("message", run_id, message_event["id"]) in runtime.workflow.calls
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_general_workflow_controls_keep_the_persisted_id_after_prefix_drift(
    tmp_path: Path,
) -> None:
    workflow = FakeWorkflowClient(workflow_id_prefix="historical-workflow")
    runtime = await _build_runtime(tmp_path, workflow=workflow)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            persisted_workflow_id = f"historical-workflow-{run_id}"
            assert created["temporal_workflow_id"] == persisted_workflow_id

            workflow.workflow_id_prefix = "current-workflow"
            assert workflow.workflow_id(run_id) == f"current-workflow-{run_id}"

            message = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "Use the historical Workflow identity"},
            )
            compact = await client.post(
                f"/api/v1/runs/{run_id}/compact",
                json={"max_history_items": 25},
            )
            switched = await client.post(
                f"/api/v1/runs/{run_id}/model",
                json={"model_profile": "deep"},
            )
            tool_call = ToolCall(
                id="tool-call-prefix-drift",
                sdk_call_id="sdk-call-prefix-drift",
                run_id=run_id,
                agent_step_id="step-prefix-drift",
                tool_id="python",
            )
            approval = Approval(
                id="approval-prefix-drift",
                run_id=run_id,
                tool_call_id=tool_call.id,
                tool_name="python",
            )
            await runtime.approval_repository.create_request(tool_call, approval)
            approved = await client.post(
                f"/api/v1/approvals/{approval.id}/approve",
                json={},
            )

            assert [
                message.status_code,
                compact.status_code,
                switched.status_code,
                approved.status_code,
            ] == [
                202,
                202,
                202,
                200,
            ]
            await _reconcile_workflow_signals_once(runtime)
            assert workflow.workflow_ids == [
                ("message", run_id, persisted_workflow_id),
                ("compact", run_id, persisted_workflow_id),
                ("switch_model", run_id, persisted_workflow_id),
                ("approve", run_id, persisted_workflow_id),
            ]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_message_losing_atomic_completion_race_is_explicitly_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)

            event_repository = runtime.control_plane.run_service._event_repository
            append_user_message = event_repository.append_user_message

            async def complete_before_message_append(
                target_run_id: str,
                message: str,
                *,
                event_id: str | None = None,
                _append_user_message: Any = append_user_message,
            ) -> object:
                (
                    fenced,
                    pending,
                ) = await runtime.run_repository.complete_if_no_pending_user_messages(
                    target_run_id,
                    consumed_user_message_ids=[],
                )
                assert fenced.status is RunStatus.COMPLETING
                assert pending == ()
                return await _append_user_message(
                    target_run_id,
                    message,
                    event_id=event_id,
                )

            # Inject the exact ordering hidden by the service's initial status
            # read: the DB completion fence commits before its message insert.
            monkeypatch.setattr(
                event_repository,
                "append_user_message",
                complete_before_message_append,
            )
            response = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "This must not be silently queued"},
            )

            assert response.status_code == 409
            assert response.json()["error"] == {
                "code": "run_not_controllable",
                "message": f"Cannot send a message to run '{run_id}' while it is completing",
                "details": {"run_id": run_id, "status": "completing"},
            }
            fenced_run = await runtime.run_repository.get(run_id)
            assert fenced_run is not None and fenced_run.status is RunStatus.COMPLETING
            timeline = await client.get(f"/api/v1/runs/{run_id}/events")
            assert not any(
                item["event_type"] == "user.message_queued" for item in timeline.json()["items"]
            )
            assert not any(
                item["payload"].get("to") == "completed"
                for item in timeline.json()["items"]
                if item["event_type"] == "run.status_changed"
            )
            assert runtime.workflow.calls == []
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_slow_pause_fence_rejects_ordinary_controls_until_paused(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    blocker = BlockingResourceStopper()
    pause_task: asyncio.Task[httpx.Response] | None = None
    try:
        supervisor = runtime.control_plane.process_supervisor
        target_http = runtime.control_plane.target_http_service
        assert supervisor is not None
        assert target_http is not None
        runtime.control_plane.run_service._safety_stopper = RunSafetyStopService(
            execution_repository=runtime.execution_repository,
            execution_runner=supervisor,
            resource_stoppers={
                "browser_sessions": blocker,
                "target_http_requests": target_http,
            },
            execution_cancel_timeout_seconds=0.2,
            execution_cancel_poll_seconds=0.01,
        )

        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            approvals: list[Approval] = []
            for suffix in ("approve", "reject"):
                tool_call = ToolCall(
                    id=f"tool-call-slow-pause-{suffix}",
                    sdk_call_id=f"sdk-call-slow-pause-{suffix}",
                    run_id=run_id,
                    agent_step_id=f"step-slow-pause-{suffix}",
                    tool_id="python",
                )
                approval = Approval(
                    id=f"approval-slow-pause-{suffix}",
                    run_id=run_id,
                    tool_call_id=tool_call.id,
                    tool_name="python",
                )
                await runtime.approval_repository.create_request(tool_call, approval)
                approvals.append(approval)

            pause_task = asyncio.create_task(client.post(f"/api/v1/runs/{run_id}/pause"))
            await asyncio.wait_for(blocker.entered.wait(), timeout=1)
            fenced = await runtime.run_repository.get(run_id)
            assert fenced is not None and fenced.status is RunStatus.PAUSING

            blocked_responses = [
                await client.post(
                    f"/api/v1/runs/{run_id}/message",
                    json={"message": "Must not cross the pause fence"},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/compact",
                    json={"max_history_items": 25},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/model",
                    json={"model_profile": "deep"},
                ),
                await client.post(
                    f"/api/v1/approvals/{approvals[0].id}/approve",
                    json={},
                ),
                await client.post(
                    f"/api/v1/approvals/{approvals[1].id}/reject",
                    json={"reason": "Not now"},
                ),
            ]

            assert [response.status_code for response in blocked_responses] == [409] * 5
            for response in blocked_responses[:3]:
                assert response.json()["error"]["code"] == "run_not_controllable"
                assert response.json()["error"]["details"]["status"] == "pausing"
            for response in blocked_responses[3:]:
                assert response.json()["error"]["code"] == "approval_not_actionable"
                assert response.json()["error"]["details"]["run_status"] == "pausing"
            for approval in approvals:
                persisted = await runtime.approval_repository.get(approval.id)
                assert persisted is not None and persisted.status.value == "pending"
            assert runtime.workflow.calls == []
            timeline = await client.get(f"/api/v1/runs/{run_id}/events")
            assert not {
                "user.message_queued",
                "agent.context_compaction_requested",
                "agent.model_switch_requested",
                "tool.approved",
                "tool.rejected",
            }.intersection(item["event_type"] for item in timeline.json()["items"])

            blocker.release.set()
            paused = await asyncio.wait_for(pause_task, timeout=1)
            pause_task = None
            assert paused.status_code == 202
            assert paused.json()["run"]["status"] == "paused"

            # PAUSED is the adjacent stable state: ordinary decisions may be
            # queued durably for the next resume, but never while PAUSING.
            accepted = [
                await client.post(
                    f"/api/v1/runs/{run_id}/message",
                    json={"message": "Continue after resume"},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/compact",
                    json={"max_history_items": 25},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/model",
                    json={"model_profile": "deep"},
                ),
                await client.post(
                    f"/api/v1/approvals/{approvals[0].id}/approve",
                    json={},
                ),
                await client.post(
                    f"/api/v1/approvals/{approvals[1].id}/reject",
                    json={"reason": "Not now"},
                ),
            ]
            assert [response.status_code for response in accepted] == [202, 202, 202, 200, 200]
            await _reconcile_workflow_signals_once(runtime)
            assert [call[0] for call in runtime.workflow.calls] == [
                "pause",
                "message",
                "compact",
                "switch_model",
                "approve",
                "reject",
            ]
    finally:
        blocker.release.set()
        if pause_task is not None:
            await asyncio.gather(pause_task, return_exceptions=True)
        await runtime.control_plane.close()


@pytest.mark.parametrize(
    "fence_status",
    [RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.COMPLETING],
)
@pytest.mark.asyncio
async def test_every_safety_fence_rejects_ordinary_workflow_signals(
    tmp_path: Path,
    fence_status: RunStatus,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            tool_call = ToolCall(
                id=f"tool-call-{fence_status.value}",
                sdk_call_id=f"sdk-call-{fence_status.value}",
                run_id=run_id,
                agent_step_id=f"step-{fence_status.value}",
                tool_id="python",
            )
            approval = Approval(
                id=f"approval-{fence_status.value}",
                run_id=run_id,
                tool_call_id=tool_call.id,
                tool_name="python",
            )
            await runtime.approval_repository.create_request(tool_call, approval)
            await runtime.run_repository.update_status(run_id, fence_status)

            responses = [
                await client.post(
                    f"/api/v1/runs/{run_id}/message",
                    json={"message": "Blocked instruction"},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/compact",
                    json={"max_history_items": 50},
                ),
                await client.post(
                    f"/api/v1/runs/{run_id}/model",
                    json={"model_profile": "deep"},
                ),
                await client.post(
                    f"/api/v1/approvals/{approval.id}/approve",
                    json={},
                ),
                await client.post(
                    f"/api/v1/approvals/{approval.id}/reject",
                    json={"reason": "Blocked"},
                ),
            ]

            assert [response.status_code for response in responses] == [409] * 5
            assert all(
                response.json()["error"]["details"].get("status", fence_status.value)
                == fence_status.value
                for response in responses[:3]
            )
            assert all(
                response.json()["error"]["details"]["run_status"] == fence_status.value
                for response in responses[3:]
            )
            persisted = await runtime.approval_repository.get(approval.id)
            assert persisted is not None and persisted.status.value == "pending"
            assert runtime.workflow.calls == []
            if fence_status in {RunStatus.CANCELLING, RunStatus.COMPLETING}:
                pause = await client.post(f"/api/v1/runs/{run_id}/pause")
                assert pause.status_code == 409
                assert pause.json()["error"]["code"] == "run_not_controllable"
                assert pause.json()["error"]["details"]["status"] == fence_status.value
                assert runtime.workflow.calls == []
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_approval_does_not_signal_when_pause_wins_after_atomic_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _build_runtime(tmp_path)
    decision_committed = asyncio.Event()
    continue_service = asyncio.Event()
    approval_task: asyncio.Task[httpx.Response] | None = None
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            tool_call = ToolCall(
                id="tool-call-approval-pause-race",
                sdk_call_id="sdk-call-approval-pause-race",
                run_id=run_id,
                agent_step_id="step-approval-pause-race",
                tool_id="python",
            )
            approval = Approval(
                id="approval-pause-race",
                run_id=run_id,
                tool_call_id=tool_call.id,
                tool_name="python",
            )
            await runtime.approval_repository.create_request(tool_call, approval)
            original_decide = runtime.approval_repository.decide_runtime

            async def hold_after_atomic_decision(
                *args: Any,
                _original_decide: Any = original_decide,
                **kwargs: Any,
            ) -> object:
                result = await _original_decide(*args, **kwargs)
                decision_committed.set()
                await continue_service.wait()
                return result

            monkeypatch.setattr(
                runtime.approval_repository,
                "decide_runtime",
                hold_after_atomic_decision,
            )
            approval_task = asyncio.create_task(
                client.post(
                    f"/api/v1/approvals/{approval.id}/approve",
                    json={},
                )
            )
            await asyncio.wait_for(decision_committed.wait(), timeout=1)
            await runtime.run_repository.update_status(run_id, RunStatus.PAUSING)
            continue_service.set()
            response = await asyncio.wait_for(approval_task, timeout=1)
            approval_task = None

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "approval_not_actionable"
            assert response.json()["error"]["details"] == {
                "approval_id": approval.id,
                "run_id": run_id,
                "run_status": "pausing",
                "approval_saved": True,
            }
            persisted = await runtime.approval_repository.get(approval.id)
            assert persisted is not None and persisted.status.value == "approved"
            assert not any(call[0] == "approve" for call in runtime.workflow.calls)
    finally:
        continue_service.set()
        if approval_task is not None:
            await asyncio.gather(approval_task, return_exceptions=True)
        await runtime.control_plane.close()


@pytest.mark.parametrize("target", [RunStatus.COMPLETED, RunStatus.FAILED])
@pytest.mark.asyncio
async def test_owner_reconciler_terminalizes_completing_run_from_durable_intent(
    tmp_path: Path,
    target: RunStatus,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.fence_finalization(run_id, target)

            result = await runtime.control_plane.run_service.stop_resources_for_cleanup(run_id)

            assert result.succeeded is True
            finalized = await runtime.run_repository.get(run_id)
            assert finalized is not None and finalized.status is target
            timeline = await client.get(f"/api/v1/runs/{run_id}/events")
            event_types = [item["event_type"] for item in timeline.json()["items"]]
            assert event_types.count("run.cleaned_up") == 1
            reconciled = [
                item
                for item in timeline.json()["items"]
                if item["event_type"] == "run.cleanup_reconciled"
            ]
            assert reconciled[-1]["payload"]["finalization_target"] == target.value
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_owner_reconciler_keeps_completing_without_trusted_intent(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.update_status(run_id, RunStatus.COMPLETING)

            with pytest.raises(ApplicationConflictError) as missing:
                await runtime.control_plane.run_service.stop_resources_for_cleanup(run_id)

            assert missing.value.code == "run_finalization_intent_missing"
            fenced = await runtime.run_repository.get(run_id)
            assert fenced is not None and fenced.status is RunStatus.COMPLETING
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_resume_converges_paused_failure_intent_after_legacy_workflow_closed(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.update_status(run_id, RunStatus.PAUSING)
            await runtime.run_repository.record_finalization_intent(run_id, RunStatus.FAILED)
            await runtime.run_repository.update_status(run_id, RunStatus.PAUSED)
            assert isinstance(runtime.workflow, FakeWorkflowClient)
            runtime.workflow.error = ApplicationConflictError(
                "workflow_not_running",
                "The legacy Workflow already closed",
            )

            response = await client.post(f"/api/v1/runs/{run_id}/resume")

            assert response.status_code == 202, response.text
            assert response.json()["run"]["status"] == "failed"
            finalized = await runtime.run_repository.get(run_id)
            assert finalized is not None and finalized.status is RunStatus.FAILED
            timeline = await client.get(f"/api/v1/runs/{run_id}/events")
            event_types = [item["event_type"] for item in timeline.json()["items"]]
            assert event_types.count("run.cleaned_up") == 1
            assert "workflow.signal_failed" in event_types
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_failure_intent_winning_resume_transition_never_reopens_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _build_runtime(tmp_path)
    resume_reached_transition = asyncio.Event()
    allow_resume_transition = asyncio.Event()
    resume_task: asyncio.Task[httpx.Response] | None = None
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.update_status(run_id, RunStatus.PAUSING)
            await runtime.run_repository.update_status(run_id, RunStatus.PAUSED)
            original_update_status = runtime.run_repository.update_status

            async def gate_running_transition(
                target_run_id: str,
                target: RunStatus,
                _original_update_status: Any = original_update_status,
            ) -> object:
                if target is RunStatus.RUNNING:
                    resume_reached_transition.set()
                    await allow_resume_transition.wait()
                return await _original_update_status(target_run_id, target)

            monkeypatch.setattr(
                runtime.run_repository,
                "update_status",
                gate_running_transition,
            )
            resume_task = asyncio.create_task(client.post(f"/api/v1/runs/{run_id}/resume"))
            await asyncio.wait_for(resume_reached_transition.wait(), timeout=1)
            await runtime.run_repository.record_finalization_intent(run_id, RunStatus.FAILED)
            allow_resume_transition.set()

            response = await asyncio.wait_for(resume_task, timeout=1)
            resume_task = None
            assert response.status_code == 202, response.text
            assert response.json()["run"]["status"] == "failed"
            finalized = await runtime.run_repository.get(run_id)
            assert finalized is not None and finalized.status is RunStatus.FAILED
            timeline = await client.get(f"/api/v1/runs/{run_id}/events")
            transitions = [
                item["payload"]
                for item in timeline.json()["items"]
                if item["event_type"] == "run.status_changed"
            ]
            assert {"from": "paused", "to": "running"} not in transitions
            assert {"from": "paused", "to": "completing"} in transitions
    finally:
        allow_resume_transition.set()
        if resume_task is not None:
            await asyncio.gather(resume_task, return_exceptions=True)
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_manual_memory_management_scope_search_and_lifecycle(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            missing_source = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Invalid",
                    "content": "No source",
                    "summary": "No source",
                    "source_refs": [],
                },
            )
            assert missing_source.status_code == 422
            first = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Staging proxy",
                    "content": "Use SOCKS5 on 127.0.0.1:1080.",
                    "summary": "Staging SOCKS5 proxy",
                    "retrieval_keywords": ["proxy", "socks5"],
                    "confidence": 0.9,
                    "importance": 0.8,
                    "source_refs": ["user://messages/message-1"],
                },
            )
            assert first.status_code == 201, first.text
            first_id = first.json()["id"]
            other = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-2",
                    "title": "Other customer proxy",
                    "content": "Use another proxy.",
                    "summary": "Other proxy",
                    "retrieval_keywords": ["proxy"],
                    "source_refs": ["user://messages/message-2"],
                },
            )
            assert other.status_code == 201

            scoped = await client.get(
                "/api/v1/memories",
                params={"scope_type": "engagement", "scope_id": "engagement-1"},
            )
            assert [item["id"] for item in scoped.json()["items"]] == [first_id]
            edited = await client.patch(
                f"/api/v1/memories/{first_id}",
                json={"summary": "Updated staging proxy"},
            )
            assert edited.status_code == 200
            assert edited.json()["summary"] == "Updated staging proxy"
            pinned = await client.post(
                f"/api/v1/memories/{first_id}/pin",
                json={"pinned": True},
            )
            assert pinned.json()["pinned"] is True
            search = await client.get(
                "/api/v1/memories/search",
                params={"q": "unrelated", "engagement_id": "engagement-1"},
            )
            assert [item["id"] for item in search.json()["items"]] == [first_id]

            replacement = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Replacement proxy",
                    "content": "Use SOCKS5 on 127.0.0.1:2080.",
                    "summary": "Replacement SOCKS5 proxy",
                    "retrieval_keywords": ["proxy", "socks5"],
                    "source_refs": ["artifact://runs/run-1/executions/ex-1/stdout"],
                    "supersedes": first_id,
                },
            )
            assert replacement.status_code == 201, replacement.text
            replacement_id = replacement.json()["id"]
            deleted = await client.delete(f"/api/v1/memories/{replacement_id}")
            assert deleted.status_code == 200
            assert deleted.json()["status"] == "deleted"
            inactive = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "include_inactive": True,
                },
            )
            assert {item["status"] for item in inactive.json()["items"]} == {
                "superseded",
                "deleted",
            }
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_sse_resumes_from_last_event_id(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "Continue from here"},
            )

            response = await client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={"Last-Event-ID": "2"},
                params={"follow": "false"},
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "id: 3" in response.text
            assert "event: user.message_queued" in response.text
            assert "id: 1" not in response.text
            assert "id: 2" not in response.text

            invalid = await client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={"Last-Event-ID": "not-a-sequence"},
                params={"follow": "false"},
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "invalid_last_event_id"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_tools_and_findings_share_persisted_control_plane(tmp_path: Path) -> None:
    runtime = await _build_runtime(
        tmp_path,
        admin_token="test-only-admin-operator-token-0002",
    )
    try:
        async for client in _client(runtime.control_plane):
            admin_headers = {"Authorization": "Bearer test-only-admin-operator-token-0002"}
            tools = await client.get("/api/v1/nodes/local/tools")
            assert tools.status_code == 200
            assert tools.json()["execution_policy"] == "registered_only"
            assert tools.json()["tools"][0]["definition"]["id"] == "python"
            assert tools.json()["tools"][0]["state"]["availability"] == "available"
            assert "environment" not in tools.json()["tools"][0]["definition"]

            denied_admin_tools = await client.get(
                "/api/v1/nodes/local/tools/admin",
                headers={"Authorization": ""},
            )
            assert denied_admin_tools.status_code == 401
            invalid_admin_token = "administrator-token-must-not-echo"
            denied_wrong_admin = await client.get(
                "/api/v1/nodes/local/tools/admin",
                headers={"Authorization": f"Bearer {invalid_admin_token}"},
            )
            assert denied_wrong_admin.status_code == 401
            assert invalid_admin_token not in denied_wrong_admin.text

            denied_refresh = await client.post(
                "/api/v1/nodes/local/refresh-tools",
                headers={"Authorization": ""},
            )
            assert denied_refresh.status_code == 401
            refreshed = await client.post(
                "/api/v1/nodes/local/refresh-tools",
                headers=admin_headers,
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["generation"] == tools.json()["generation"] + 1

            updated = await client.put(
                "/api/v1/nodes/local/tools/python",
                headers=admin_headers,
                json={
                    "enabled": False,
                    "command": ["python"],
                    "executor": "process",
                    "capabilities": ["scripting", "edited"],
                    "approval": "sensitive",
                    "timeout": 45,
                    "environment": {"RIFTX_EDITED": "tool-environment-secret"},
                },
            )
            assert updated.status_code == 200
            updated_payload = updated.json()
            assert updated_payload["generation"] == refreshed.json()["generation"] + 1
            definition = updated_payload["tools"][0]["definition"]
            assert definition["enabled"] is False
            assert definition["capabilities"] == ["scripting", "edited"]
            assert definition["environment_variables"] == ["RIFTX_EDITED"]
            assert "environment" not in definition
            assert "tool-environment-secret" not in updated.text
            assert updated_payload["tools"][0]["state"]["availability"] == "disabled"

            public_tools = await client.get("/api/v1/nodes/local/tools")
            assert public_tools.status_code == 200
            assert "tool-environment-secret" not in public_tools.text
            assert public_tools.json()["tools"][0]["definition"]["environment_variables"] == [
                "RIFTX_EDITED"
            ]

            admin_tools = await client.get(
                "/api/v1/nodes/local/tools/admin",
                headers=admin_headers,
            )
            assert admin_tools.status_code == 200
            assert admin_tools.json()["tools"][0]["definition"]["environment"] == {
                "RIFTX_EDITED": "tool-environment-secret"
            }
            assert public_tools.json()["source_digest"] != admin_tools.json()["source_digest"]

            invalid_environment_value = "invalid-environment-value-must-not-echo"
            invalid_update = await client.put(
                "/api/v1/nodes/local/tools/python",
                headers=admin_headers,
                json={
                    "enabled": False,
                    "command": ["python"],
                    "executor": "process",
                    "capabilities": ["scripting"],
                    "approval": "sensitive",
                    "timeout": 45,
                    "environment": {"RIFTX_EDITED": [invalid_environment_value]},
                },
            )
            assert invalid_update.status_code == 422
            assert invalid_environment_value not in invalid_update.text
            persisted_text = await asyncio.to_thread(
                runtime.control_plane.settings.tools_config_path.read_text
            )
            persisted_tools = yaml.safe_load(persisted_text)
            assert persisted_tools["tools"]["python"]["approval"] == "sensitive"

            missing_node = await client.get("/api/v1/nodes/remote/tools")
            assert missing_node.status_code == 404
            assert missing_node.json()["error"]["code"] == "node_not_found"

            created = await _create_run(client)
            run_id = str(created["id"])
            finding = Finding(
                run_id=run_id,
                title="Exposed development service",
                severity=FindingSeverity.MEDIUM,
                description="The endpoint exposes development metadata.",
            )
            await runtime.finding_repository.create(finding)

            findings = await client.get(f"/api/v1/runs/{run_id}/findings")
            assert findings.status_code == 200
            assert findings.json()["items"][0]["id"] == finding.id
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_unified_errors_and_temporal_outage(tmp_path: Path) -> None:
    workflow = FakeWorkflowClient(
        error=ServiceUnavailableError(
            "temporal_unavailable",
            "Temporal is unavailable",
        )
    )
    runtime = await _build_runtime(tmp_path, workflow=workflow)
    try:
        async for client in _client(runtime.control_plane):
            missing = await client.get("/api/v1/runs/missing")
            assert missing.status_code == 404
            assert missing.json() == {
                "error": {
                    "code": "resource_not_accessible",
                    "message": "The requested resource was not found",
                    "details": {},
                }
            }

            invalid = await client.post("/api/v1/runs", json={"objective": ""})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "validation_error"

            missing_route = await client.get("/api/v1/not-a-route")
            assert missing_route.status_code == 404
            assert missing_route.json()["error"]["code"] == "route_not_found"

            created = await client.post(
                "/api/v1/runs",
                json={"objective": "Saved despite outage"},
            )
            assert created.status_code == 201
            assert created.json()["status"] == "waiting_user"
            run_id = created.json()["id"]

            requested_message_event_id = str(uuid4())
            unavailable = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={
                    "message": "Begin only after Temporal recovers",
                    "message_event_id": requested_message_event_id,
                },
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "temporal_unavailable"
            message_event_id = unavailable.json()["error"]["details"]["message_event_id"]
            assert message_event_id == requested_message_event_id
            assert unavailable.json()["error"]["details"]["retry_same_message"] is True

            persisted = await client.get(f"/api/v1/runs/{run_id}")
            assert persisted.status_code == 200
            events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert [item["event_type"] for item in events.json()["items"]] == [
                "run.created",
                "conversation.context_ready",
                "user.message_queued",
            ]

            workflow.error = None
            mismatched_retry = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={
                    "message": "A different instruction",
                    "message_event_id": message_event_id,
                },
            )
            assert mismatched_retry.status_code == 409
            assert mismatched_retry.json()["error"]["code"] == "message_retry_conflict"
            recovered = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={
                    "message": "Begin only after Temporal recovers",
                    "message_event_id": message_event_id,
                },
            )
            assert recovered.status_code == 202
            assert [call[0] for call in workflow.calls] == ["message"]
            assert workflow.calls[0][2] == message_event_id
            recovered_events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert [
                item["id"]
                for item in recovered_events.json()["items"]
                if item["event_type"] == "user.message_queued"
            ] == [message_event_id]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_production_control_plane_defers_and_retries_temporal_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text("version: 1\nexecution_policy: registered_only\ntools: {}\n")
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        """\
default_profile: primary
models:
  primary:
    provider: openai_compatible
    model: test-model
    api: chat_completions
    base_url: http://127.0.0.1:8000/v1
    requires_api_key: false
"""
    )
    temporal = RecordingSignalWithStartClient()
    connection_attempts: list[tuple[str, str]] = []

    async def connect(
        cls: type[Client],
        target_host: str,
        *,
        namespace: str = "default",
        **_: object,
    ) -> object:
        del cls
        connection_attempts.append((target_host, namespace))
        if len(connection_attempts) == 1:
            raise ConnectionError("Temporal is still starting")
        return temporal

    monkeypatch.setattr(Client, "connect", classmethod(connect))
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "secrets" / "local-principal.json",
        admin_token="test-only-local-operator-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'lazy-temporal.db'}",
        tools_config_path=tools_path,
        models_config_path=models_path,
        model_secrets_path=tmp_path / "secrets" / "models.json",
        workspace_root=tmp_path / "workspaces",
        runner_state_path=tmp_path / "runner",
        temporal_address="temporal.test:7233",
        temporal_namespace="test-namespace",
        sse_poll_interval_seconds=0.001,
        sse_heartbeat_seconds=0.005,
    )

    runtime = await build_control_plane(settings)
    assert isinstance(runtime.audit_service, AuditApplicationService)
    assert isinstance(runtime.audit_preflight_service, AuditPreflightApplicationService)
    process_executor = runtime.process_supervisor._process_executor
    assert process_executor._require_containment is True
    assert runtime.terminal_supervisor._require_containment is True
    assert runtime.terminal_supervisor._containment_manager is process_executor.containment_manager
    assert runtime.execution_runner._local_terminal is runtime.terminal_supervisor
    cleanup_reconciler = runtime._cleanup_reconciler_task
    try:
        assert cleanup_reconciler is not None and not cleanup_reconciler.done()
        assert connection_attempts == []
        async for client in _client(runtime):
            disabled_preflight = await client.post(
                "/api/v1/audits/preflight",
                json={
                    "repository_path": "/RIFTX-PREFLIGHT-CANARY",
                    "unknown": "RIFTX-PREFLIGHT-CANARY",
                },
            )
            assert disabled_preflight.status_code == 503
            assert disabled_preflight.json()["error"]["code"] == "audit_feature_disabled"
            assert "RIFTX-PREFLIGHT-CANARY" not in disabled_preflight.text
            missing_preflight = await client.get(
                "/api/v1/audits/preflight/missing-preflight-job"
            )
            missing_preflight_cancel = await client.post(
                "/api/v1/audits/preflight/missing-preflight-job/cancel"
            )
            assert missing_preflight.status_code == 404
            assert missing_preflight_cancel.status_code == 404
            assert missing_preflight.json()["error"]["code"] == "resource_not_accessible"
            assert missing_preflight_cancel.json() == missing_preflight.json()

            created = await client.post(
                "/api/v1/runs",
                json={"objective": "Wait for an explicit instruction"},
            )
            assert created.status_code == 201, created.text
            run_id = str(created.json()["id"])
            assert created.json()["status"] == "waiting_user"
            assert connection_attempts == []

            paused = await client.post(f"/api/v1/runs/{run_id}/pause")
            assert paused.status_code == 202
            assert paused.json()["run"]["status"] == "paused"
            pause_events = await client.get(f"/api/v1/runs/{run_id}/events")
            pause_event = next(
                item
                for item in pause_events.json()["items"]
                if item["event_type"] == "run.pause_requested"
            )
            assert set(pause_event["payload"]["stop_resources"]) == {
                "executions",
                "browser_sessions",
                "target_http_requests",
            }
            resumed = await client.post(f"/api/v1/runs/{run_id}/resume")
            assert resumed.status_code == 202
            assert resumed.json()["run"]["status"] == "waiting_user"
            assert connection_attempts == []

            cancelled_before_start = await client.post(
                "/api/v1/runs",
                json={"objective": "Cancel without starting Temporal"},
            )
            assert cancelled_before_start.status_code == 201
            cancelled_run_id = str(cancelled_before_start.json()["id"])
            stopped = await client.post(f"/api/v1/runs/{cancelled_run_id}/cancel-current-execution")
            assert stopped.status_code == 202
            cancelled = await client.post(f"/api/v1/runs/{cancelled_run_id}/cancel")
            assert cancelled.status_code == 202
            assert cancelled.json()["run"]["status"] == "cancelled"
            assert connection_attempts == []

            requested_message_event_id = str(uuid4())
            unavailable = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={
                    "message": "Start when Temporal becomes available",
                    "message_event_id": requested_message_event_id,
                },
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "temporal_unavailable"
            message_event_id = unavailable.json()["error"]["details"]["message_event_id"]
            assert message_event_id == requested_message_event_id

            recovered = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={
                    "message": "Start when Temporal becomes available",
                    "message_event_id": message_event_id,
                },
            )
            assert recovered.status_code == 202, recovered.text

        assert connection_attempts == [
            ("temporal.test:7233", "test-namespace"),
            ("temporal.test:7233", "test-namespace"),
        ]
        assert len(temporal.calls) == 1
        _, start_options = temporal.calls[0]
        assert start_options["start_signal"] == "user_input"
        assert start_options["start_signal_args"] == [message_event_id]
        assert start_options["id_conflict_policy"] is WorkflowIDConflictPolicy.USE_EXISTING
        assert start_options["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    finally:
        await runtime.close()
    assert cleanup_reconciler is not None and cleanup_reconciler.done()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group verification")
@pytest.mark.asyncio
async def test_api_cancel_kills_uncontained_process_group_but_refuses_stop_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort cleanup must never become false whole-tree stop evidence."""

    # This one test deliberately exercises the development escape hatch. The
    # API integration suite otherwise injects affirmative test containment for
    # ordinary lifecycle coverage.
    monkeypatch.setattr(
        LinuxCgroupV2Manager,
        "autodetect",
        classmethod(lambda cls, **kwargs: None),
    )

    async def wait_for_nonempty_file(path: Path, timeout_seconds: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if await asyncio.to_thread(lambda: path.exists() and bool(path.stat().st_size)):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out waiting for non-empty file {path}")

    def process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        return True

    async def wait_for_process_exit(pid: int, timeout_seconds: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if not process_exists(pid):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"process {pid} did not exit")

    async def wait_for_process_group_exit(
        process_group_id: int,
        timeout_seconds: float = 3.0,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if not process_group_exists(process_group_id):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"process group {process_group_id} did not exit")

    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text("version: 1\nexecution_policy: registered_only\ntools: {}\n")
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        """\
default_profile: primary
models:
  primary:
    provider: openai_compatible
    model: test-model
    api: chat_completions
    base_url: http://127.0.0.1:8000/v1
    requires_api_key: false
"""
    )
    heartbeat = tmp_path / "child-heartbeat"
    runtime: ControlPlane | None = None
    parent_pid: int | None = None
    child_pid: int | None = None
    process_group_id: int | None = None

    # Reserving, but deliberately not listening on, an ephemeral loopback port
    # gives the production Temporal client a deterministic real transport
    # failure without starting another service.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable_temporal:
        unavailable_temporal.bind(("127.0.0.1", 0))
        temporal_port = unavailable_temporal.getsockname()[1]
        settings = APISettings(
            trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
            local_principal_path=tmp_path / "secrets" / "local-principal.json",
            admin_token="test-only-local-operator-token-0001",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'stop-outage.db'}",
            tools_config_path=tools_path,
            models_config_path=models_path,
            model_secrets_path=tmp_path / "secrets" / "models.json",
            workspace_root=tmp_path / "workspaces",
            runner_state_path=tmp_path / "runner",
            temporal_address=f"127.0.0.1:{temporal_port}",
            sse_poll_interval_seconds=0.001,
            sse_heartbeat_seconds=0.005,
            # This test exercises the explicit development escape hatch and
            # verifies fail-closed cancellation when only a POSIX process
            # group—not kernel containment—is available.
            require_containment=False,
        )

        try:
            runtime = await build_control_plane(settings)
            run_repository = SQLAlchemyRunRepository(runtime.database.session_factory)
            supervisor = runtime.process_supervisor
            assert supervisor is not None

            async for client in _client(runtime):
                created = await client.post(
                    "/api/v1/runs",
                    json={"objective": "Stop a harmless process tree during an outage"},
                )
                assert created.status_code == 201, created.text
                run_id = str(created.json()["id"])
                await run_repository.update_status(run_id, RunStatus.PREPARING)
                await run_repository.update_status(run_id, RunStatus.RUNNING)

                started = await supervisor.start(
                    ExecutionLaunchRequest(
                        execution_key=f"api-stop-tree:{run_id}",
                        execution_id="api-stop-process-tree",
                        run_id=run_id,
                        node_id=settings.node_id,
                        executor_type=ExecutorType.PROCESS,
                        cwd=tmp_path,
                        argv=[
                            sys.executable,
                            str(PROCESS_TREE_FIXTURE),
                            "child",
                            "--seconds",
                            "30",
                            "--heartbeat",
                            str(heartbeat),
                        ],
                        tool_id="harmless-process-tree-fixture",
                    )
                )
                assert started.status is ExecutionStatus.RUNNING
                assert started.pid is not None
                assert started.process_group_id is not None
                parent_pid = started.pid
                process_group_id = started.process_group_id
                assert process_group_id != os.getpgrp()
                await wait_for_nonempty_file(Path(started.stdout_path))
                await wait_for_nonempty_file(heartbeat)
                output = await supervisor.read_output(started.id)
                child_pid = int(output.stdout.data.decode().strip().splitlines()[0])
                assert process_exists(parent_pid)
                assert process_exists(child_pid)
                assert process_group_exists(process_group_id)

                cancelled = await asyncio.wait_for(
                    client.post(f"/api/v1/runs/{run_id}/cancel"),
                    timeout=15,
                )

                assert cancelled.status_code == 503, cancelled.text
                error = cancelled.json()["error"]
                assert error["code"] == "execution_cancel_failed"
                assert error["details"]["confirmed_execution_ids"] == []
                assert (
                    "complete descendant absence cannot be proven"
                    in error["details"]["failed_executions"][started.id]
                )
                await wait_for_process_exit(parent_pid)
                await wait_for_process_exit(child_pid)
                await wait_for_process_group_exit(process_group_id)
                await supervisor.wait(started.id)

                execution_response = await client.get(f"/api/v1/executions/{started.id}")
                assert execution_response.status_code == 200
                # Even though the known process group is gone, the durable
                # record stays active because an escaped descendant cannot be
                # ruled out without kernel containment.
                assert execution_response.json()["status"] == "running"
                run_response = await client.get(f"/api/v1/runs/{run_id}")
                assert run_response.status_code == 200
                assert run_response.json()["status"] == "cancelling"

                events = await client.get(f"/api/v1/runs/{run_id}/events")
                assert events.status_code == 200
                event_items = events.json()["items"]
                cancel_event = next(
                    item for item in event_items if item["event_type"] == "run.cancel_requested"
                )
                assert cancel_event["payload"]["workflow_synced"] is False
                assert cancel_event["payload"]["confirmed_execution_ids"] == []
                stop_resources = cancel_event["payload"]["stop_resources"]
                assert stop_resources["executions"]["confirmed_ids"] == []
                assert started.id in stop_resources["executions"]["failures"]
                assert stop_resources["browser_sessions"]["attempted_ids"] == []
                assert stop_resources["target_http_requests"]["attempted_ids"] == []
                # The Workflow must stay blocked when physical stop cannot be
                # proven, so Temporal is intentionally not signalled at all.
                assert all(item["event_type"] != "workflow.signal_failed" for item in event_items)
        finally:
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                # Every cleanup target comes from this test's unique process;
                # never use a name-based or workspace-wide kill.
                if process_group_id is not None and process_group_exists(process_group_id):
                    os.killpg(process_group_id, signal.SIGKILL)
                    await wait_for_process_group_exit(process_group_id)
                if parent_pid is not None:
                    await wait_for_process_exit(parent_pid)
                if child_pid is not None:
                    await wait_for_process_exit(child_pid)


@pytest.mark.asyncio
async def test_api_restart_recovers_runs_from_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "durable.db"
    first = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(first.control_plane):
        created = await _create_run(client)
        run_id = str(created["id"])
    await first.control_plane.close()

    second = await _build_runtime(tmp_path, database_path=database_path)
    try:
        async for client in _client(second.control_plane):
            recovered = await client.get(f"/api/v1/runs/{run_id}")
            assert recovered.status_code == 200
            assert recovered.json()["id"] == run_id
            assert recovered.json()["objective"]["description"] == "Inspect the local service"
    finally:
        await second.control_plane.close()


def test_api_settings_load_web_dist_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web_dist = tmp_path / "web-dist"
    monkeypatch.setenv("RIFTX_WEB_DIST", str(web_dist))
    monkeypatch.setenv("RIFTX_TRUST_PROFILE", "local_single_operator")
    monkeypatch.setenv("RIFTX_ADMIN_TOKEN", "test-only-local-operator-token-0001")

    assert APISettings.from_environment().web_dist_path == web_dist


@pytest.mark.asyncio
async def test_built_web_ui_uses_spa_fallback(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    web_dist = tmp_path / "web-dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<html><body>RiftX WebUI</body></html>")
    runtime.control_plane.settings = replace(
        runtime.control_plane.settings,
        web_dist_path=web_dist,
    )
    try:
        async for client in _client(runtime.control_plane):
            response = await client.get("/runs/example-run")
            assert response.status_code == 200
            assert "RiftX WebUI" in response.text
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_approval_endpoints_decide_and_recover_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-control-plane.db"
    runtime = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id="tool-call-1",
            sdk_call_id="sdk-call-1",
            run_id=str(run["id"]),
            agent_step_id="step-1",
            tool_id="python",
            arguments={"args": ["--version"]},
        )
        approval = Approval(
            id="approval-1",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
            command=["python", "--version"],
            cwd=str(tmp_path),
            target_summary="ip:127.0.0.1",
            env_diff={"RIFTX_TEST": "1"},
            reason="Verify the local runtime.",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        database = runtime.control_plane.database
        await SQLAlchemyAgentSessionRepository(database.session_factory).create(
            AgentSession(
                id="session-1",
                run_id=str(run["id"]),
                model_profile="fast",
            )
        )
        await SQLAlchemyAgentCycleRepository(database.session_factory).create(
            RuntimeAgentCycle(
                id="cycle-1",
                run_id=str(run["id"]),
                session_id="session-1",
                sequence=1,
            )
        )
        await SQLAlchemyAgentStepRepository(database.session_factory).create(
            AgentStep(
                id="step-1",
                cycle_id="cycle-1",
                sequence=1,
                step_type=AgentStepType.TOOL_PROPOSAL,
            )
        )
        await SQLAlchemyToolCallIntentRepository(database.session_factory).create(
            ToolCallIntent(
                id="intent-1",
                run_id=str(run["id"]),
                session_id="session-1",
                cycle_id="cycle-1",
                step_id="step-1",
                tool_id="python",
            )
        )
        await runtime.runtime_approval_repository.create(
            RuntimeApprovalRequest(
                id=approval.id,
                run_id=str(run["id"]),
                session_id="session-1",
                cycle_id="cycle-1",
                tool_call_intent_id="intent-1",
            )
        )

        profile = await client.get("/api/v1/security/profile")
        principal_id = profile.json()["principal_id"]

        listed = await client.get(f"/api/v1/runs/{run['id']}/approvals")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["command"] == ["python", "--version"]

        forged = await client.post(
            "/api/v1/approvals/approval-1/approve",
            json={
                "approve_for_run": True,
                "decided_by": "forged-client",
                "created_by": "forged-client",
                "requester_principal_id": "forged-client",
                "role": "owner",
                "user_id": "forged-client",
            },
        )
        assert forged.status_code == 422
        assert (await runtime.approval_repository.get(approval.id)).status.value == "pending"
        runtime_approval = await runtime.runtime_approval_repository.get(approval.id)
        assert runtime_approval is not None
        assert runtime_approval.status.value == "pending"
        assert not await runtime.approval_repository.is_granted(str(run["id"]), "python")
        assert not [call for call in runtime.workflow.calls if call[0] == "approve"]

        approved = await client.post(
            "/api/v1/approvals/approval-1/approve",
            json={"approve_for_run": True},
            headers={
                "Authorization": "Bearer test-only-local-operator-token-0001",
                "X-Forwarded-User": "proxy-forgery",
                "X-Forwarded-Role": "owner",
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["decided_by"] == principal_id
        assert await runtime.approval_repository.is_granted(str(run["id"]), "python")
        grant = await runtime.approval_repository._get_grant(str(run["id"]), "python")
        assert grant is not None
        assert grant.created_by == principal_id
        runtime_request = await runtime.runtime_approval_repository.get(approval.id)
        assert runtime_request is not None
        assert runtime_request.decided_by == principal_id
        approval_intents = await _workflow_signal_records(runtime, str(run["id"]))
        assert [(item.signal_kind, item.delivery_state) for item in approval_intents] == [
            ("approve", "pending")
        ]
        assert approval_intents[0].workflow_id == run["temporal_workflow_id"]
        await _reconcile_workflow_signals_once(runtime)
        approval_intents = await _workflow_signal_records(runtime, str(run["id"]))
        assert approval_intents[0].delivery_state == "delivered"
        assert ("approve", str(run["id"]), "approval-1") in runtime.workflow.calls
        assert (
            "approve",
            str(run["id"]),
            str(run["temporal_workflow_id"]),
        ) in runtime.workflow.workflow_ids
        decision_events = await client.get(f"/api/v1/runs/{run['id']}/events")
        approved_event = next(
            item
            for item in decision_events.json()["items"]
            if item["event_type"] == "tool.approved"
        )
        assert approved_event["payload"]["decided_by"] == principal_id
        assert "forged-client" not in json.dumps(approved_event)

        rejected_call = ToolCall(
            id="tool-call-2",
            sdk_call_id="sdk-call-2",
            run_id=str(run["id"]),
            agent_step_id="step-1",
            tool_id="python",
            arguments={"args": ["unsafe.py"]},
        )
        rejected_request = Approval(
            id="approval-2",
            run_id=str(run["id"]),
            tool_call_id=rejected_call.id,
            tool_name="python",
            command=["python", "unsafe.py"],
            cwd=str(tmp_path),
            target_summary="ip:127.0.0.1",
            reason="Execute a follow-up action.",
        )
        await runtime.approval_repository.create_request(rejected_call, rejected_request)
        rejected = await client.post(
            "/api/v1/approvals/approval-2/reject",
            json={"reason": "Outside authorized scope"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["reason"] == "Outside authorized scope"
        approval_intents = await _workflow_signal_records(runtime, str(run["id"]))
        assert [(item.signal_kind, item.delivery_state) for item in approval_intents] == [
            ("approve", "delivered"),
            ("reject", "pending"),
        ]
        await _reconcile_workflow_signals_once(runtime)
        assert ("reject", str(run["id"]), "approval-2") in runtime.workflow.calls
        assert all(
            item.delivery_state == "delivered"
            for item in await _workflow_signal_records(runtime, str(run["id"]))
        )

    await runtime.control_plane.close()

    restarted = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(restarted.control_plane):
        response = await client.get(f"/api/v1/runs/{run['id']}/approvals?status=approved")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == ["approval-1"]
    await restarted.control_plane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status", "first_payload", "retry_payload"),
    [
        (
            "approve",
            "approved",
            {
                "reason": "Authorized after review",
                "approve_for_run": True,
            },
            {
                "reason": "Authorized after review",
                "approve_for_run": True,
            },
        ),
        (
            "reject",
            "rejected",
            {
                "reason": "Outside the authorized boundary",
            },
            {
                "reason": "Outside the authorized boundary",
            },
        ),
    ],
)
async def test_approval_signal_retry_recovers_without_changing_saved_decision(
    tmp_path: Path,
    action: str,
    expected_status: str,
    first_payload: dict[str, object],
    retry_payload: dict[str, object],
) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id=f"tool-call-{action}-outage",
            sdk_call_id=f"sdk-call-{action}-outage",
            run_id=str(run["id"]),
            agent_step_id="step-outage",
            tool_id="python",
        )
        approval = Approval(
            id=f"approval-{action}-outage",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
            reason="Original Agent request",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        runtime.workflow.fail = True

        response = await client.post(
            f"/api/v1/approvals/{approval.id}/{action}",
            json=first_payload,
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected_status
        persisted = await runtime.approval_repository.get(approval.id)
        assert persisted is not None
        assert persisted.status.value == expected_status
        profile = await client.get("/api/v1/security/profile")
        principal_id = profile.json()["principal_id"]
        assert persisted.decided_by == principal_id
        assert persisted.reason == first_payload["reason"]
        intents = await _workflow_signal_records(runtime, str(run["id"]))
        assert len(intents) == 1
        assert intents[0].signal_kind == action
        assert intents[0].delivery_state == "pending"

        await _reconcile_workflow_signals_once(runtime)
        intents = await _workflow_signal_records(runtime, str(run["id"]))
        assert len(intents) == 1
        assert intents[0].delivery_state == "retryable"
        assert intents[0].attempt == 1
        assert intents[0].last_error_code == "reconciled_not_delivered"
        assert not [call for call in runtime.workflow.calls if call[0] in {"approve", "reject"}]

        runtime.workflow.fail = False
        recovered = await client.post(
            f"/api/v1/approvals/{approval.id}/{action}",
            json=retry_payload,
        )

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == expected_status
        assert recovered.json()["decided_by"] == principal_id
        assert recovered.json()["reason"] == first_payload["reason"]
        assert len(await _workflow_signal_records(runtime, str(run["id"]))) == 1
        await _reconcile_workflow_signals_once(runtime)
        assert [call for call in runtime.workflow.calls if call[0] in {"approve", "reject"}] == [
            (action, str(run["id"]), approval.id)
        ]
        delivered = await _workflow_signal_records(runtime, str(run["id"]))
        assert len(delivered) == 1
        assert delivered[0].delivery_state == "delivered"
        assert delivered[0].attempt == 2
        decided_events = await client.get(f"/api/v1/runs/{run['id']}/events")
        assert [
            item["event_type"]
            for item in decided_events.json()["items"]
            if item["event_type"] in {"tool.approved", "tool.rejected"}
        ] == [f"tool.{expected_status}"]
        if action == "approve":
            assert await runtime.approval_repository.is_granted(
                str(run["id"]),
                "python",
            )
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_approval_preserves_workflow_error_classification_in_outbox(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id="tool-call-closed-workflow",
            sdk_call_id="sdk-call-closed-workflow",
            run_id=str(run["id"]),
            agent_step_id="step-closed-workflow",
            tool_id="python",
        )
        approval = Approval(
            id="approval-closed-workflow",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        assert isinstance(runtime.workflow, FakeWorkflowClient)
        runtime.workflow.error = ApplicationConflictError(
            "workflow_not_running",
            "The Workflow is no longer running",
        )

        response = await client.post(
            "/api/v1/approvals/approval-closed-workflow/approve",
            json={},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        pending = await _workflow_signal_records(runtime, str(run["id"]))
        assert len(pending) == 1
        assert pending[0].delivery_state == "pending"

        await _reconcile_workflow_signals_once(runtime)

        superseded = await _workflow_signal_records(runtime, str(run["id"]))
        assert len(superseded) == 1
        assert superseded[0].delivery_state == "superseded"
        assert superseded[0].last_error_code == "workflow_not_running"
        assert superseded[0].next_attempt_at is None
        assert superseded[0].lease_owner is None
        assert superseded[0].lease_expires_at is None
        assert superseded[0].delivery_receipt_digest is None
        assert runtime.workflow.calls == []
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_terminal_run_approval_is_not_actionable(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        run_id = str(run["id"])
        tool_call = ToolCall(
            id="tool-call-terminal-run",
            sdk_call_id="sdk-call-terminal-run",
            run_id=run_id,
            agent_step_id="step-terminal-run",
            tool_id="python",
        )
        approval = Approval(
            id="approval-terminal-run",
            run_id=run_id,
            tool_call_id=tool_call.id,
            tool_name="python",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
        await runtime.run_repository.update_status(run_id, RunStatus.FAILED)

        response = await client.post(
            "/api/v1/approvals/approval-terminal-run/approve",
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "approval_not_actionable"
        persisted = await runtime.approval_repository.get("approval-terminal-run")
        assert persisted is not None
        assert persisted.status.value == "pending"
        assert isinstance(runtime.workflow, FakeWorkflowClient)
        assert not any(call[0] == "approve" for call in runtime.workflow.calls)
    await runtime.control_plane.close()


_TERMINAL_SCRIPT = r"""
import signal
import sys

def interrupted(signum, frame):
    print("INTERRUPTED", flush=True)

signal.signal(signal.SIGINT, interrupted)
print("READY", flush=True)
for line in sys.stdin:
    print("ECHO:" + line.rstrip(), flush=True)
"""


@pytest.mark.asyncio
async def test_terminal_rest_lifecycle_and_start_failure(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            created = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": [sys.executable, "-u", "-c", _TERMINAL_SCRIPT],
                    "owner": "agent",
                    "cols": 100,
                    "rows": 30,
                },
            )
            assert created.status_code == 201, created.text
            session = created.json()
            assert session["status"] == "open"
            assert session["owner"] == "agent"
            assert session["execution_status"] == "running"

            fetched = await client.get(f"/api/v1/terminals/{session['id']}")
            assert fetched.status_code == 200
            assert fetched.json()["id"] == session["id"]

            closed = await client.delete(f"/api/v1/terminals/{session['id']}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"
            assert closed.json()["execution_status"] == "cancelled"

            missing = await client.get("/api/v1/terminals/missing")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "resource_not_accessible"

            failed = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={"argv": [str(tmp_path / "does-not-exist")]},
            )
            assert failed.status_code == 409
            assert failed.json()["error"]["code"] == "terminal_start_failed"
    finally:
        await runtime.control_plane.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY process verification")
@pytest.mark.asyncio
async def test_production_pause_stops_local_pty_through_execution_router(
    tmp_path: Path,
) -> None:
    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text("version: 1\nexecution_policy: registered_only\ntools: {}\n")
    models_path = tmp_path / "models.yaml"
    models_path.write_text(
        """\
default_profile: primary
models:
  primary:
    provider: openai_compatible
    model: test-model
    api: chat_completions
    base_url: http://127.0.0.1:8000/v1
    requires_api_key: false
"""
    )
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "secrets" / "local-principal.json",
        admin_token="test-only-local-operator-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'local-pty-stop.db'}",
        tools_config_path=tools_path,
        models_config_path=models_path,
        model_secrets_path=tmp_path / "secrets" / "models.json",
        workspace_root=tmp_path / "workspaces",
        runner_state_path=tmp_path / "runner",
        temporal_address="temporal.test:7233",
        sse_poll_interval_seconds=0.001,
        sse_heartbeat_seconds=0.005,
    )
    runtime = await build_control_plane(settings)
    try:
        async for client in _client(runtime):
            run_response = await client.post(
                "/api/v1/runs",
                json={"objective": "Stop a local PTY through Run safety"},
            )
            assert run_response.status_code == 201, run_response.text
            run = run_response.json()
            created = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={"argv": [sys.executable, "-u", "-c", _TERMINAL_SCRIPT]},
            )
            assert created.status_code == 201, created.text
            session = created.json()
            assert session["execution_status"] == "running"
            assert session["pid"] is not None
            os.kill(int(session["pid"]), 0)

            paused = await client.post(f"/api/v1/runs/{run['id']}/pause")

            assert paused.status_code == 202, paused.text
            assert paused.json()["run"]["status"] == "paused"
            terminal = await runtime.terminal_supervisor.get(str(session["id"]))
            execution = await runtime.terminal_supervisor.get_execution(str(session["id"]))
            assert terminal.status is TerminalStatus.CLOSED
            assert execution.status is ExecutionStatus.CANCELLED
            assert execution.physical_stop_confirmed_at is not None
            with pytest.raises(ProcessLookupError):
                os.kill(int(session["pid"]), 0)

            events = await client.get(f"/api/v1/runs/{run['id']}/events")
            pause_event = next(
                item
                for item in events.json()["items"]
                if item["event_type"] == "run.pause_requested"
            )
            execution_stop = pause_event["payload"]["stop_resources"]["executions"]
            assert execution_stop["confirmed_statuses"] == {session["execution_id"]: "cancelled"}
            assert execution_stop["failures"] == {}
    finally:
        await runtime.close()


def _receive_ws_message(websocket: Any, expected_type: str) -> dict[str, object]:
    for _ in range(100):
        message = websocket.receive_json()
        if message.get("type") == expected_type:
            return message
    raise AssertionError(f"did not receive websocket message type {expected_type!r}")


def _receive_ws_state(websocket: Any, **expected: object) -> dict[str, object]:
    for _ in range(100):
        message = _receive_ws_message(websocket, "state")
        session = message["session"]
        if isinstance(session, dict) and all(
            session.get(key) == value for key, value in expected.items()
        ):
            return session
    raise AssertionError(f"did not receive terminal state matching {expected!r}")


def _receive_ws_output(websocket: Any, expected: str) -> str:
    content = ""
    for _ in range(100):
        message = _receive_ws_message(websocket, "output")
        content += str(message.get("data", ""))
        if expected in content:
            return content
    raise AssertionError(f"did not receive terminal output {expected!r}; output={content!r}")


def test_terminal_websocket_takeover_io_resize_interrupt_and_release(tmp_path: Path) -> None:
    runtime = asyncio.run(_build_runtime(tmp_path))
    app = create_app(control_plane=runtime.control_plane)
    operator_headers = {"Authorization": "Bearer test-only-local-operator-token-0001"}
    try:
        with TestClient(app, headers=operator_headers) as client:
            run_response = client.post(
                "/api/v1/runs",
                json={"objective": "Exercise the local PTY"},
            )
            assert run_response.status_code == 201
            run = run_response.json()
            created = client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": [sys.executable, "-u", "-c", _TERMINAL_SCRIPT],
                    "owner": "agent",
                },
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["id"]

            websocket_path = f"/api/v1/terminals/{session_id}/ws"
            assert "test-only-local-operator-token-0001" not in websocket_path
            with client.websocket_connect(
                websocket_path,
                headers=operator_headers,
            ) as websocket:
                _receive_ws_state(websocket, owner="agent", status="open")
                _receive_ws_output(websocket, "READY")

                websocket.send_json({"type": "input", "data": "blocked\n"})
                error = _receive_ws_message(websocket, "error")
                assert error["code"] == "terminal_not_owned"

                websocket.send_json({"type": "takeover"})
                _receive_ws_state(websocket, owner="user")
                websocket.send_json({"type": "input", "data": "你好 RiftX\n"})
                assert "ECHO:你好 RiftX" in _receive_ws_output(websocket, "ECHO:你好 RiftX")

                websocket.send_json({"type": "resize", "cols": 132, "rows": 48})
                _receive_ws_state(websocket, cols=132, rows=48)
                websocket.send_json({"type": "interrupt"})
                _receive_ws_output(websocket, "INTERRUPTED")

                websocket.send_json({"type": "ping"})
                _receive_ws_message(websocket, "pong")
                websocket.send_json({"type": "release"})
                summary = _receive_ws_message(websocket, "terminal_takeover_summary")
                assert summary["summary"]["byte_count"] > 0
                assert summary["summary"]["artifact_id"]
                assert "ECHO:你好 RiftX" in summary["summary"]["summary"]
                _receive_ws_state(websocket, owner="agent")

            closed = client.delete(f"/api/v1/terminals/{session_id}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"
            assert closed.json()["transcript_artifact_id"]

            with client.websocket_connect(
                "/api/v1/terminals/missing/ws",
                headers=operator_headers,
            ) as websocket:
                error = websocket.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "resource_not_accessible"
    finally:
        asyncio.run(runtime.control_plane.close())


@pytest.mark.asyncio
async def test_artifact_registration_snapshots_and_recovers_content(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact.db"
    runtime = await _build_runtime(tmp_path, database_path=database_path)
    artifact_id = ""
    run_id = ""
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            source = Path(str(created["workspace_path"])) / "scan output.txt"
            source.write_text("original evidence\n")

            response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={
                    "source_path": str(source),
                    "name": "scan.txt",
                    "description": "Service scan output",
                },
            )
            assert response.status_code == 201
            artifact = response.json()
            artifact_id = str(artifact["id"])
            assert artifact["mime_type"] == "text/plain"
            assert artifact["size"] == len(b"original evidence\n")
            assert artifact["sha256"] == (
                "62b0c0767a0d936528c5e12e349eb572c57ff271930d84715d43ad1e4599d279"
            )
            assert artifact["content_url"] == (f"/api/v1/artifacts/{artifact_id}/content")
            assert "path" not in artifact

            source.write_text("mutated source\n")
            content = await client.get(artifact["content_url"])
            assert content.status_code == 200
            assert content.content == b"original evidence\n"
            assert content.headers["etag"] == f'"sha256:{artifact["sha256"]}"'
            assert "attachment" in content.headers["content-disposition"]

            listed = await client.get(f"/api/v1/runs/{run_id}/artifacts")
            assert listed.status_code == 200
            assert listed.json()["items"] == [artifact]
            fetched = await client.get(f"/api/v1/artifacts/{artifact_id}")
            assert fetched.json() == artifact

            events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert events.json()["items"][-1]["event_type"] == "artifact.registered"
    finally:
        await runtime.control_plane.close()

    restarted = await _build_runtime(tmp_path, database_path=database_path)
    try:
        async for client in _client(restarted.control_plane):
            content = await client.get(f"/api/v1/artifacts/{artifact_id}/content")
            assert content.status_code == 200
            assert content.content == b"original evidence\n"
            listed = await client.get(f"/api/v1/runs/{run_id}/artifacts")
            assert listed.json()["items"][0]["id"] == artifact_id
    finally:
        await restarted.control_plane.close()


@pytest.mark.asyncio
async def test_target_http_event_artifacts_are_redacted_in_rest_sse_and_reports(
    tmp_path: Path,
) -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_TARGET_HTTP_EVENT_API"
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            owner_run = await _create_run(client)
            foreign_run = await _create_run(client)
            owner_run_id = str(owner_run["id"])
            foreign_run_id = str(foreign_run["id"])
            workspace = Path(str(owner_run["workspace_path"]))

            sensitive_source = workspace / "legacy-sensitive.bin"
            sensitive_source.write_bytes(canary.encode())
            sensitive_response = await client.post(
                f"/api/v1/runs/{owner_run_id}/artifacts",
                json={
                    "source_path": str(sensitive_source),
                    "name": f"legacy-generic-{canary}.bin",
                    "mime_type": f"application/x-{canary.lower()}",
                },
            )
            assert sensitive_response.status_code == 201, sensitive_response.text
            sensitive_id = str(sensitive_response.json()["id"])

            ordinary_source = workspace / "ordinary.txt"
            ordinary_source.write_text("ordinary evidence")
            ordinary_response = await client.post(
                f"/api/v1/runs/{owner_run_id}/artifacts",
                json={"source_path": str(ordinary_source), "name": "ordinary.txt"},
            )
            assert ordinary_response.status_code == 201, ordinary_response.text
            ordinary_id = str(ordinary_response.json()["id"])

            marker_id = "artifact-authoritative-marker"
            async with runtime.control_plane.database.session_factory() as session, session.begin():
                session.add(
                    ArtifactRecord(
                        id=marker_id,
                        run_id=owner_run_id,
                        execution_id=None,
                        name="target-http-orphan-request.json",
                        path=f"/restricted/{canary}",
                        mime_type="application/json",
                        sha256="f" * 64,
                        size=42,
                        description="Immutable Target HTTP request",
                        created_at=datetime.now(tz=UTC),
                    )
                )
                session.add(
                    TargetHttpRequestRecord(
                        id="exchange-cross-run-event",
                        execution_key=f"execution:v1:{'e' * 64}",
                        run_id=foreign_run_id,
                        session_id="session-cross-run-event",
                        tool_call_id="intent-cross-run-event",
                        node_id="node-cross-run-event",
                        method="GET",
                        url=f"https://{canary}:password@target.example/?secret={canary}",
                        request_json={"authorization": canary},
                        result_json={"body_excerpt": canary},
                        request_artifact_id=sensitive_id,
                        response_artifact_id=None,
                        created_at=datetime.now(tz=UTC),
                    )
                )

            event_repository = SQLAlchemyRunEventRepository(
                runtime.control_plane.database.session_factory
            )
            await event_repository.append(
                owner_run_id,
                "artifact.registered",
                {
                    "artifact_id": marker_id,
                    "name": f"tampered-{canary}.bin",
                    "mime_type": f"application/x-{canary.lower()}",
                    "sha256": canary,
                    "size": 42,
                },
            )
            legacy_payload = {
                "execution_key": f"execution:v1:{canary}",
                "request_id": canary,
                "method": f"GET-{canary}",
                "url": f"https://{canary}:password@target.example/private?token={canary}",
                "status_code": canary,
                "runner_reason": canary,
                "reason": canary,
            }
            for event_type in (
                "target_http.request_started",
                "target_http.request_failed",
                "target_http.response_received",
                "target_http.request_cancelled",
                "target_http.future_event",
            ):
                await event_repository.append(owner_run_id, event_type, legacy_payload)

            events_response = await client.get(
                f"/api/v1/runs/{owner_run_id}/events",
                params={"limit": 1000},
            )
            sse_response = await client.get(
                f"/api/v1/runs/{owner_run_id}/events/stream",
                params={"follow": "false"},
            )
            assert events_response.status_code == sse_response.status_code == 200
            assert canary not in events_response.text
            assert canary not in sse_response.text

            events = events_response.json()["items"]
            registered = [item for item in events if item["event_type"] == "artifact.registered"]
            restricted_payload = {
                "artifact_class": "target_http_sensitive",
                "content_restricted": True,
            }
            assert sum(item["payload"] == restricted_payload for item in registered) == 2
            ordinary_event = next(
                item for item in registered if item["payload"].get("artifact_id") == ordinary_id
            )
            assert ordinary_event["payload"]["name"] == "ordinary.txt"

            sse_items = [
                json.loads(line.removeprefix("data: "))
                for line in sse_response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert [item["payload"] for item in sse_items] == [item["payload"] for item in events]

            source = await runtime.control_plane.report_service.build_source(owner_run_id)
            serialized_source = source.model_dump_json()
            assert canary not in serialized_source
            assert [item.id for item in source.artifacts] == [ordinary_id]
            restricted_report_events = [
                item
                for item in source.key_events
                if item.event_type == "artifact.registered"
                and item.payload.get("content_restricted") is True
            ]
            assert len(restricted_report_events) == 2

            listed = await client.get(f"/api/v1/runs/{owner_run_id}/artifacts")
            assert [item["id"] for item in listed.json()["items"]] == [ordinary_id]
            for restricted_id in (sensitive_id, marker_id):
                fetched = await client.get(f"/api/v1/artifacts/{restricted_id}")
                content = await client.get(f"/api/v1/artifacts/{restricted_id}/content")
                assert fetched.status_code == content.status_code == 404
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_real_target_http_metadata_routes_filter_page_and_never_reveal_secrets(
    tmp_path: Path,
) -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_TRAFFIC_REAL_API"
    request_artifact_id = f"artifact-request-{canary}"
    response_artifact_id = f"artifact-response-{canary}"
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            owner = await _create_run(client)
            other = await _create_run(client)
            owner_run_id = str(owner["id"])
            other_run_id = str(other["id"])
            now = datetime.now(tz=UTC)
            async with runtime.control_plane.database.session_factory() as session, session.begin():
                session.add_all(
                    [
                        ArtifactRecord(
                            id=request_artifact_id,
                            run_id=owner_run_id,
                            execution_id=None,
                            name=f"request-{canary}.json",
                            path=f"/restricted/{canary}/request.json",
                            mime_type="application/json",
                            sha256="a" * 64,
                            size=100,
                            description=canary,
                            created_at=now,
                        ),
                        ArtifactRecord(
                            id=response_artifact_id,
                            run_id=owner_run_id,
                            execution_id=None,
                            name=f"response-{canary}.bin",
                            path=f"/restricted/{canary}/response.bin",
                            mime_type="application/octet-stream",
                            sha256="b" * 64,
                            size=200,
                            description=canary,
                            created_at=now,
                        ),
                    ]
                )

            writer = SQLAlchemyTargetHttpRequestRepository(
                runtime.control_plane.database.session_factory
            )
            seeded: list[tuple[str, str, int]] = []
            for index, (method, status_code) in enumerate(
                (("GET", 200), ("POST", 404), ("GET", 500))
            ):
                exchange_id = f"exchange-real-api-{index}"
                request = TargetHttpRequest(
                    execution_key=f"execution-key-real-api-{index}",
                    method=method,
                    url=(
                        f"https://{canary}:password@target.example/private/{canary}"
                        f"?signature={canary}#{canary}"
                    ),
                    headers={"Authorization": f"Bearer {canary}"},
                    cookies={"session": canary},
                    body=canary if method == "POST" else None,
                    proxy=f"http://{canary}.invalid",
                    client_cert_ref=canary,
                )
                submission = TargetHttpSubmission(
                    run_id=owner_run_id,
                    session_id=f"session-real-api-{index}",
                    tool_call_id=f"intent-real-api-{index}",
                    node_id=f"node-real-api-{index}",
                    request=request,
                )
                result = TargetHttpResult(
                    request_id=exchange_id,
                    execution_key=request.execution_key,
                    request_hash=request.fingerprint,
                    status_code=status_code,
                    response_headers={"set-cookie": canary},
                    elapsed_ms=index + 1,
                    content_type=f"text/plain; secret={canary}",
                    content_length=200,
                    body_excerpt=canary,
                    request_artifact_id=request_artifact_id,
                    response_artifact_id=response_artifact_id,
                    tls_summary={"verified": True, "client_certificate_used": False},
                    final_url=f"https://target.example/final?signature={canary}",
                    redirect_chain=[f"https://redirect.example/?secret={canary}"],
                    truncated=False,
                )
                await writer.create(submission, result)
                seeded.append((exchange_id, method, status_code))

            history = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"limit": 1},
            )
            assert history.status_code == 200, history.text
            history_model = TrafficExchangePage.model_validate(history.json())
            assert len(history_model.items) == 1
            assert history_model.has_more is True
            assert history_model.next_cursor is not None
            assert canary not in history.text
            assert request_artifact_id not in history.text
            assert response_artifact_id not in history.text
            assert request_artifact_id not in {
                history_model.items[0].artifacts.request.opaque_ref,
                history_model.items[0].artifacts.response.opaque_ref,
            }

            cursor = history_model.next_cursor
            assert cursor is not None
            inserted_exchange_id = "exchange-real-api-newer-than-snapshot"
            inserted_request = TargetHttpRequest(
                execution_key="execution-key-real-api-newer",
                method="PUT",
                url="https://target.example/newer",
            )
            inserted_submission = TargetHttpSubmission(
                run_id=owner_run_id,
                session_id="session-real-api-newer",
                tool_call_id="intent-real-api-newer",
                node_id="node-real-api-newer",
                request=inserted_request,
            )
            await writer.create(
                inserted_submission,
                TargetHttpResult(
                    request_id=inserted_exchange_id,
                    execution_key=inserted_request.execution_key,
                    request_hash=inserted_request.fingerprint,
                    status_code=302,
                    elapsed_ms=1,
                    content_type="application/json",
                    content_length=0,
                    final_url=inserted_request.url,
                ),
            )
            assert history_model.snapshot.created_through is not None
            async with runtime.control_plane.database.session_factory() as session, session.begin():
                await session.execute(
                    update(TargetHttpRequestRecord)
                    .where(TargetHttpRequestRecord.id == inserted_exchange_id)
                    .values(
                        created_at=history_model.snapshot.created_through + timedelta(seconds=1)
                    )
                )

            continued = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"limit": 1, "cursor": cursor},
            )
            assert continued.status_code == 200, continued.text
            assert inserted_exchange_id not in {
                item["exchange_id"] for item in continued.json()["items"]
            }
            fresh = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"limit": 1},
            )
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["items"][0]["exchange_id"] == inserted_exchange_id

            post_only = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"method": "POST"},
            )
            server_errors = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"status_class": "server_error"},
            )
            assert [item["method"] for item in post_only.json()["items"]] == ["POST"]
            assert [item["response"]["status_code"] for item in server_errors.json()["items"]] == [
                500
            ]

            detail = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges/{seeded[0][0]}"
            )
            assert detail.status_code == 200, detail.text
            TrafficExchangeDetail.model_validate(detail.json())
            assert canary not in detail.text
            assert request_artifact_id not in detail.text
            assert response_artifact_id not in detail.text

            inaccessible = []
            for run_id, exchange_id in (
                (owner_run_id, "exchange-missing"),
                (other_run_id, seeded[0][0]),
                ("run-missing", seeded[0][0]),
            ):
                response = await client.get(
                    f"/api/v1/runs/{run_id}/target-http/exchanges/{exchange_id}"
                )
                assert response.status_code == 404
                inaccessible.append(response.json())
            assert inaccessible[0] == inaccessible[1] == inaccessible[2]

            tampered_cursor = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"
            tampered = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"limit": 1, "cursor": tampered_cursor},
            )
            assert tampered.status_code == 422
            assert tampered.json()["error"]["code"] == "invalid_traffic_cursor"
            assert tampered_cursor not in tampered.text

            async with runtime.control_plane.database.session_factory() as session, session.begin():
                await session.execute(
                    delete(TargetHttpRequestRecord).where(
                        TargetHttpRequestRecord.id == seeded[0][0]
                    )
                )
            stale = await client.get(
                f"/api/v1/runs/{owner_run_id}/target-http/exchanges",
                params={"limit": 1, "cursor": cursor},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "stale_traffic_cursor"
            assert cursor not in stale.text
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_artifact_registration_rejects_escaped_and_invalid_sources(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            workspace = Path(str(created["workspace_path"]))
            outside = tmp_path / "outside.txt"
            outside.write_text("not run evidence")

            escaped = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(outside)},
            )
            assert escaped.status_code == 409
            assert escaped.json()["error"]["code"] == "artifact_source_outside_run"

            symlink = workspace / "escaped-link.txt"
            symlink.symlink_to(outside)
            linked = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(symlink)},
            )
            assert linked.status_code == 409
            assert linked.json()["error"]["code"] == "artifact_source_unavailable"

            missing = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(workspace / "missing.txt")},
            )
            assert missing.status_code == 409
            assert missing.json()["error"]["code"] == "artifact_source_unavailable"

            directory = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(workspace)},
            )
            assert directory.status_code == 409
            assert directory.json()["error"]["code"] == "artifact_source_not_file"

            source = workspace / "valid.txt"
            source.write_text("valid")
            invalid_name = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source), "name": "../escape.txt"},
            )
            assert invalid_name.status_code == 409
            assert invalid_name.json()["error"]["code"] == "invalid_artifact_name"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_artifact_download_detects_snapshot_tampering(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            source = Path(str(created["workspace_path"])) / "evidence.txt"
            source.write_text("trusted")
            response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source)},
            )
            artifact_id = str(response.json()["id"])
            artifact = await runtime.artifact_repository.get(artifact_id)
            assert artifact is not None
            snapshot = Path(artifact.path)
            await asyncio.to_thread(snapshot.chmod, 0o644)
            await asyncio.to_thread(snapshot.write_text, "tampered")

            content = await client.get(f"/api/v1/artifacts/{artifact_id}/content")
            assert content.status_code == 409
            assert content.json()["error"]["code"] == "artifact_integrity_mismatch"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_findings_are_editable_and_validate_artifact_evidence(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            first = await _create_run(client)
            first_run_id = str(first["id"])
            source = Path(str(first["workspace_path"])) / "proof.txt"
            source.write_text("service exposed")
            artifact_response = await client.post(
                f"/api/v1/runs/{first_run_id}/artifacts",
                json={"source_path": str(source), "description": "raw proof"},
            )
            artifact_id = str(artifact_response.json()["id"])

            created = await client.post(
                f"/api/v1/runs/{first_run_id}/findings",
                json={
                    "title": "  Exposed service  ",
                    "severity": "high",
                    "affected_assets": [" 127.0.0.1 ", "127.0.0.1"],
                    "description": "  Development service is reachable.  ",
                    "evidence": [
                        {
                            "artifact_id": artifact_id,
                            "description": "Banner capture",
                            "location": "line:1",
                        }
                    ],
                    "reproduction_steps": [" curl localhost ", "curl localhost"],
                },
            )
            assert created.status_code == 201
            finding = created.json()
            finding_id = str(finding["id"])
            assert finding["title"] == "Exposed service"
            assert finding["status"] == "draft"
            assert finding["affected_assets"] == ["127.0.0.1"]
            assert finding["reproduction_steps"] == ["curl localhost"]
            assert finding["evidence"][0]["artifact_id"] == artifact_id
            before_confirmation = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "asset",
                    "scope_id": f"{first['engagement_id']}::127.0.0.1",
                },
            )
            assert before_confirmation.json()["items"] == []

            updated = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={
                    "status": "confirmed",
                    "severity": "critical",
                    "impact": "Administrative metadata is exposed.",
                },
            )
            assert updated.status_code == 200
            assert updated.json()["status"] == "confirmed"
            assert updated.json()["severity"] == "critical"
            assert updated.json()["title"] == "Exposed service"
            assert updated.json()["updated_at"] > finding["updated_at"]

            same_value = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={"severity": "critical"},
            )
            assert same_value.status_code == 200
            assert same_value.json() == updated.json()

            promoted = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "asset",
                    "scope_id": f"{first['engagement_id']}::127.0.0.1",
                },
            )
            assert promoted.status_code == 200
            assert len(promoted.json()["items"]) == 1
            promoted_memory = promoted.json()["items"][0]
            assert promoted_memory["memory_type"] == "episodic"
            assert promoted_memory["title"] == "Exposed service"
            assert promoted_memory["source_refs"] == [
                f"finding://{finding_id}",
                f"artifact://{artifact_id}",
            ]

            fetched = await client.get(f"/api/v1/findings/{finding_id}")
            assert fetched.json() == updated.json()
            listed = await client.get(
                f"/api/v1/runs/{first_run_id}/findings",
                params={"status": "confirmed", "severity": "critical"},
            )
            assert [item["id"] for item in listed.json()["items"]] == [finding_id]

            second = await _create_run(client)
            second_run_id = str(second["id"])
            mismatch = await client.post(
                f"/api/v1/runs/{second_run_id}/findings",
                json={
                    "title": "Invalid evidence",
                    "severity": "low",
                    "evidence": [{"artifact_id": artifact_id}],
                },
            )
            assert mismatch.status_code == 409
            assert mismatch.json()["error"]["code"] == "finding_artifact_run_mismatch"

            missing = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={"evidence": [{"artifact_id": "missing"}]},
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "artifact_not_found"

            no_changes = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={},
            )
            assert no_changes.status_code == 409
            assert no_changes.json()["error"]["code"] == "empty_finding_update"

            empty = await client.post(
                f"/api/v1/runs/{first_run_id}/findings",
                json={
                    "title": "Empty evidence",
                    "severity": "info",
                    "evidence": [{}],
                },
            )
            assert empty.status_code == 409
            assert empty.json()["error"]["code"] == "empty_finding_evidence"

            events = await client.get(f"/api/v1/runs/{first_run_id}/events")
            assert [item["event_type"] for item in events.json()["items"]][-4:] == [
                "artifact.registered",
                "finding.created",
                "finding.updated",
                "memory.promotion_evaluated",
            ]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_finding_repository_conflict_has_stable_api_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created_run = await _create_run(client)
            created = await client.post(
                f"/api/v1/runs/{created_run['id']}/findings",
                json={"title": "Finding", "severity": "info"},
            )
            assert created.status_code == 201

            async def reject_stale_write(
                finding: Finding,
                *,
                expected_updated_at: datetime,
            ) -> tuple[Finding, bool]:
                del finding, expected_updated_at
                raise RepositoryConflictError("simulated stale writer")

            monkeypatch.setattr(runtime.finding_repository, "save", reject_stale_write)
            response = await client.patch(
                f"/api/v1/findings/{created.json()['id']}",
                json={"title": "Changed"},
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "finding_update_conflict"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_reports_generate_list_get_and_link_finding_evidence(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            run_id = str(run["id"])
            premature = await client.post(
                f"/api/v1/runs/{run_id}/reports",
                json={"formats": ["markdown"]},
            )
            assert premature.status_code == 409
            assert premature.json()["error"]["code"] == "run_not_reportable"
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.update_status(run_id, RunStatus.COMPLETED)
            source = Path(str(run["workspace_path"])) / "proof.txt"
            source.write_text("service exposed")
            artifact_response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source), "description": "Banner proof"},
            )
            assert artifact_response.status_code == 201
            evidence_artifact_id = str(artifact_response.json()["id"])
            finding_response = await client.post(
                f"/api/v1/runs/{run_id}/findings",
                json={
                    "title": "Exposed service",
                    "severity": "high",
                    "description": "A development endpoint is reachable.",
                    "evidence": [
                        {
                            "artifact_id": evidence_artifact_id,
                            "description": "Open banner proof",
                        }
                    ],
                },
            )
            assert finding_response.status_code == 201
            finding_id = str(finding_response.json()["id"])

            generated = await client.post(
                f"/api/v1/runs/{run_id}/reports",
                json={"formats": ["markdown", "html", "json"]},
            )
            assert generated.status_code == 201, generated.text
            reports = generated.json()["items"]
            assert [item["format"] for item in reports] == ["markdown", "html", "json"]
            assert all(item["finding_ids"] == [finding_id] for item in reports)
            assert all(item["content_url"].startswith("/api/v1/artifacts/") for item in reports)

            listed = await client.get(f"/api/v1/runs/{run_id}/reports")
            assert {item["id"] for item in listed.json()["items"]} == {
                item["id"] for item in reports
            }
            markdown_report = reports[0]
            fetched = await client.get(f"/api/v1/reports/{markdown_report['id']}")
            assert fetched.json() == markdown_report
            content = await client.get(markdown_report["content_url"])
            assert content.status_code == 200
            assert f"/api/v1/artifacts/{evidence_artifact_id}/content" in content.text

            events = await client.get(f"/api/v1/runs/{run_id}/events")
            event_types = [item["event_type"] for item in events.json()["items"]]
            assert event_types.count("report.generated") == 3
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_runner_registration_heartbeat_and_node_management(tmp_path: Path) -> None:
    runtime = await _build_runtime(
        tmp_path,
        admin_token="test-only-admin-operator-token-0002",
    )
    async for client in _client(runtime.control_plane):
        missing_auth = await client.post(
            "/api/v1/nodes/register",
            json={
                "node_id": "missing-auth",
                "name": "Missing Auth",
                "platform": "linux",
                "architecture": "x86_64",
            },
        )
        assert missing_auth.status_code == 401

        denied = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer wrong"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner A",
                "platform": "windows",
                "architecture": "amd64",
            },
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "runner_registration_denied"

        registered = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner A",
                "platform": "windows",
                "architecture": "amd64",
                "runner_version": "2.0.0",
                "capabilities": [
                    "powershell",
                    "conpty",
                    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
                ],
                "labels": {"zone": "internal"},
            },
        )
        assert registered.status_code == 200
        assert registered.json()["created"] is True
        assert registered.json()["node"]["status"] == "online"
        first_token = registered.json()["runner_token"]
        first_principal = registered.json()["principal"]
        assert first_principal["epoch"] == 1
        assert first_principal["instance_id"]

        repeated = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner Primary",
                "platform": "windows",
                "architecture": "amd64",
                "runner_version": "2.0.1",
                "capabilities": ["powershell", RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
        runner_token = repeated.json()["runner_token"]
        runner_principal = repeated.json()["principal"]
        assert runner_token != first_token
        assert runner_principal["epoch"] == 2
        assert runner_principal["instance_id"] != first_principal["instance_id"]

        superseded_owner = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers={
                "Authorization": f"Bearer {first_token}",
                "X-RiftX-Runner-Instance-ID": first_principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(first_principal["epoch"]),
            },
            json={"status": "degraded", "labels": {"stale": "ignored"}},
        )
        # Superseded owners retain a fenced identity so they can acknowledge
        # safety commands for effects they started. Their heartbeat cannot
        # refresh the current owner's liveness state.
        assert superseded_owner.status_code == 200
        assert superseded_owner.json()["status"] == "online"
        assert superseded_owner.json()["labels"] == {}

        runner_headers = {
            "Authorization": f"Bearer {runner_token}",
            "X-RiftX-Node-ID": "windows-a",
            "X-RiftX-Runner-Instance-ID": runner_principal["instance_id"],
            "X-RiftX-Runner-Epoch": str(runner_principal["epoch"]),
        }
        mixed_identity = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers={
                "Authorization": f"Bearer {runner_token}",
                "X-RiftX-Runner-Instance-ID": first_principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(first_principal["epoch"]),
            },
            json={},
        )
        assert mixed_identity.status_code == 401
        assert mixed_identity.json()["error"]["code"] == "runner_authentication_failed"
        unauthenticated_heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers={
                "X-RiftX-Runner-Instance-ID": runner_principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(runner_principal["epoch"]),
            },
            json={},
        )
        assert unauthenticated_heartbeat.status_code == 401

        invalid_heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers=runner_headers,
            json={"status": "offline"},
        )
        assert invalid_heartbeat.status_code == 422
        assert invalid_heartbeat.json()["error"]["code"] == "validation_error"

        heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers=runner_headers,
            json={"status": "degraded", "labels": {"load": "high"}},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["status"] == "degraded"
        assert heartbeat.json()["labels"] == {"load": "high"}

        listed = await client.get("/api/v1/nodes?status=degraded")
        assert listed.status_code == 200
        assert [node["id"] for node in listed.json()["items"]] == ["windows-a"]

        denied_disconnect = await client.post(
            "/api/v1/nodes/windows-a/disconnect",
            headers={"Authorization": ""},
        )
        assert denied_disconnect.status_code == 401
        disconnected = await client.post(
            "/api/v1/nodes/windows-a/disconnect",
            headers={"Authorization": "Bearer test-only-admin-operator-token-0002"},
        )
        assert disconnected.status_code == 200
        assert disconnected.json()["status"] == "offline"

        missing = await client.get("/api/v1/nodes/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "node_not_found"
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_every_runner_callback_rejects_invalid_credentials_with_structured_401(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": "Bearer wrong-bootstrap"},
                json={
                    "node_id": "runner-auth",
                    "name": "Runner Auth",
                    "platform": "linux",
                    "architecture": "x86_64",
                },
            )
            assert registration.status_code == 401
            assert registration.json()["error"] == {
                "code": "runner_registration_denied",
                "message": "Runner registration token is invalid",
                "details": {},
            }

            headers = {
                "Authorization": "Bearer wrong-runner-token",
                "X-RiftX-Node-ID": "runner-auth",
                "X-RiftX-Runner-Instance-ID": "runner-instance-auth",
                "X-RiftX-Runner-Epoch": "1",
            }
            responses = [
                await client.post(
                    "/api/v1/nodes/runner-auth/heartbeat",
                    headers={
                        "Authorization": "Bearer wrong-runner-token",
                        "X-RiftX-Runner-Instance-ID": "runner-instance-auth",
                        "X-RiftX-Runner-Epoch": "1",
                    },
                    json={},
                ),
                await client.get(
                    "/api/v1/runner/commands/next",
                    headers=headers,
                ),
                await client.post(
                    "/api/v1/runner/commands/command-auth/finish-owned",
                    headers=headers,
                    json={
                        "lease_id": "lease-auth",
                        "state_version": 0,
                        "envelope_digest": "a" * 64,
                        "binding_digest": "b" * 64,
                        "succeeded": True,
                    },
                ),
                await client.post(
                    "/api/v1/runner/commands/command-auth/lease",
                    headers=headers,
                    json={"lease_id": "lease-auth"},
                ),
                await client.post(
                    "/api/v1/runner/commands/command-auth/output",
                    headers=headers,
                    json={"lease_id": "lease-auth", "offset": 0, "data": ""},
                ),
                await client.post(
                    "/api/v1/runner/executions/execution-auth/status",
                    headers=headers,
                    json={"status": "running"},
                ),
                await client.post(
                    "/api/v1/runner/executions/execution-auth/output",
                    headers=headers,
                    json={"stream": "stdout", "offset": 0, "data": ""},
                ),
            ]

            assert len(responses) == 7
            for response in responses:
                assert response.status_code == 401, response.text
                assert response.json()["error"] == {
                    "code": "runner_authentication_failed",
                    "message": "Runner credentials are missing or invalid",
                    "details": {},
                }
    finally:
        await runtime.control_plane.close()


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [
        ("execution_key", "another-execution-key"),
        ("owner", {"instance_id": "another-runner-instance", "epoch": 99}),
        ("status", "exited"),
        ("physical_stop_confirmed", False),
    ],
)
@pytest.mark.asyncio
async def test_runner_cancel_ack_requires_exact_execution_owner_and_physical_stop(
    tmp_path: Path,
    invalid_field: str,
    invalid_value: object,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
                json={
                    "node_id": "runner-cancel-ack",
                    "name": "Runner Cancel ACK",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
                },
            )
            assert registration.status_code == 200
            registered = registration.json()
            principal = registered["principal"]
            headers = {
                "Authorization": f"Bearer {registered['runner_token']}",
                "X-RiftX-Node-ID": "runner-cancel-ack",
                "X-RiftX-Runner-Instance-ID": principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(principal["epoch"]),
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Verify durable cancellation acknowledgement",
                    "node_id": "runner-cancel-ack",
                    "engagement": {"name": "Runner cancellation acknowledgement"},
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            execution_paths = RunnerPaths(
                runtime.control_plane.settings.runner_state_path
            ).execution(str(run["id"]), "execution-cancel-ack")
            execution = Execution(
                id="execution-cancel-ack",
                execution_key="execution-key-cancel-ack",
                run_id=str(run["id"]),
                node_id="runner-cancel-ack",
                owner=RunnerPrincipal.model_validate(principal),
                executor_type=ExecutorType.PROCESS,
                argv=["sleep", "30"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(execution_paths.stdout),
                stderr_path=str(execution_paths.stderr),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            await runtime.execution_repository.create_if_absent(execution)
            command, created = await runtime.control_plane.runner_control_service.enqueue(
                "runner-cancel-ack",
                kind=RunnerCommandKind.CANCEL,
                idempotency_key=f"cancel-ack:{invalid_field}",
                run_id=str(run["id"]),
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.EXECUTION,
                resource_id=execution.id,
                execution_id=execution.id,
                output_contract=RunnerOutputContract(
                    result_schema="riftx.runner-result/execution-stop/v1",
                    stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
                ),
                payload={
                    "execution_id": "execution-cancel-ack",
                    "execution_key": "execution-key-cancel-ack",
                },
            )
            assert created is True
            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            assert leased.status_code == 200
            leased_command = leased.json()["command"]
            assert leased_command["id"] == command.id
            assert leased_command["target"] == principal

            valid_ack: dict[str, object] = {
                "execution_id": "execution-cancel-ack",
                "local_execution_id": "execution-cancel-ack",
                "execution_key": "execution-key-cancel-ack",
                "owner": principal,
                "status": "cancelled",
                "physical_stop_confirmed": True,
            }
            wrong_lease = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": "wrong-lease",
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": valid_ack,
                },
            )
            assert wrong_lease.status_code == 409
            before_ack = await runtime.execution_repository.get(execution.id)
            assert before_ack is not None
            assert before_ack.status is ExecutionStatus.RUNNING
            assert before_ack.physical_stop_confirmed_at is None

            invalid_ack = {**valid_ack, invalid_field: invalid_value}
            rejected = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": leased_command["lease_id"],
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": invalid_ack,
                },
            )

            assert rejected.status_code == 409
            assert rejected.json()["error"] == {
                "code": "runner_stop_ack_invalid",
                "message": "Runner stop acknowledgement did not prove the owning resource stopped",
                "details": {
                    "command_id": command.id,
                    "invalid_fields": [invalid_field],
                },
            }

            accepted = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": leased_command["lease_id"],
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": valid_ack,
                },
            )
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "completed"
            persisted_execution = await runtime.execution_repository.get(execution.id)
            assert persisted_execution is not None
            assert persisted_execution.status is ExecutionStatus.CANCELLED
            assert persisted_execution.physical_stop_confirmed_at is not None
    finally:
        await runtime.control_plane.close()


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.HARD_TIMEOUT,
    ],
)
@pytest.mark.asyncio
async def test_runner_cancel_ack_preserves_confirmed_natural_execution_outcome(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
                json={
                    "node_id": "runner-natural-ack",
                    "name": "Runner Natural Outcome ACK",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
                },
            )
            assert registration.status_code == 200
            registered = registration.json()
            principal = registered["principal"]
            headers = {
                "Authorization": f"Bearer {registered['runner_token']}",
                "X-RiftX-Node-ID": "runner-natural-ack",
                "X-RiftX-Runner-Instance-ID": principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(principal["epoch"]),
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Preserve a natural terminal outcome",
                    "node_id": "runner-natural-ack",
                    "engagement": {"name": "Natural outcome cancellation ACK"},
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            execution_id = f"execution-natural-{status.value}"
            execution_key = f"natural-{status.value}-key"
            execution_paths = RunnerPaths(
                runtime.control_plane.settings.runner_state_path
            ).execution(str(run["id"]), execution_id)
            execution = Execution(
                id=execution_id,
                execution_key=execution_key,
                run_id=str(run["id"]),
                node_id="runner-natural-ack",
                owner=RunnerPrincipal.model_validate(principal),
                executor_type=ExecutorType.PROCESS,
                argv=["true"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(execution_paths.stdout),
                stderr_path=str(execution_paths.stderr),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            execution.transition_to(status)
            await runtime.execution_repository.create_if_absent(execution)
            command, _ = await runtime.control_plane.runner_control_service.enqueue(
                "runner-natural-ack",
                kind=RunnerCommandKind.CANCEL,
                idempotency_key=f"cancel-natural:{status.value}",
                run_id=str(run["id"]),
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.EXECUTION,
                resource_id=execution.id,
                execution_id=execution.id,
                output_contract=RunnerOutputContract(
                    result_schema="riftx.runner-result/execution-stop/v1",
                    stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
                ),
                payload={
                    "execution_id": execution_id,
                    "execution_key": execution_key,
                },
            )
            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            leased_command = leased.json()["command"]
            assert leased_command["id"] == command.id
            acknowledged = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": leased_command["lease_id"],
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": {
                        "execution_id": execution_id,
                        "local_execution_id": execution_id,
                        "execution_key": execution_key,
                        "owner": principal,
                        "status": "cancelled",
                        "physical_stop_confirmed": True,
                    },
                },
            )
            assert acknowledged.status_code == 200
            persisted = await runtime.execution_repository.get(execution_id)
            assert persisted is not None
            assert persisted.status is status
            assert persisted.physical_stop_confirmed_at is not None
    finally:
        await runtime.control_plane.close()


@pytest.mark.parametrize(
    ("executor_type", "natural_status"),
    [
        (ExecutorType.PROCESS, ExecutionStatus.EXITED),
        (ExecutorType.PTY, ExecutionStatus.COMPLETED),
    ],
)
@pytest.mark.asyncio
async def test_starting_execution_accepts_natural_stop_proof_and_cancel_ack(
    tmp_path: Path,
    executor_type: ExecutorType,
    natural_status: ExecutionStatus,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
                json={
                    "node_id": "runner-starting-natural",
                    "name": "Runner Starting Natural Stop",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
                },
            )
            assert registration.status_code == 200
            registered = registration.json()
            principal = registered["principal"]
            headers = {
                "Authorization": f"Bearer {registered['runner_token']}",
                "X-RiftX-Node-ID": "runner-starting-natural",
                "X-RiftX-Runner-Instance-ID": principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(principal["epoch"]),
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Accept a natural stop before RUNNING commits",
                    "node_id": "runner-starting-natural",
                    "engagement": {"name": "Starting natural stop"},
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            suffix = executor_type.value
            execution_id = f"execution-starting-natural-{suffix}"
            execution_key = f"starting-natural-{suffix}-key"
            execution_paths = RunnerPaths(
                runtime.control_plane.settings.runner_state_path
            ).execution(str(run["id"]), execution_id)
            execution = Execution(
                id=execution_id,
                execution_key=execution_key,
                run_id=str(run["id"]),
                node_id="runner-starting-natural",
                owner=RunnerPrincipal.model_validate(principal),
                executor_type=executor_type,
                argv=["true"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(execution_paths.stdout),
                stderr_path=str(execution_paths.stderr),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            await runtime.execution_repository.create_if_absent(execution)
            terminal_id = f"terminal-starting-natural-{suffix}"
            if executor_type is ExecutorType.PTY:
                await runtime.terminal_repository.create(
                    TerminalSession(
                        id=terminal_id,
                        run_id=str(run["id"]),
                        execution_id=execution.id,
                    )
                )
            launch_kind = (
                RunnerCommandKind.TERMINAL_START
                if executor_type is ExecutorType.PTY
                else RunnerCommandKind.EXECUTE
            )
            launch_request: dict[str, object] = {
                "run_id": str(run["id"]),
                "node_id": "runner-starting-natural",
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
                "runner_principal": principal,
            }
            launch_payload: dict[str, object] = {
                "execution_id": execution.id,
                "request": launch_request,
            }
            if executor_type is ExecutorType.PTY:
                launch_payload["session_id"] = terminal_id
                launch_request["session_id"] = terminal_id
            launch_command, _ = await runtime.control_plane.runner_control_service.enqueue(
                "runner-starting-natural",
                kind=launch_kind,
                idempotency_key=f"launch-starting-natural:{suffix}",
                run_id=str(run["id"]),
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=(
                    RunnerOperationFamily.TERMINAL
                    if executor_type is ExecutorType.PTY
                    else RunnerOperationFamily.EXECUTION
                ),
                resource_kind=(
                    RunnerResourceKind.TERMINAL_SESSION
                    if executor_type is ExecutorType.PTY
                    else RunnerResourceKind.EXECUTION
                ),
                resource_id=terminal_id if executor_type is ExecutorType.PTY else execution.id,
                execution_id=execution.id,
                output_contract=RunnerOutputContract(
                    max_output_bytes=100_000_000,
                    allowed_streams=("stderr", "stdout"),
                    result_schema=(
                        "riftx.runner-result/terminal-start/v1"
                        if executor_type is ExecutorType.PTY
                        else "riftx.runner-result/execution-start/v1"
                    ),
                ),
                payload=launch_payload,
            )
            leased_launch = await _lease_and_finish_runner_command(
                client,
                headers,
                launch_command.id,
            )
            natural_report = {
                **_runner_execution_callback_fields(leased_launch),
                "status": natural_status.value,
                "exit_code": 0,
                "physical_stop_confirmed": True,
            }

            stopped = await client.post(
                f"/api/v1/runner/executions/{execution.id}/status",
                headers=headers,
                json=natural_report,
            )

            assert stopped.status_code == 200
            assert stopped.json()["status"] == "cancelled"
            assert stopped.json()["exit_code"] == 0
            assert stopped.json()["physical_stop_confirmed_at"] is not None
            if executor_type is ExecutorType.PTY:
                terminal = await runtime.terminal_repository.get_by_execution(execution.id)
                assert terminal is not None and terminal.status is TerminalStatus.CLOSED
            command, _ = await runtime.control_plane.runner_control_service.enqueue(
                "runner-starting-natural",
                kind=RunnerCommandKind.CANCEL,
                idempotency_key=f"cancel-starting-natural:{suffix}",
                run_id=str(run["id"]),
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.EXECUTION,
                resource_id=execution.id,
                execution_id=execution.id,
                output_contract=RunnerOutputContract(
                    result_schema="riftx.runner-result/execution-stop/v1",
                    stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
                ),
                payload={
                    "execution_id": execution.id,
                    "execution_key": execution.execution_key,
                },
            )
            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            leased_command = leased.json()["command"]
            assert leased_command["id"] == command.id
            repeated = await client.post(
                f"/api/v1/runner/executions/{execution.id}/status",
                headers=headers,
                json=natural_report,
            )
            assert repeated.status_code == 200
            assert repeated.json()["status"] == "cancelled"
            acknowledged = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": leased_command["lease_id"],
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": {
                        "execution_id": execution.id,
                        "local_execution_id": execution.id,
                        "execution_key": execution.execution_key,
                        "owner": principal,
                        "status": "cancelled",
                        "physical_stop_confirmed": True,
                    },
                },
            )
            assert acknowledged.status_code == 200
            persisted = await runtime.execution_repository.get(execution.id)
            assert persisted is not None
            assert persisted.status is ExecutionStatus.CANCELLED
            assert persisted.physical_stop_confirmed_at is not None
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_node_api_exposes_runtime_and_active_execution_state(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        await runtime.control_plane.node_service.register(
            NodeRegistration(
                node_id="local",
                name="Local Runner",
                platform="linux",
                architecture="x86_64",
                runner_version="2.0.0",
                capabilities=("scripting",),
                labels={
                    "shell": "/bin/zsh",
                    "working_directory": str(tmp_path),
                    "tool_count": "99",
                },
            )
        )
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            execution = Execution(
                id="execution-active",
                execution_key="active-key",
                run_id=str(run["id"]),
                node_id="local",
                executor_type=ExecutorType.PROCESS,
                argv=["python", "-V"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(tmp_path / "stdout.log"),
                stderr_path=str(tmp_path / "stderr.log"),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            await runtime.execution_repository.create_if_absent(execution)

            fetched = await client.get("/api/v1/nodes/local")
            listed = await client.get("/api/v1/nodes")

            assert fetched.status_code == 200
            node = fetched.json()
            assert node["platform"] == "linux"
            assert node["architecture"] == "x86_64"
            assert node["shell"] == "/bin/zsh"
            assert node["working_directory"] == str(tmp_path)
            assert node["tool_count"] == 1
            assert node["active_execution_ids"] == [execution.id]
            assert node["current_run_ids"] == [execution.run_id]
            assert listed.json()["items"] == [node]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_runner_control_channel_reconnects_and_bounds_updates(
    tmp_path: Path,
) -> None:
    # Keep this wall-clock API check comfortably above scheduler jitter from
    # concurrently running process/PTY tests. Repository-level lease tests use
    # an injected clock for exact boundary assertions.
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=0.5)
    async for client in _client(runtime.control_plane):
        registration = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
            json={
                "node_id": "runner-a",
                "name": "Runner A",
                "platform": "linux",
                "architecture": "x86_64",
                "capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
            },
        )
        token = registration.json()["runner_token"]
        principal = registration.json()["principal"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-RiftX-Node-ID": "runner-a",
            "X-RiftX-Runner-Instance-ID": principal["instance_id"],
            "X-RiftX-Runner-Epoch": str(principal["epoch"]),
        }
        other_registration = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
            json={
                "node_id": "runner-b",
                "name": "Runner B",
                "platform": "linux",
                "architecture": "x86_64",
                "capabilities": [RUNNER_COMMAND_OWNERSHIP_CAPABILITY],
            },
        )
        other_headers = {
            "Authorization": f"Bearer {other_registration.json()['runner_token']}",
            "X-RiftX-Node-ID": "runner-b",
            "X-RiftX-Runner-Instance-ID": other_registration.json()["principal"]["instance_id"],
            "X-RiftX-Runner-Epoch": str(other_registration.json()["principal"]["epoch"]),
        }
        run_response = await client.post(
            "/api/v1/runs",
            json={
                "objective": "Remote execution channel test",
                "node_id": "runner-a",
                "engagement": {"name": "Authorized remote test"},
            },
        )
        assert run_response.status_code == 201
        run = run_response.json()
        paths = RunnerPaths(runtime.control_plane.settings.runner_state_path)
        output_paths = paths.execution(str(run["id"]), "execution-remote")
        execution = Execution(
            id="execution-remote",
            execution_key="remote-key",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PROCESS,
            argv=["echo", "hello"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(output_paths.stdout),
            stderr_path=str(output_paths.stderr),
            owner=RunnerPrincipal.model_validate(principal),
        )
        execution.transition_to(ExecutionStatus.STARTING)
        _, created = await runtime.execution_repository.create_if_absent(execution)
        assert created is True

        launch_payload: dict[str, object] = {
            "execution_id": execution.id,
            "request": {
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
                "runner_principal": principal,
                "argv": execution.argv,
                "cwd": execution.cwd,
            },
        }
        command, command_created = await runtime.control_plane.runner_control_service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.EXECUTE,
            idempotency_key="execute:remote-key",
            run_id=str(run["id"]),
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.EXECUTION,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution.id,
            execution_id=execution.id,
            output_contract=RunnerOutputContract(
                max_output_bytes=100_000_000,
                allowed_streams=("stderr", "stdout"),
                result_schema="riftx.runner-result/execution-start/v1",
            ),
            payload=launch_payload,
        )
        repeated, repeated_created = await runtime.control_plane.runner_control_service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.EXECUTE,
            idempotency_key="execute:remote-key",
            run_id=str(run["id"]),
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.EXECUTION,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=execution.id,
            execution_id=execution.id,
            output_contract=RunnerOutputContract(
                max_output_bytes=100_000_000,
                allowed_streams=("stderr", "stdout"),
                result_schema="riftx.runner-result/execution-start/v1",
            ),
            payload=launch_payload,
        )
        assert command_created is True
        assert repeated_created is False
        assert repeated.id == command.id

        leased = await client.get("/api/v1/runner/commands/next", headers=headers)
        leased_command = leased.json()["command"]
        assert leased_command["id"] == command.id
        assert leased_command["kind"] == "execute"
        assert leased_command["target"] == principal
        first_lease = leased_command["lease_id"]
        execution_callback_fields = _runner_execution_callback_fields(leased_command)

        renewed = await client.post(
            f"/api/v1/runner/commands/{command.id}/lease",
            headers=headers,
            json={
                "lease_id": first_lease,
                **_runner_command_callback_fields(leased_command),
            },
        )
        assert renewed.status_code == 200
        renewed_payload = renewed.json()
        assert renewed_payload["id"] == command.id
        assert datetime.fromisoformat(renewed_payload["lease_expires_at"]) > datetime.fromisoformat(
            leased_command["lease_expires_at"]
        )

        cross_node_renewal = await client.post(
            f"/api/v1/runner/commands/{command.id}/lease",
            headers=other_headers,
            json={
                "lease_id": first_lease,
                **_runner_command_callback_fields(leased_command),
            },
        )
        assert cross_node_renewal.status_code == 401

        still_leased = await client.get("/api/v1/runner/commands/next", headers=headers)
        assert still_leased.json()["command"] is None

        await asyncio.sleep(renewed_payload["lease_duration_seconds"] + 0.05)
        re_leased = await client.get("/api/v1/runner/commands/next", headers=headers)
        re_leased_command = re_leased.json()["command"]
        assert re_leased_command["id"] == command.id
        assert re_leased_command["attempts"] == 2
        assert re_leased_command["lease_id"] != first_lease

        stale_renewal = await client.post(
            f"/api/v1/runner/commands/{command.id}/lease",
            headers=headers,
            json={
                "lease_id": first_lease,
                **_runner_command_callback_fields(leased_command),
            },
        )
        assert stale_renewal.status_code == 409

        stale_finish = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish-owned",
            headers=headers,
            json={
                "lease_id": first_lease,
                **_runner_command_callback_fields(leased_command),
                "succeeded": True,
            },
        )
        assert stale_finish.status_code == 409

        oversized_result = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish-owned",
            headers=headers,
            json={
                "lease_id": re_leased_command["lease_id"],
                **_runner_command_callback_fields(re_leased_command),
                "succeeded": True,
                "result": {"value": "x" * (64 * 1024)},
            },
        )
        assert oversized_result.status_code == 409
        assert oversized_result.json()["error"]["code"] == "runner_result_too_large"

        finished = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish-owned",
            headers=headers,
            json={
                "lease_id": re_leased_command["lease_id"],
                **_runner_command_callback_fields(re_leased_command),
                "succeeded": True,
                "result": {
                    "execution_id": execution.id,
                    "local_execution_id": execution.id,
                    "execution_key": execution.execution_key,
                    "owner": principal,
                    "status": "running",
                },
            },
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "completed"

        running = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=headers,
            json={
                **execution_callback_fields,
                "status": "running",
                "pid": 4242,
                "process_group_id": 4242,
                "executable_path": "/usr/bin/echo",
                "tool_id": "echo",
                "tool_version": "9.1",
                "platform_system": "linux",
                "platform_release": "6.10",
                "platform_architecture": "x86_64",
                "process_created_at": "2026-07-30T00:00:00Z",
            },
        )
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert running.json()["executable_path"] == "/usr/bin/echo"
        assert running.json()["tool_version"] == "9.1"
        assert running.json()["platform_architecture"] == "x86_64"

        for invalid_status in ("running", "failed", "lost"):
            invalid_proof = await client.post(
                f"/api/v1/runner/executions/{execution.id}/status",
                headers=headers,
                json={
                    **execution_callback_fields,
                    "status": invalid_status,
                    "physical_stop_confirmed": True,
                },
            )
            assert invalid_proof.status_code == 409
            assert invalid_proof.json()["error"] == {
                "code": "runner_execution_stop_proof_invalid",
                "message": "Runner cannot attach physical-stop proof to this execution status",
                "details": {"status": invalid_status},
            }

        cross_node_status = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=other_headers,
            json={**execution_callback_fields, "status": "running", "pid": 4243},
        )
        assert cross_node_status.status_code == 401
        assert cross_node_status.json()["error"]["code"] == ("runner_execution_scope_mismatch")

        output = await client.post(
            f"/api/v1/runner/executions/{execution.id}/output",
            headers=headers,
            json={
                **execution_callback_fields,
                "stream": "stdout",
                "offset": 0,
                "data": "aGVsbG8K",
            },
        )
        assert output.status_code == 200
        assert output.json()["next_offset"] == 6
        assert output_paths.stdout.read_bytes() == b"hello\n"

        replay = await client.post(
            f"/api/v1/runner/executions/{execution.id}/output",
            headers=headers,
            json={
                **execution_callback_fields,
                "stream": "stdout",
                "offset": 0,
                "data": "aGVsbG8K",
            },
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "runner_output_offset_mismatch"

        missing_stop_proof = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=headers,
            json={**execution_callback_fields, "status": "exited", "exit_code": 0},
        )
        assert missing_stop_proof.status_code == 409
        assert missing_stop_proof.json()["error"] == {
            "code": "runner_execution_stop_proof_required",
            "message": "Runner stopped-status reports require affirmative physical-stop proof",
            "details": {"status": "exited"},
        }

        exited = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=headers,
            json={
                **execution_callback_fields,
                "status": "exited",
                "exit_code": 0,
                "physical_stop_confirmed": True,
            },
        )
        assert exited.status_code == 200
        assert exited.json()["exit_code"] == 0
        assert exited.json()["physical_stop_confirmed_at"] is not None

        legacy_paths = paths.execution(str(run["id"]), "execution-legacy-terminal")
        legacy_terminal_execution = Execution(
            id="execution-legacy-terminal",
            execution_key="legacy-terminal-key",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PROCESS,
            argv=["legacy-tool"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(legacy_paths.stdout),
            stderr_path=str(legacy_paths.stderr),
            executable_path="/usr/bin/legacy-tool",
            owner=RunnerPrincipal.model_validate(principal),
        )
        legacy_terminal_execution.transition_to(ExecutionStatus.STARTING)
        legacy_terminal_execution.transition_to(ExecutionStatus.RUNNING)
        legacy_terminal_execution.transition_to(ExecutionStatus.COMPLETED)
        await runtime.execution_repository.create_if_absent(legacy_terminal_execution)
        legacy_terminal_reconciled = await client.post(
            f"/api/v1/runner/executions/{legacy_terminal_execution.id}/status",
            headers=headers,
            json={
                **execution_callback_fields,
                "status": "exited",
                "exit_code": 0,
                "physical_stop_confirmed": True,
                "executable_path": "/tmp/untrusted-legacy-replacement",
                "tool_version": "1.0",
            },
        )
        assert legacy_terminal_reconciled.status_code == 409
        assert legacy_terminal_reconciled.json()["error"]["code"] == (
            "runner_execution_callback_binding_missing"
        )
        persisted_legacy = await runtime.execution_repository.get(
            legacy_terminal_execution.id
        )
        assert persisted_legacy is not None
        assert persisted_legacy.status is ExecutionStatus.COMPLETED
        assert persisted_legacy.physical_stop_confirmed_at is None
        assert persisted_legacy.exit_code is None
        assert persisted_legacy.executable_path == "/usr/bin/legacy-tool"
        assert persisted_legacy.tool_version is None

        terminal_paths = paths.terminal(str(run["id"]), "terminal-remote")
        terminal_execution = Execution(
            id="execution-terminal-remote",
            execution_key="terminal:terminal-remote",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PTY,
            argv=["pwsh.exe"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
            owner=RunnerPrincipal.model_validate(principal),
        )
        terminal_execution.transition_to(ExecutionStatus.STARTING)
        await runtime.execution_repository.create_if_absent(terminal_execution)
        await runtime.terminal_repository.create(
            TerminalSession(
                id="terminal-remote",
                run_id=str(run["id"]),
                execution_id=terminal_execution.id,
            )
        )
        terminal_launch = await _bind_test_terminal_launch(
            runtime,
            client,
            headers,
            run_id=str(run["id"]),
            node_id="runner-a",
            terminal_id="terminal-remote",
            execution_id=terminal_execution.id,
        )
        terminal_callback_fields = _runner_execution_callback_fields(terminal_launch)
        terminal_running = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/status",
            headers=headers,
            json={
                **terminal_callback_fields,
                "status": "running",
                "pid": 5150,
                "process_group_id": 5150,
            },
        )
        assert terminal_running.status_code == 200
        persisted_terminal = await runtime.terminal_repository.get("terminal-remote")
        assert persisted_terminal is not None
        assert persisted_terminal.status is TerminalStatus.OPEN

        terminal_output = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/output",
            headers=headers,
            json={
                **terminal_callback_fields,
                "stream": "stdout",
                "offset": 0,
                "data": "UkVBRFkK",
            },
        )
        assert terminal_output.status_code == 200
        assert terminal_paths.transcript.read_bytes() == b"READY\n"

        terminal_exited = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/status",
            headers=headers,
            json={
                **terminal_callback_fields,
                "status": "exited",
                "exit_code": 0,
                "physical_stop_confirmed": True,
            },
        )
        assert terminal_exited.status_code == 200
        assert terminal_exited.json()["physical_stop_confirmed_at"] is not None
        persisted_terminal = await runtime.terminal_repository.get("terminal-remote")
        assert persisted_terminal is not None
        assert persisted_terminal.status is TerminalStatus.CLOSED

        failed_paths = paths.terminal(str(run["id"]), "terminal-failed-projection")
        failed_execution = Execution(
            id="execution-terminal-failed-projection",
            execution_key="terminal:terminal-failed-projection",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PTY,
            argv=["pwsh.exe"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(failed_paths.transcript),
            stderr_path=str(failed_paths.transcript),
            owner=RunnerPrincipal.model_validate(principal),
        )
        failed_execution.transition_to(ExecutionStatus.STARTING)
        await runtime.execution_repository.create_if_absent(failed_execution)
        await runtime.terminal_repository.create(
            TerminalSession(
                id="terminal-failed-projection",
                run_id=str(run["id"]),
                execution_id=failed_execution.id,
            )
        )
        failed_launch = await _bind_test_terminal_launch(
            runtime,
            client,
            headers,
            run_id=str(run["id"]),
            node_id="runner-a",
            terminal_id="terminal-failed-projection",
            execution_id=failed_execution.id,
        )
        failed_callback_fields = _runner_execution_callback_fields(failed_launch)
        assert (
            await client.post(
                f"/api/v1/runner/executions/{failed_execution.id}/status",
                headers=headers,
                json={
                    **failed_callback_fields,
                    "status": "running",
                    "pid": 5250,
                    "process_group_id": 5250,
                },
            )
        ).status_code == 200
        failed = await client.post(
            f"/api/v1/runner/executions/{failed_execution.id}/status",
            headers=headers,
            json={**failed_callback_fields, "status": "failed", "exit_code": 1},
        )
        assert failed.status_code == 200
        assert failed.json()["physical_stop_confirmed_at"] is None
        failed_terminal = await runtime.terminal_repository.get("terminal-failed-projection")
        assert failed_terminal is not None
        assert failed_terminal.status is TerminalStatus.OPEN
        failed_reconciled = await client.post(
            f"/api/v1/runner/executions/{failed_execution.id}/status",
            headers=headers,
            json={
                **failed_callback_fields,
                "status": "completed",
                "physical_stop_confirmed": True,
            },
        )
        assert failed_reconciled.status_code == 200
        assert failed_reconciled.json()["status"] == "cancelled"
        assert failed_reconciled.json()["physical_stop_confirmed_at"] is not None
        failed_terminal = await runtime.terminal_repository.get("terminal-failed-projection")
        assert failed_terminal is not None
        assert failed_terminal.status is TerminalStatus.CLOSED

        lost_paths = paths.terminal(str(run["id"]), "terminal-lost-projection")
        lost_execution = Execution(
            id="execution-terminal-lost-projection",
            execution_key="terminal:terminal-lost-projection",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PTY,
            argv=["pwsh.exe"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(lost_paths.transcript),
            stderr_path=str(lost_paths.transcript),
            owner=RunnerPrincipal.model_validate(principal),
        )
        lost_execution.transition_to(ExecutionStatus.STARTING)
        await runtime.execution_repository.create_if_absent(lost_execution)
        await runtime.terminal_repository.create(
            TerminalSession(
                id="terminal-lost-projection",
                run_id=str(run["id"]),
                execution_id=lost_execution.id,
            )
        )
        lost_launch = await _bind_test_terminal_launch(
            runtime,
            client,
            headers,
            run_id=str(run["id"]),
            node_id="runner-a",
            terminal_id="terminal-lost-projection",
            execution_id=lost_execution.id,
        )
        lost_callback_fields = _runner_execution_callback_fields(lost_launch)
        assert (
            await client.post(
                f"/api/v1/runner/executions/{lost_execution.id}/status",
                headers=headers,
                json={
                    **lost_callback_fields,
                    "status": "running",
                    "pid": 5350,
                    "process_group_id": 5350,
                },
            )
        ).status_code == 200
        lost = await client.post(
            f"/api/v1/runner/executions/{lost_execution.id}/status",
            headers=headers,
            json={**lost_callback_fields, "status": "lost"},
        )
        assert lost.status_code == 200
        lost_terminal = await runtime.terminal_repository.get("terminal-lost-projection")
        assert lost_terminal is not None
        assert lost_terminal.status is TerminalStatus.LOST
        recovery_command, _ = await runtime.control_plane.runner_control_service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.CANCEL,
            idempotency_key="cancel:terminal-lost-projection",
            run_id=str(run["id"]),
            origin=RunnerCommandOrigin.APPLICATION_SERVICE,
            operation_family=RunnerOperationFamily.SAFETY_STOP,
            resource_kind=RunnerResourceKind.EXECUTION,
            resource_id=lost_execution.id,
            execution_id=lost_execution.id,
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1",
                stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            ),
            payload={
                "execution_id": lost_execution.id,
                "execution_key": lost_execution.execution_key,
            },
        )
        recovery_lease = await client.get("/api/v1/runner/commands/next", headers=headers)
        leased_recovery = recovery_lease.json()["command"]
        assert leased_recovery["id"] == recovery_command.id
        natural_stop_report = {
            **lost_callback_fields,
            "status": "completed",
            "physical_stop_confirmed": True,
            "executable_path": "/usr/bin/pwsh",
        }
        reconciled = await client.post(
            f"/api/v1/runner/executions/{lost_execution.id}/status",
            headers=headers,
            json=natural_stop_report,
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "cancelled"
        assert reconciled.json()["physical_stop_confirmed_at"] is not None
        stop_confirmed_at = reconciled.json()["physical_stop_confirmed_at"]
        repeated_natural_stop = await client.post(
            f"/api/v1/runner/executions/{lost_execution.id}/status",
            headers=headers,
            json=natural_stop_report,
        )
        assert repeated_natural_stop.status_code == 200
        assert repeated_natural_stop.json()["status"] == "cancelled"
        assert repeated_natural_stop.json()["physical_stop_confirmed_at"] == (stop_confirmed_at)
        enriched_natural_stop = await client.post(
            f"/api/v1/runner/executions/{lost_execution.id}/status",
            headers=headers,
            json={
                **natural_stop_report,
                "exit_code": 0,
                "executable_path": "/tmp/untrusted-replacement",
                "tool_version": "7.5",
            },
        )
        assert enriched_natural_stop.status_code == 200
        assert enriched_natural_stop.json()["status"] == "cancelled"
        assert enriched_natural_stop.json()["exit_code"] == 0
        assert enriched_natural_stop.json()["executable_path"] == "/usr/bin/pwsh"
        assert enriched_natural_stop.json()["tool_version"] == "7.5"
        reconciled_terminal = await runtime.terminal_repository.get("terminal-lost-projection")
        assert reconciled_terminal is not None
        assert reconciled_terminal.status is TerminalStatus.CLOSED
        recovery_ack = await client.post(
            f"/api/v1/runner/commands/{recovery_command.id}/finish-owned",
            headers=headers,
            json={
                "lease_id": leased_recovery["lease_id"],
                **_runner_command_callback_fields(leased_recovery),
                "succeeded": True,
                "result": {
                    "execution_id": lost_execution.id,
                    "local_execution_id": lost_execution.id,
                    "execution_key": lost_execution.execution_key,
                    "owner": principal,
                    "status": "cancelled",
                    "physical_stop_confirmed": True,
                },
            },
        )
        assert recovery_ack.status_code == 200

        barrier_paths = paths.terminal(str(run["id"]), "terminal-status-barrier")
        barrier_execution = Execution(
            id="execution-terminal-status-barrier",
            execution_key="terminal:terminal-status-barrier",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PTY,
            argv=["pwsh.exe"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(barrier_paths.transcript),
            stderr_path=str(barrier_paths.transcript),
            owner=RunnerPrincipal.model_validate(principal),
        )
        barrier_execution.transition_to(ExecutionStatus.STARTING)
        await runtime.execution_repository.create_if_absent(barrier_execution)
        await runtime.terminal_repository.create(
            TerminalSession(
                id="terminal-status-barrier",
                run_id=str(run["id"]),
                execution_id=barrier_execution.id,
            )
        )
        barrier_launch = await _bind_test_terminal_launch(
            runtime,
            client,
            headers,
            run_id=str(run["id"]),
            node_id="runner-a",
            terminal_id="terminal-status-barrier",
            execution_id=barrier_execution.id,
        )
        barrier_callback_fields = _runner_execution_callback_fields(barrier_launch)
        running_barrier, cancelled_barrier = await asyncio.gather(
            client.post(
                f"/api/v1/runner/executions/{barrier_execution.id}/status",
                headers=headers,
                json={
                    **barrier_callback_fields,
                    "status": "running",
                    "pid": 5450,
                    "process_group_id": 5450,
                },
            ),
            client.post(
                f"/api/v1/runner/executions/{barrier_execution.id}/status",
                headers=headers,
                json={
                    **barrier_callback_fields,
                    "status": "cancelled",
                    "physical_stop_confirmed": True,
                },
            ),
        )
        assert running_barrier.status_code in {200, 409}
        assert cancelled_barrier.status_code == 200
        barrier_persisted = await runtime.execution_repository.get(barrier_execution.id)
        assert barrier_persisted is not None
        assert barrier_persisted.status is ExecutionStatus.CANCELLED
        assert barrier_persisted.physical_stop_confirmed_at is not None
        barrier_terminal = await runtime.terminal_repository.get("terminal-status-barrier")
        assert barrier_terminal is not None
        assert barrier_terminal.status is TerminalStatus.CLOSED

        projection_events = await client.get(f"/api/v1/runs/{run['id']}/events")
        assert projection_events.status_code == 200
        session_events: dict[str, list[str]] = {}
        lost_closed_statuses: list[str] = []
        for item in projection_events.json()["items"]:
            session_id = item["payload"].get("session_id")
            if session_id in {"terminal-failed-projection", "terminal-lost-projection"}:
                session_events.setdefault(session_id, []).append(item["event_type"])
            if session_id == "terminal-lost-projection" and item["event_type"] == "terminal.closed":
                lost_closed_statuses.append(item["payload"]["status"])
        assert session_events["terminal-failed-projection"] == [
            "terminal.opened",
            "terminal.closed",
        ]
        assert session_events["terminal-lost-projection"] == [
            "terminal.opened",
            "terminal.lost",
            "terminal.closed",
        ]
        assert lost_closed_statuses == ["cancelled"]
        barrier_event_types = [
            item["event_type"]
            for item in projection_events.json()["items"]
            if item["payload"].get("session_id") == "terminal-status-barrier"
        ]
        assert barrier_event_types[-1] == "terminal.closed"

        empty = await client.get(
            "/api/v1/runner/commands/next?wait_seconds=0.01",
            headers=headers,
        )
        assert empty.status_code == 200
        assert empty.json()["command"] is None
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_target_http_command_uploads_bounded_response_body(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
                json={
                    "node_id": "runner-http",
                    "name": "Runner HTTP",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "capabilities": [
                        "target_http",
                        RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
                    ],
                },
            )
            headers = {
                "Authorization": f"Bearer {registration.json()['runner_token']}",
                "X-RiftX-Node-ID": "runner-http",
                "X-RiftX-Runner-Instance-ID": registration.json()["principal"]["instance_id"],
                "X-RiftX-Runner-Epoch": str(registration.json()["principal"]["epoch"]),
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Upload a bounded remote HTTP response",
                    "node_id": "runner-http",
                    "engagement": {"name": "Remote Target HTTP ownership"},
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            database = runtime.control_plane.database
            await SQLAlchemyAgentSessionRepository(database.session_factory).create(
                AgentSession(
                    id="session-target-http-control",
                    run_id=str(run["id"]),
                    model_profile="fast",
                )
            )
            await SQLAlchemyAgentCycleRepository(database.session_factory).create(
                RuntimeAgentCycle(
                    id="cycle-target-http-control",
                    run_id=str(run["id"]),
                    session_id="session-target-http-control",
                    sequence=1,
                )
            )
            await SQLAlchemyAgentStepRepository(database.session_factory).create(
                AgentStep(
                    id="step-target-http-control",
                    cycle_id="cycle-target-http-control",
                    sequence=1,
                    step_type=AgentStepType.TOOL_PROPOSAL,
                )
            )
            tool_call_id = "intent-target-http-control"
            await SQLAlchemyToolCallIntentRepository(database.session_factory).create(
                ToolCallIntent(
                    id=tool_call_id,
                    run_id=str(run["id"]),
                    session_id="session-target-http-control",
                    cycle_id="cycle-target-http-control",
                    step_id="step-target-http-control",
                    tool_id="target_http",
                )
            )
            command, created = await runtime.control_plane.runner_control_service.enqueue(
                "runner-http",
                kind=RunnerCommandKind.TARGET_HTTP,
                idempotency_key="target-http:integration-key",
                run_id=str(run["id"]),
                origin=RunnerCommandOrigin.APPLICATION_SERVICE,
                operation_family=RunnerOperationFamily.TARGET_HTTP,
                resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
                resource_id=tool_call_id,
                output_contract=RunnerOutputContract(
                    max_result_bytes=64 * 1024,
                    max_output_bytes=32,
                    allowed_streams=("command",),
                    result_schema="riftx.runner-result/target-http/v1",
                ),
                payload={
                    "launch": {
                        "run_id": str(run["id"]),
                        "session_id": "session-target-http-control",
                        "tool_call_id": tool_call_id,
                        "node_id": "runner-http",
                        "scope": {"domains": ["example.com"]},
                        "request": {
                            "execution_key": "target-http:integration-key",
                            "method": "GET",
                            "url": "https://example.com/",
                            "headers": {},
                            "timeout_seconds": 5,
                            "max_response_bytes": 32,
                        },
                    },
                },
            )
            assert created is True
            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            leased_command = leased.json()["command"]
            lease_id = leased_command["lease_id"]

            uploaded = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    **_runner_command_callback_fields(leased_command),
                    "offset": 0,
                    "data": base64.b64encode(b"remote body").decode(),
                },
            )
            assert uploaded.status_code == 200
            assert uploaded.json()["next_offset"] == 11

            replay = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    **_runner_command_callback_fields(leased_command),
                    "offset": 0,
                    "data": base64.b64encode(b"remote body").decode(),
                },
            )
            assert replay.status_code == 409
            assert replay.json()["error"]["code"] == "runner_output_offset_mismatch"

            oversized = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    **_runner_command_callback_fields(leased_command),
                    "offset": 11,
                    "data": base64.b64encode(b"x" * 22).decode(),
                },
            )
            assert oversized.status_code == 409
            assert oversized.json()["error"]["code"] == "runner_command_output_too_large"

            finished = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    **_runner_command_callback_fields(leased_command),
                    "succeeded": True,
                    "result": {
                        "result": {
                            "request_id": "request-target-http-control",
                            "execution_key": "target-http:integration-key",
                            "request_hash": "a" * 64,
                            "status_code": 200,
                            "elapsed_ms": 1,
                            "final_url": "https://example.com/",
                            "truncated": False,
                        }
                    },
                },
            )
            assert finished.status_code == 200
            assert (
                await runtime.control_plane.runner_control_service.read_command_output(command.id)
                == b"remote body"
            )
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_terminal_api_routes_commands_and_streams_transcript(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": f"Bearer {RUNNER_BOOTSTRAP_TOKEN}"},
                json={
                    "node_id": "windows-a",
                    "name": "Windows Runner A",
                    "platform": "windows",
                    "architecture": "amd64",
                    "capabilities": [
                        "powershell",
                        "conpty",
                        RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
                    ],
                },
            )
            token = registration.json()["runner_token"]
            principal = registration.json()["principal"]
            headers = {
                "Authorization": f"Bearer {token}",
                "X-RiftX-Node-ID": "windows-a",
                "X-RiftX-Runner-Instance-ID": principal["instance_id"],
                "X-RiftX-Runner-Epoch": str(principal["epoch"]),
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Remote Windows terminal",
                    "node_id": "windows-a",
                    "engagement": {"name": "Remote terminal E2E"},
                },
            )
            run = run_response.json()
            created = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": ["pwsh.exe", "-NoLogo", "-NoProfile"],
                    "cols": 132,
                    "rows": 48,
                },
            )
            assert created.status_code == 201, created.text
            terminal = created.json()
            assert terminal["status"] == "open"
            assert terminal["execution_status"] == "running"

            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            start_command = leased.json()["command"]
            assert start_command["kind"] == "terminal_start"
            assert start_command["payload"]["session_id"] == terminal["id"]
            assert start_command["payload"]["execution_id"] == terminal["execution_id"]
            assert start_command["payload"]["request"]["node_id"] == "windows-a"

            running = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/status",
                headers=headers,
                json={
                    **_runner_execution_callback_fields(start_command),
                    "status": "running",
                    "pid": 5150,
                    "process_group_id": 5150,
                },
            )
            assert running.status_code == 200
            assert running.json()["pid"] == 5150
            await client.post(
                f"/api/v1/runner/commands/{start_command['id']}/finish-owned",
                headers=headers,
                json={
                    "lease_id": start_command["lease_id"],
                    **_runner_command_callback_fields(start_command),
                    "succeeded": True,
                    "result": {"session_id": terminal["id"]},
                },
            )

            uploaded = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/output",
                headers=headers,
                json={
                    **_runner_execution_callback_fields(start_command),
                    "stream": "stdout",
                    "offset": 0,
                    "data": "UkVBRFkNCg==",
                },
            )
            assert uploaded.status_code == 200
            output = await runtime.control_plane.terminal_service.read(terminal["id"])
            assert output.data == b"READY\r\n"

            closed = await client.delete(f"/api/v1/terminals/{terminal['id']}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "open"
            assert closed.json()["execution_status"] == "running"
            close_lease = await client.get("/api/v1/runner/commands/next", headers=headers)
            close_command = close_lease.json()["command"]
            assert close_command["kind"] == "cancel"
            assert close_command["payload"] == {
                "execution_id": terminal["execution_id"],
                "execution_key": f"terminal:{terminal['id']}",
            }

            cancelled = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/status",
                headers=headers,
                json={
                    **_runner_execution_callback_fields(start_command),
                    "status": "cancelled",
                    "exit_code": 130,
                    "physical_stop_confirmed": True,
                },
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["exit_code"] == 130
            assert cancelled.json()["physical_stop_confirmed_at"] is not None
            close_acknowledged = await client.post(
                f"/api/v1/runner/commands/{close_command['id']}/finish-owned",
                headers=headers,
                json={
                    "lease_id": close_command["lease_id"],
                    **_runner_command_callback_fields(close_command),
                    "succeeded": True,
                    "result": {
                        "execution_id": terminal["execution_id"],
                        "local_execution_id": terminal["execution_id"],
                        "execution_key": f"terminal:{terminal['id']}",
                        "owner": principal,
                        "status": "cancelled",
                        "physical_stop_confirmed": True,
                        "session_id": terminal["id"],
                    },
                },
            )
            assert close_acknowledged.status_code == 200
            fetched = await client.get(f"/api/v1/terminals/{terminal['id']}")
            assert fetched.json()["status"] == "closed"
            assert fetched.json()["execution_status"] == "cancelled"
            assert fetched.json()["pid"] == 5150
            assert fetched.json()["exit_code"] == 130
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_legacy_four_null_terminal_replacement_runs_daemon_and_projects_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path, runner_command_lease_seconds=1.0)
    daemon: RunnerDaemon | None = None
    try:
        async for client in _client(runtime.control_plane):
            node_id = "legacy-terminal-runner"
            terminal_id = "legacy-terminal-session"
            execution_id = "legacy-terminal-execution"
            execution_key = f"terminal:{terminal_id}"
            runner_state = tmp_path / "legacy-terminal-runner-state"
            runner_client = RunnerControlClient(
                server_url="http://test",
                node_id=node_id,
                credentials=RunnerCredentialStore(runner_state / "runner-credentials.json"),
                registration_token=RUNNER_BOOTSTRAP_TOKEN,
                client=client,
            )
            daemon_config = RunnerDaemonConfig(
                server_url="http://test",
                node_id=node_id,
                name="Legacy Terminal Runner",
                state_path=runner_state,
                platform="windows",
                architecture="amd64",
                capabilities=("conpty",),
                registration_token=RUNNER_BOOTSTRAP_TOKEN,
                poll_wait_seconds=0.01,
                require_containment=False,
            )
            await runner_client.connect(daemon_config.registration)
            principal = runner_client.principal
            assert principal is not None

            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Reconcile a pre-ownership remote terminal",
                    "node_id": node_id,
                    "engagement": {"name": "Legacy terminal replacement E2E"},
                },
            )
            assert run_response.status_code == 201, run_response.text
            run = run_response.json()
            run_id = str(run["id"])

            central_paths = RunnerPaths(runtime.control_plane.settings.runner_state_path).terminal(
                run_id, terminal_id
            )
            central_execution = Execution(
                id=execution_id,
                execution_key=execution_key,
                run_id=run_id,
                node_id=node_id,
                owner=principal,
                executor_type=ExecutorType.PTY,
                argv=["pwsh.exe"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(central_paths.transcript),
                stderr_path=str(central_paths.transcript),
            )
            central_execution.transition_to(ExecutionStatus.STARTING)
            central_execution.transition_to(ExecutionStatus.RUNNING)
            await runtime.execution_repository.create_if_absent(central_execution)
            central_terminal = TerminalSession(
                id=terminal_id,
                run_id=run_id,
                execution_id=execution_id,
                runner_id=node_id,
                shell="pwsh.exe",
                cwd=str(run["workspace_path"]),
            )
            central_terminal.transition_to(TerminalStatus.OPEN)
            await runtime.terminal_repository.create(central_terminal)

            local_executions = FileExecutionRepository(runner_state / "executions.json")
            local_terminals = FileTerminalRepository(runner_state / "terminals.json")
            local_paths = RunnerPaths(runner_state).terminal(run_id, terminal_id)
            local_execution = Execution(
                id=execution_id,
                execution_key=execution_key,
                run_id=run_id,
                node_id=node_id,
                owner=principal,
                executor_type=ExecutorType.PTY,
                argv=["pwsh.exe"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(local_paths.transcript),
                stderr_path=str(local_paths.transcript),
            )
            local_execution.transition_to(ExecutionStatus.STARTING)
            local_execution.transition_to(ExecutionStatus.RUNNING)
            await local_executions.create_if_absent(local_execution)
            local_terminal = TerminalSession(
                id=terminal_id,
                run_id=run_id,
                execution_id=execution_id,
                runner_id=node_id,
                shell="pwsh.exe",
                cwd=str(run["workspace_path"]),
            )
            local_terminal.transition_to(TerminalStatus.OPEN)
            await local_terminals.create(local_terminal)

            callback_fields = (
                "runner_command_id",
                "runner_effect_binding_id",
                "runner_binding_digest",
                "runner_envelope_digest",
            )
            assert tuple(
                getattr(central_execution, field_name) for field_name in callback_fields
            ) == (None, None, None, None)
            assert tuple(
                getattr(local_execution, field_name) for field_name in callback_fields
            ) == (None, None, None, None)

            commands = SQLAlchemyRunnerCommandRepository(
                runtime.control_plane.database.session_factory
            )
            legacy_command_id = "legacy-terminal-command"
            seeded_at = datetime.now(UTC)
            async with (
                runtime.control_plane.database.session_factory() as session,
                session.begin(),
            ):
                session.add(
                    RunnerCommandRecord(
                        id=legacy_command_id,
                        node_id=node_id,
                        kind=RunnerCommandKind.BROWSER.value,
                        idempotency_key="legacy:untrusted-terminal-command",
                        target_runner_instance_id=principal.instance_id,
                        target_runner_epoch=principal.epoch,
                        payload_json={
                            "execution_id": "attacker-selected-execution",
                            "session_id": "attacker-selected-terminal",
                        },
                        status=RunnerCommandStatus.PENDING.value,
                        attempts=0,
                        lease_id=None,
                        lease_expires_at=None,
                        result_json={},
                        error="",
                        state_version=0,
                        created_at=seeded_at,
                        updated_at=seeded_at,
                        completed_at=None,
                    )
                )
            quarantined = await commands.quarantine(
                legacy_command_id,
                reason="legacy_ownership_missing",
                quarantined_at=seeded_at,
                expected_state_version=0,
            )
            assert quarantined.ownership_state is RunnerCommandOwnershipState.QUARANTINED

            assert (
                await runtime.control_plane.runner_control_service.reconcile_quarantined_commands()
                == 1
            )
            async with runtime.control_plane.database.session_factory() as session:
                quarantine_record = await session.get(
                    RunnerCommandOwnershipRecord,
                    legacy_command_id,
                )
                assert quarantine_record is not None
                replacement_command_id = quarantine_record.replacement_command_id
                assert quarantine_record.reconciliation_state == "replaced"
                assert replacement_command_id is not None

            replacement = await commands.get(replacement_command_id)
            assert replacement is not None
            assert replacement.kind is RunnerCommandKind.TERMINAL_CLOSE
            assert replacement.status is RunnerCommandStatus.PENDING
            assert replacement.ownership_state is RunnerCommandOwnershipState.VERIFIED
            assert replacement.target == principal
            assert replacement.payload == {
                "session_id": terminal_id,
                "execution_id": execution_id,
                "execution_key": execution_key,
            }
            assert "attacker-selected" not in repr(replacement)
            ownership = replacement.ownership
            assert ownership is not None
            binding = ownership.effect_binding
            assert binding.origin is RunnerCommandOrigin.SAFETY_RECONCILER
            assert binding.operation_family is RunnerOperationFamily.SAFETY_STOP
            assert binding.resource_kind is RunnerResourceKind.TERMINAL_SESSION
            assert binding.resource_id == terminal_id
            assert binding.execution_id == execution_id
            assert binding.target == principal
            assert ownership.output_contract.result_schema == (
                "riftx.runner-result/terminal-stop/v1"
            )
            assert ownership.output_contract.stop_ack_schema == RUNNER_STOP_ACK_TERMINAL_SCHEMA

            leased = await runner_client.poll(wait_seconds=0)
            assert leased is not None
            assert leased.id == replacement.id
            assert leased.kind is RunnerCommandKind.TERMINAL_CLOSE

            class DurableLegacyTerminalHandler:
                def __init__(
                    self,
                    *,
                    expected_execution_id: str,
                    expected_terminal_id: str,
                    executions: FileExecutionRepository,
                    terminals: FileTerminalRepository,
                ) -> None:
                    self.expected_execution_id = expected_execution_id
                    self.expected_terminal_id = expected_terminal_id
                    self.executions = executions
                    self.terminals = terminals

                async def handle(
                    self,
                    kind: RunnerCommandKind,
                    payload: dict[str, object],
                    *,
                    journal_identity: Any = None,
                    effect_guard: Any = None,
                    on_admitted: Any = None,
                ) -> object:
                    del kind, payload, journal_identity, effect_guard, on_admitted
                    raise AssertionError("legacy replacement must use cancel_execution")

                async def cancel_execution(self, target_execution_id: str) -> Execution:
                    assert target_execution_id == self.expected_execution_id
                    execution = await self.executions.get(target_execution_id)
                    assert execution is not None
                    assert execution.executor_type is ExecutorType.PTY
                    stopped_at = datetime.now(UTC)
                    execution.transition_to(
                        ExecutionStatus.CANCELLED,
                        at=stopped_at,
                        exit_code=130,
                    )
                    execution.physical_stop_confirmed_at = stopped_at
                    execution = await self.executions.save(execution)
                    terminal = await self.terminals.get_by_execution(target_execution_id)
                    assert terminal is not None
                    assert terminal.id == self.expected_terminal_id
                    terminal.transition_to(TerminalStatus.CLOSED, at=stopped_at)
                    await self.terminals.save(terminal)
                    return execution

                async def close(self) -> None:
                    return None

            daemon = RunnerDaemon(
                config=daemon_config,
                client=runner_client,
                supervisor=ProcessSupervisor(
                    local_executions,
                    RunnerPaths(runner_state),
                    termination_grace_seconds=0.01,
                ),
                executions=local_executions,
                terminal_handler=DurableLegacyTerminalHandler(
                    expected_execution_id=execution_id,
                    expected_terminal_id=terminal_id,
                    executions=local_executions,
                    terminals=local_terminals,
                ),
            )
            await daemon.handle_command(leased)

            expected_ack = {
                "execution_id": execution_id,
                "local_execution_id": execution_id,
                "execution_key": execution_key,
                "owner": principal.model_dump(mode="json"),
                "status": ExecutionStatus.CANCELLED.value,
                "physical_stop_confirmed": True,
                "session_id": terminal_id,
            }
            finished = await commands.get(replacement.id)
            assert finished is not None
            assert finished.status is RunnerCommandStatus.COMPLETED
            assert finished.result == expected_ack

            local_stopped = await local_executions.get(execution_id)
            local_closed = await local_terminals.get(terminal_id)
            assert local_stopped is not None
            assert local_stopped.status is ExecutionStatus.CANCELLED
            assert local_stopped.physical_stop_confirmed_at is not None
            assert tuple(getattr(local_stopped, field_name) for field_name in callback_fields) == (
                None,
                None,
                None,
                None,
            )
            assert local_closed is not None
            assert local_closed.status is TerminalStatus.CLOSED
            tombstone = await OperationJournal(
                runner_state / "execution-cancellations.json"
            ).get_resource(f"execution:{execution_id}")
            assert tombstone is not None
            assert tombstone.outcome.get("state") == "physical_stop_confirmed"

            projected_execution = await runtime.execution_repository.get(execution_id)
            projected_terminal = await runtime.terminal_repository.get(terminal_id)
            assert projected_execution is not None
            assert projected_execution.status is ExecutionStatus.CANCELLED
            assert projected_execution.physical_stop_confirmed_at is not None
            assert tuple(
                getattr(projected_execution, field_name) for field_name in callback_fields
            ) == (None, None, None, None)
            assert projected_terminal is not None
            assert projected_terminal.status is TerminalStatus.CLOSED
            assert projected_terminal.closed_at is not None

            receipt = await commands.get_stop_receipt(replacement.id)
            assert receipt is not None
            assert receipt.command_id == replacement.id
            assert receipt.effect_binding_id == binding.id
            assert receipt.envelope_digest == ownership.envelope_digest
            assert receipt.binding_digest == binding.binding_digest
            assert receipt.operation is RunnerCommandKind.TERMINAL_CLOSE
            assert receipt.operation_family is RunnerOperationFamily.SAFETY_STOP
            assert receipt.resource_kind is RunnerResourceKind.TERMINAL_SESSION
            assert receipt.resource_id == terminal_id
            assert receipt.execution_id == execution_id
            assert receipt.node_id == node_id
            assert receipt.principal == principal
            assert receipt.ack_digest == runner_stop_ack_digest(expected_ack)
            async with runtime.control_plane.database.session_factory() as session:
                projection = await session.get(RunnerStopProjectionRecord, receipt.id)
                assert projection is not None
                assert projection.projection_state == "applied"
                assert projection.state_version == 1
            assert await commands.list_pending_stop_receipts() == []

            completion_intents = [
                record
                for record in await _workflow_signal_records(runtime, run_id)
                if record.signal_kind == WorkflowSignalKind.EXECUTION_COMPLETED.value
            ]
            assert completion_intents == []
            assert isinstance(runtime.workflow, FakeWorkflowClient)
            assert not any(
                call == ("execution_completed", run_id, execution_id)
                for call in runtime.workflow.calls
            )
            terminal_events = [
                event
                for event in await runtime.event_repository.list_after(run_id)
                if event.event_type == "terminal.closed"
                and event.payload.get("session_id") == terminal_id
            ]
            assert len(terminal_events) == 1
            assert terminal_events[0].payload["execution_id"] == execution_id
            assert await runtime.control_plane.runner_control_service.reconcile_stop_receipts() == 0
            assert (
                await runtime.control_plane.runner_control_service.reconcile_quarantined_commands()
                == 0
            )
    finally:
        if daemon is not None:
            await daemon.close()
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_execution_api_exposes_provenance_and_cursor_output(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            run_id = str(run["id"])
            paths = RunnerPaths(runtime.control_plane.settings.runner_state_path).execution(
                run_id, "execution-public"
            )
            paths.stdout.parent.mkdir(parents=True, exist_ok=True)
            paths.stdout.write_bytes(b"hello execution\n")
            paths.stderr.write_bytes(b"diagnostic\n")
            execution = Execution(
                id="execution-public",
                execution_key="public-key",
                run_id=run_id,
                node_id="local",
                executor_type=ExecutorType.PROCESS,
                argv=["/usr/bin/printf", "hello"],
                tool_id="printf",
                tool_version="coreutils 9",
                executable_path="/usr/bin/printf",
                cwd=str(run["workspace_path"]),
                env_diff={"LANG": "C.UTF-8"},
                platform_system="linux",
                platform_release="6.10",
                platform_architecture="x86_64",
                stdout_path=str(paths.stdout),
                stderr_path=str(paths.stderr),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            execution.transition_to(ExecutionStatus.EXITED, exit_code=0)
            await runtime.execution_repository.create_if_absent(execution)
            events_before_wait = await runtime.event_repository.list_after(run_id)

            listed = await client.get(f"/api/v1/runs/{run_id}/executions")
            fetched = await client.get(f"/api/v1/executions/{execution.id}")
            waited = await client.post(
                f"/api/v1/executions/{execution.id}/wait",
                params={"timeout_seconds": 0.1, "max_bytes": 5},
            )
            output = await client.get(
                f"/api/v1/executions/{execution.id}/output",
                params={"max_bytes": 5},
            )

            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [execution.id]
            assert fetched.status_code == 200
            assert fetched.json()["tool_id"] == "printf"
            assert fetched.json()["tool_version"] == "coreutils 9"
            assert fetched.json()["executable_path"] == "/usr/bin/printf"
            assert fetched.json()["platform_system"] == "linux"
            assert fetched.json()["platform_architecture"] == "x86_64"
            assert waited.status_code == 200
            assert waited.json()["wait_status"] == "execution_completed"
            assert waited.json()["execution_status"] == "exited"
            assert waited.json()["partial_output"] == "hellodiagn"
            events_after_wait = await runtime.event_repository.list_after(run_id)
            assert events_after_wait == events_before_wait
            assert output.status_code == 200
            assert output.json()["stdout"]["data"] == "aGVsbG8="
            assert output.json()["stdout"]["next_cursor"] == 5
            assert output.json()["stdout"]["eof"] is False
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_complete_agent_runner_sse_finding_report_lifecycle(tmp_path: Path) -> None:
    """Exercise the exact V2 design §28.5 chain through public API boundaries."""

    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "fake-success": {
                        "command": [sys.executable, str(FAKE_TOOL_FIXTURE)],
                        "executor": "process",
                        "capabilities": ["deterministic_verification"],
                        "approval": "never",
                        "timeout": 30,
                        "version_probe": {
                            "command": [
                                sys.executable,
                                str(FAKE_TOOL_FIXTURE),
                                "--version",
                            ]
                        },
                    }
                },
            },
            sort_keys=False,
        )
    )
    environment = await WorkflowEnvironment.start_time_skipping()
    runtime: RuntimeFixture | None = None
    task_queue = f"riftx-e2e-{uuid4()}"
    temporal_config = TemporalRuntimeConfig(
        task_queue=task_queue,
        workflow_id_prefix="e2e-workflow",
    )
    workflow_client = TemporalRunClient(environment.client, temporal_config)

    try:
        runtime = await _build_runtime(tmp_path, workflow=workflow_client)
        registry = ToolRegistry(tools_path, node_id="local")
        await registry.refresh()
        event_repository = SQLAlchemyRunEventRepository(
            runtime.control_plane.database.session_factory
        )
        supervisor = runtime.control_plane.process_supervisor
        assert supervisor is not None
        browser_service = runtime.control_plane.browser_service
        target_http_service = runtime.control_plane.target_http_service
        assert browser_service is not None
        assert target_http_service is not None
        model = FullRunModel()
        agent_cycle = AgentCycle(
            services=AgentRuntimeServices(
                tool_registry=registry,
                skill_registry=create_default_skill_registry(),
                supervisor=supervisor,
                finding_repository=runtime.finding_repository,
                event_repository=event_repository,
                finding_service=runtime.control_plane.finding_service,
                artifact_service=runtime.control_plane.artifact_service,
                terminal_service=runtime.control_plane.terminal_service,
                approval_repository=runtime.approval_repository,
            ),
            session_factory=runtime.control_plane.database.session_factory,
            checkpoint_store=SQLAlchemyCheckpointStore(
                runtime.control_plane.database.session_factory
            ),
            model=model,
            model_provider=FixedModelProvider(model),
        )
        activities = RiftXActivities(
            run_repository=runtime.run_repository,
            event_repository=event_repository,
            tool_registry=registry,
            safety_stopper=RunSafetyStopService(
                execution_repository=runtime.execution_repository,
                execution_runner=supervisor,
                resource_stoppers={
                    "browser_sessions": browser_service,
                    "target_http_requests": target_http_service,
                },
                execution_cancel_timeout_seconds=0.2,
                execution_cancel_poll_seconds=0.01,
            ),
            agent_cycle=agent_cycle,
            approval_recorder=ApprovalRequestRecorder(
                approval_repository=runtime.approval_repository,
                event_repository=event_repository,
                tool_registry=registry,
            ),
            closure_verifier=ClosureVerifierApplicationService(
                runs=runtime.run_repository,
                task_graphs=SQLAlchemyTaskGraphRepository(
                    runtime.control_plane.database.session_factory
                ),
                reasoning_graphs=SQLAlchemyReasoningGraphRepository(
                    runtime.control_plane.database.session_factory
                ),
                evidence=SQLAlchemyEvidenceLedgerRepository(
                    runtime.control_plane.database.session_factory
                ),
            ),
            report_service=runtime.control_plane.report_service,
            session_factory=runtime.control_plane.database.session_factory,
        )

        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[RiftXRunWorkflow],
            activities=activities.registered(),
            max_cached_workflows=0,
        ):
            async for client in _client(runtime.control_plane):
                created_response = await client.post(
                    "/api/v1/runs",
                    json={
                        "objective": "Execute the deterministic full-run verifier",
                        "engagement": {"name": "Authorized E2E fixture"},
                        "success_criteria": [
                            {
                                "description": "Execute the fixture and record a finding",
                                "required": True,
                            }
                        ],
                        "entry_points": [{"kind": "ip", "value": "127.0.0.1"}],
                        "scope": {"ips": ["127.0.0.1"]},
                        "approval_mode": "balanced",
                    },
                )
                assert created_response.status_code == 201, created_response.text
                created = created_response.json()
                run_id = str(created["id"])
                instruction_response = await client.post(
                    f"/api/v1/runs/{run_id}/message",
                    json={
                        "message": (
                            "Execute the authorized deterministic verifier and complete "
                            "the stated objective."
                        )
                    },
                )
                assert instruction_response.status_code == 202, instruction_response.text
                handle = workflow_client.get_handle(run_id)
                workflow_result = await handle.result()

                assert workflow_result["phase"] == WorkflowPhase.COMPLETED.value
                assert model.calls == 4

                run_response = await client.get(f"/api/v1/runs/{run_id}")
                executions_response = await client.get(f"/api/v1/runs/{run_id}/executions")
                findings_response = await client.get(f"/api/v1/runs/{run_id}/findings")
                artifacts_response = await client.get(f"/api/v1/runs/{run_id}/artifacts")
                reports_response = await client.get(f"/api/v1/runs/{run_id}/reports")
                events_response = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    params={"limit": 1000},
                )
                sse_response = await client.get(
                    f"/api/v1/runs/{run_id}/events/stream",
                    params={"follow": "false"},
                )

                assert run_response.json()["status"] == "completed"
                executions = executions_response.json()["items"]
                assert len(executions) == 1
                assert executions[0]["tool_id"] == "fake-success"
                assert executions[0]["status"] == "exited"
                assert executions[0]["exit_code"] == 0
                output_response = await client.get(
                    f"/api/v1/executions/{executions[0]['id']}/output"
                )
                stdout = base64.b64decode(output_response.json()["stdout"]["data"])
                assert stdout.decode().strip() == "args=e2e"

                findings = findings_response.json()["items"]
                assert len(findings) == 1
                assert findings[0]["title"] == "Deterministic test service exposure"
                assert findings[0]["severity"] == "high"

                reports = reports_response.json()["items"]
                assert {item["format"] for item in reports} == {"markdown", "html", "json"}
                assert workflow_result["report_id"] in {item["id"] for item in reports}
                assert all(item["finding_ids"] == [findings[0]["id"]] for item in reports)
                assert len(artifacts_response.json()["items"]) == 3
                markdown_report = next(item for item in reports if item["format"] == "markdown")
                report_content = await client.get(markdown_report["content_url"])
                assert "Deterministic test service exposure" in report_content.text
                assert "Closure outcome:** `partial`" in report_content.text

                events = events_response.json()["items"]
                event_types = [item["event_type"] for item in events]
                ordered_types = [
                    "run.created",
                    "run.prepared",
                    "agent.tool_completed",
                    "finding.created",
                    "agent.completion_requested",
                    "agent.cycle_completed",
                    "run.closure_evaluated",
                    "report.generated",
                    "run.cleaned_up",
                ]
                positions = [event_types.index(event_type) for event_type in ordered_types]
                assert positions == sorted(positions)
                closure_event = next(
                    item for item in events if item["event_type"] == "run.closure_evaluated"
                )
                assert closure_event["payload"]["outcome"] == "partial"
                assert closure_event["payload"]["reason_codes"] == [
                    "success_criterion_unmapped",
                    "task_graph_missing",
                ]
                assert sse_response.status_code == 200
                sse_event_types = [
                    line.removeprefix("event: ")
                    for line in sse_response.text.splitlines()
                    if line.startswith("event: ")
                ]
                assert sse_event_types == event_types
    finally:
        if runtime is not None:
            await runtime.control_plane.close()
        await environment.shutdown()


@pytest.mark.asyncio
async def test_model_profile_configuration_is_redacted_and_drives_run_defaults(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        model_environment={"RIFTX_MODEL_AVAILABLE_KEY": "environment-secret-value"},
        admin_token="test-only-admin-operator-token-0002",
    )
    try:
        async for client in _client(runtime.control_plane):
            admin_headers = {"Authorization": "Bearer test-only-admin-operator-token-0002"}
            initial = await client.get("/api/v1/model-profiles")
            assert initial.status_code == 200
            assert {item["request_mode"] for item in initial.json()["profiles"]} == {
                "chat_completions"
            }

            configured = await client.put(
                "/api/v1/model-profiles/lab",
                headers=admin_headers,
                json={
                    "provider": "openai_compatible",
                    "model": "lab-model",
                    "request_mode": "responses",
                    "base_url": "https://models.example/v1",
                    "api_key_env": "RIFTX_MODEL_LAB_KEY",
                    "api_key": "api-secret-value",
                },
            )
            assert configured.status_code == 200, configured.text
            assert configured.json()["request_mode"] == "responses"
            assert configured.json()["has_stored_api_key"] is True
            assert "api-secret-value" not in configured.text

            missing_compatible_base = await client.put(
                "/api/v1/model-profiles/missing-base",
                headers=admin_headers,
                json={"model": "missing-base-model", "requires_api_key": False},
            )
            assert missing_compatible_base.status_code == 422
            assert "explicit base_url" in missing_compatible_base.text

            excessive_timeout = await client.put(
                "/api/v1/model-profiles/excessive-timeout",
                headers=admin_headers,
                json={
                    "provider": "openai",
                    "model": "slow-model",
                    "requires_api_key": False,
                    "timeout_seconds": 600.01,
                },
            )
            assert excessive_timeout.status_code == 422

            disabled_key_value = "disabled-profile-secret-value"
            disabled_key = await client.put(
                "/api/v1/model-profiles/disabled-key",
                headers=admin_headers,
                json={
                    "provider": "openai",
                    "model": "local-model",
                    "requires_api_key": False,
                    "api_key": disabled_key_value,
                },
            )
            assert disabled_key.status_code == 422
            assert disabled_key_value not in disabled_key.text

            invalid_secret_change = await client.put(
                "/api/v1/model-profiles/lab",
                headers=admin_headers,
                json={
                    "model": "lab-model",
                    "api_key": "validation-secret-value",
                    "clear_stored_api_key": True,
                },
            )
            assert invalid_secret_change.status_code == 422
            assert "validation-secret-value" not in invalid_secret_change.text

            invalid_secret_type = await client.put(
                "/api/v1/model-profiles/lab",
                headers=admin_headers,
                json={
                    "model": "lab-model",
                    "api_key": {"token": "typed-secret-value"},
                },
            )
            assert invalid_secret_type.status_code == 422
            assert "typed-secret-value" not in invalid_secret_type.text
            assert invalid_secret_type.json()["error"]["details"][0]["input"] == "[redacted]"

            for forbidden_environment in (
                "AWS_SECRET_ACCESS_KEY",
                "DATABASE_PASSWORD",
                "RIFTX_ADMIN_TOKEN",
            ):
                forbidden_environment_response = await client.put(
                    "/api/v1/model-profiles/environment-exfiltration",
                    headers=admin_headers,
                    json={
                        "model": "attacker-model",
                        "base_url": "https://capture.example/v1",
                        "api_key_env": forbidden_environment,
                    },
                )
                assert forbidden_environment_response.status_code == 422
                assert "must start with 'RIFTX_MODEL_'" in forbidden_environment_response.text

            for forbidden_base_url in (
                "http://169.254.169.254/v1",
                "http://2852039166/v1",
                "http://0xa9fea9fe/v1",
                "http://0251.0376.0251.0376/v1",
                "http://0.0.0.0/v1",
                "http://224.0.0.1/v1",
                "http://[::]/v1",
                "http://[fe80::1]/v1",
                "http://[ff02::1]/v1",
                "http://[::ffff:169.254.169.254]/v1",
                "https://${AWS_SECRET_ACCESS_KEY}.capture.example/v1",
            ):
                forbidden_endpoint = await client.put(
                    "/api/v1/model-profiles/unsafe-endpoint",
                    headers=admin_headers,
                    json={
                        "model": "attacker-model",
                        "base_url": forbidden_base_url,
                        "requires_api_key": False,
                    },
                )
                assert forbidden_endpoint.status_code == 422, forbidden_endpoint.text

            allowed_loopback = await client.put(
                "/api/v1/model-profiles/local-loopback",
                headers=admin_headers,
                json={
                    "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key_env": None,
                    "requires_api_key": False,
                },
            )
            assert allowed_loopback.status_code == 200, allowed_loopback.text
            removed_loopback = await client.delete(
                "/api/v1/model-profiles/local-loopback",
                headers=admin_headers,
            )
            assert removed_loopback.status_code == 200

            listed = await client.get("/api/v1/model-profiles")
            assert listed.status_code == 200
            assert "api-secret-value" not in listed.text
            assert {item["name"] for item in listed.json()["profiles"]} == {
                "deep",
                "fast",
                "lab",
                "primary",
            }

            environment_profile = await client.put(
                "/api/v1/model-profiles/environment",
                headers=admin_headers,
                json={
                    "model": "environment-model",
                    "base_url": "https://models.example/v1",
                    "api_key_env": "RIFTX_MODEL_AVAILABLE_KEY",
                },
            )
            assert environment_profile.status_code == 200
            assert environment_profile.json()["api_key_configured"] is True
            assert "environment-secret-value" not in environment_profile.text

            selected = await client.put(
                "/api/v1/model-profiles/default",
                headers=admin_headers,
                json={"profile": "lab"},
            )
            assert selected.status_code == 200
            assert selected.json()["default_profile"] == "lab"

            missing_default = await client.put(
                "/api/v1/model-profiles/default",
                headers=admin_headers,
                json={"profile": "missing"},
            )
            assert missing_default.status_code == 404
            assert missing_default.json()["error"]["code"] == "model_profile_not_found"

            created = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Use the configured default",
                    "engagement": {"name": "Authorized model test"},
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["model_profile"] == "lab"

            environment_run = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Use an environment credential",
                    "model_profile": "environment",
                    "engagement": {"name": "Authorized model test"},
                },
            )
            assert environment_run.status_code == 201
            assert environment_run.json()["model_profile"] == "environment"

            missing_credentials = await client.put(
                "/api/v1/model-profiles/no-credentials",
                headers=admin_headers,
                json={
                    "model": "missing-key-model",
                    "base_url": "https://models.example/v1",
                    "api_key_env": "RIFTX_MODEL_MISSING_KEY",
                },
            )
            assert missing_credentials.status_code == 200
            assert missing_credentials.json()["api_key_configured"] is False

            rejected_default = await client.put(
                "/api/v1/model-profiles/default",
                headers=admin_headers,
                json={"profile": "no-credentials"},
            )
            assert rejected_default.status_code == 409
            assert rejected_default.json()["error"]["code"] == "model_credentials_missing"

            rejected_default_credential_removal = await client.put(
                "/api/v1/model-profiles/lab",
                headers=admin_headers,
                json={
                    "model": "lab-model",
                    "base_url": "https://models.example/v1",
                    "api_key_env": "RIFTX_MODEL_LAB_KEY",
                    "clear_stored_api_key": True,
                },
            )
            assert rejected_default_credential_removal.status_code == 409
            assert (
                rejected_default_credential_removal.json()["error"]["code"]
                == "model_credentials_missing"
            )

            rejected_create = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Reject missing credentials",
                    "model_profile": "no-credentials",
                    "engagement": {"name": "Authorized model test"},
                },
            )
            assert rejected_create.status_code == 503
            assert rejected_create.json()["error"]["code"] == "model_credentials_missing"

            rejected_switch = await client.post(
                f"/api/v1/runs/{created.json()['id']}/model",
                json={"model_profile": "no-credentials"},
            )
            assert rejected_switch.status_code == 503
            assert rejected_switch.json()["error"]["code"] == "model_credentials_missing"
            assert not any(
                call[0] == "switch_model" and call[2] == "no-credentials"
                for call in runtime.workflow.calls
            )

            missing = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Reject an unknown profile",
                    "model_profile": "missing",
                    "engagement": {"name": "Authorized model test"},
                },
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "model_profile_not_found"

            active_profile = await client.delete(
                "/api/v1/model-profiles/environment",
                headers=admin_headers,
            )
            assert active_profile.status_code == 409
            assert active_profile.json()["error"]["code"] == "model_profile_in_use"

            removed = await client.delete(
                "/api/v1/model-profiles/primary",
                headers=admin_headers,
            )
            assert removed.status_code == 200
            cannot_remove_default = await client.delete(
                "/api/v1/model-profiles/lab",
                headers=admin_headers,
            )
            assert cannot_remove_default.status_code == 409

        secrets_path = runtime.control_plane.settings.model_secrets_path
        models_path = runtime.control_plane.settings.models_config_path
        assert (
            json.loads(secrets_path.read_text())["api_keys"]["lab"]["value"] == "api-secret-value"
        )
        assert "api-secret-value" not in models_path.read_text()
        assert stat.S_IMODE(secrets_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_model_profile_administration_requires_configured_admin_token(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        admin_token="test-only-admin-operator-token-0002",
    )
    try:
        async for client in _client(runtime.control_plane):
            readable = await client.get("/api/v1/model-profiles")
            assert readable.status_code == 200
            denied_detail = await client.get(
                "/api/v1/model-profiles/admin",
                headers={"Authorization": ""},
            )
            assert denied_detail.status_code == 401
            denied = await client.put(
                "/api/v1/model-profiles/lab",
                headers={"Authorization": ""},
                json={
                    "model": "lab-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "requires_api_key": False,
                },
            )
            assert denied.status_code == 401
            allowed = await client.put(
                "/api/v1/model-profiles/lab",
                headers={"Authorization": "Bearer test-only-admin-operator-token-0002"},
                json={
                    "model": "lab-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "requires_api_key": False,
                },
            )
            assert allowed.status_code == 200

        runtime.control_plane.settings = replace(runtime.control_plane.settings, admin_token=None)
        with pytest.raises(DeploymentProfileError) as captured:
            create_app(control_plane=runtime.control_plane)
        assert captured.value.code == "local_operator_credential_required"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_model_profile_override_is_effective_and_cannot_be_removed(tmp_path: Path) -> None:
    runtime = await _build_runtime(
        tmp_path,
        model_profile_override="fast",
        admin_token="test-only-admin-operator-token-0002",
    )
    try:
        async for client in _client(runtime.control_plane):
            listed = await client.get("/api/v1/model-profiles")
            assert listed.status_code == 200
            assert listed.json()["default_profile"] == "primary"
            assert listed.json()["effective_default_profile"] == "fast"

            created = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Use the configured override",
                    "engagement": {"name": "Authorized override test"},
                },
            )
            assert created.status_code == 201
            assert created.json()["model_profile"] == "fast"

            blocked = await client.delete(
                "/api/v1/model-profiles/fast",
                headers={"Authorization": "Bearer test-only-admin-operator-token-0002"},
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "model_profile_in_use"
    finally:
        await runtime.control_plane.close()
