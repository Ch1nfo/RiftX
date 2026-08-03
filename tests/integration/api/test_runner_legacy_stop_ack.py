from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.integration.api.test_control_plane import (
    RUNNER_BOOTSTRAP_TOKEN,
    _build_runtime,
    _client,
)

import riftx.api.routes.runner_control as runner_control_route
from riftx.application.run_kind_effects import (
    PolicyDenialReason,
    RunKindEffectPolicyDenied,
)
from riftx.application.services import NodeRegistration
from riftx.domain import (
    ExecutionStatus,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
)
from riftx.domain.base import utc_now
from riftx.persistence.mappers import (
    runner_command_ownership_to_record,
    runner_command_to_record,
)
from riftx.persistence.orm import (
    RunnerCommandOwnershipRecord,
    RunnerCommandRecord,
    RunnerStopProjectionRecord,
    RunnerStopReceiptRecord,
)
from riftx.persistence.workflow_signals import WorkflowSignalIntentRecord


@pytest.mark.asyncio
async def test_legacy_finish_wire_records_only_quarantine_stop_evidence(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    service = runtime.control_plane.runner_control_service
    # Keep this test deterministic: the assertion is about the finish route,
    # not the independent background replacement reconciler.
    service.reconcile_quarantined_commands = AsyncMock(return_value=0)  # type: ignore[method-assign]
    registration = NodeRegistration(
        node_id="legacy-ack-node",
        name="Legacy ACK Runner",
        platform="linux",
        architecture="x86_64",
        runner_version="2.9.0",
        capabilities=(),
    )
    original = await service.register(
        registration,
        bootstrap_token=RUNNER_BOOTSTRAP_TOKEN,
    )
    now = utc_now()
    command = RunnerCommand(
        id="legacy-wire-stop",
        node_id=registration.node_id,
        target=original.principal,
        kind=RunnerCommandKind.CANCEL,
        idempotency_key="legacy-wire-stop",
        payload={"untrusted_legacy_resource": "must-not-be-projected"},
        status=RunnerCommandStatus.LEASED,
        attempts=1,
        lease_id="legacy-wire-lease",
        lease_expires_at=now - timedelta(minutes=1),
        result={"legacy_result": "preserved"},
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )
    async with runtime.control_plane.database.session_factory() as session, session.begin():
        session.add(runner_command_to_record(command))
        await session.flush()
        session.add(runner_command_ownership_to_record(command))

    headers = {
        "Authorization": f"Bearer {original.token}",
        "X-RiftX-Node-ID": registration.node_id,
        "X-RiftX-Runner-Instance-ID": original.principal.instance_id,
        "X-RiftX-Runner-Epoch": str(original.principal.epoch),
    }
    ack = {
        "execution_id": "runner-local-execution",
        "local_execution_id": "runner-local-execution",
        "execution_key": "runner-local-key",
        "owner": original.principal.model_dump(mode="json"),
        "status": ExecutionStatus.CANCELLED.value,
        "physical_stop_confirmed": True,
    }
    legacy_wire = {
        "lease_id": command.lease_id,
        "succeeded": True,
        "result": ack,
    }
    owned_wire = {
        **legacy_wire,
        "state_version": 0,
        "envelope_digest": "a" * 64,
        "binding_digest": "b" * 64,
    }

    try:
        async for client in _client(runtime.control_plane):
            old_wire_on_owned_route = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish-owned",
                headers=headers,
                json=legacy_wire,
            )
            assert old_wire_on_owned_route.status_code == 422

            owned_wire_on_legacy_route = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=headers,
                json=owned_wire,
            )
            assert owned_wire_on_legacy_route.status_code == 422

            accepted = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=headers,
                json=legacy_wire,
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json() == {
                "id": command.id,
                "status": RunnerCommandStatus.LEASED.value,
                "state_version": 1,
                "completed_at": None,
            }

            replay = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=headers,
                json=legacy_wire,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == accepted.json()

            wrong_lease = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=headers,
                json={**legacy_wire, "lease_id": "wrong-lease"},
            )
            assert wrong_lease.status_code == 409
            assert wrong_lease.json()["error"]["code"] == "runner_command_lease_mismatch"

            replacement = await service.register(
                registration,
                bootstrap_token=RUNNER_BOOTSTRAP_TOKEN,
            )
            foreign_headers = {
                "Authorization": f"Bearer {replacement.token}",
                "X-RiftX-Node-ID": registration.node_id,
                "X-RiftX-Runner-Instance-ID": replacement.principal.instance_id,
                "X-RiftX-Runner-Epoch": str(replacement.principal.epoch),
            }
            wrong_principal = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=foreign_headers,
                json=legacy_wire,
            )
            assert wrong_principal.status_code == 401
            assert (
                wrong_principal.json()["error"]["code"]
                == "runner_command_scope_mismatch"
            )

        async with runtime.control_plane.database.session_factory() as session:
            record = await session.get(RunnerCommandRecord, command.id)
            ownership = await session.get(RunnerCommandOwnershipRecord, command.id)
            assert record is not None and ownership is not None
            assert record.status == RunnerCommandStatus.LEASED.value
            assert record.lease_id == command.lease_id
            assert record.completed_at is None
            assert record.state_version == 1
            assert ownership.verification_state == "quarantined"
            assert ownership.reconciliation_state == "untouched"
            assert ownership.replacement_command_id is None
            assert record.result_json["legacy_result"] == "preserved"
            evidence = record.result_json["_riftx_legacy_stop_ack_evidence"]
            assert evidence["ack"] == ack
            assert evidence["principal"] == original.principal.model_dump(mode="json")
            assert evidence["lease_id"] == command.lease_id
            assert (
                await session.scalar(
                    select(func.count()).select_from(RunnerStopReceiptRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(RunnerStopProjectionRecord)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(WorkflowSignalIntentRecord)
                )
                == 0
            )
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_legacy_finish_route_policy_denial_precedes_service_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _build_runtime(tmp_path)
    service = runtime.control_plane.runner_control_service
    registration = await service.register(
        NodeRegistration(
            node_id="legacy-policy-node",
            name="Legacy Policy Runner",
            platform="linux",
            architecture="x86_64",
            runner_version="2.9.0",
            capabilities=(),
        ),
        bootstrap_token=RUNNER_BOOTSTRAP_TOKEN,
    )
    service.record_legacy_stop_ack = AsyncMock()  # type: ignore[method-assign]

    def deny_policy(*args: object, **kwargs: object) -> None:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.MODE_MISMATCH)

    monkeypatch.setattr(
        runner_control_route,
        "require_run_kind_effect_policy",
        deny_policy,
    )
    headers = {
        "Authorization": f"Bearer {registration.token}",
        "X-RiftX-Node-ID": "legacy-policy-node",
        "X-RiftX-Runner-Instance-ID": registration.principal.instance_id,
        "X-RiftX-Runner-Epoch": str(registration.principal.epoch),
    }

    try:
        async for client in _client(runtime.control_plane):
            denied = await client.post(
                "/api/v1/runner/commands/legacy-policy-command/finish",
                headers=headers,
                json={
                    "lease_id": "legacy-policy-lease",
                    "succeeded": True,
                    "result": {},
                },
            )
            assert denied.status_code == 409
            assert denied.json()["error"]["code"] == "run_kind_effect_policy_denied"
        service.record_legacy_stop_ack.assert_not_awaited()
    finally:
        await runtime.control_plane.close()
