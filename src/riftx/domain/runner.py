"""Durable remote Runner identity, ownership, and command protocol models."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeGuard

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from .base import DomainModel, new_id, utc_now
from .enums import (
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerOperationFamily,
    RunnerResourceKind,
)

RUNNER_EFFECT_BINDING_SCHEMA_VERSION: Literal[
    "riftx.runner-effect-binding/v1"
] = "riftx.runner-effect-binding/v1"
RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION: Literal[
    "riftx.runner-command-ownership/v1"
] = "riftx.runner-command-ownership/v1"
RUNNER_OUTPUT_CONTRACT_SCHEMA_VERSION: Literal[
    "riftx.runner-output-contract/v1"
] = "riftx.runner-output-contract/v1"
RUNNER_STOP_RECEIPT_SCHEMA_VERSION: Literal[
    "riftx.runner-stop-receipt/v1"
] = "riftx.runner-stop-receipt/v1"
RUNNER_COMMAND_OWNERSHIP_CAPABILITY = "runner_command_ownership_v1"
RUNNER_STOP_ACK_EXECUTION_SCHEMA = "riftx.runner-stop-ack/execution/v1"
RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA = "riftx.runner-stop-ack/target-http/v1"
RUNNER_STOP_ACK_BROWSER_SCHEMA = "riftx.runner-stop-ack/browser/v1"
RUNNER_STOP_ACK_TERMINAL_SCHEMA = "riftx.runner-stop-ack/terminal/v1"


@dataclass(frozen=True, slots=True)
class RunnerCommandProtocol:
    """Closed protocol registry entry shared by both sides of Runner RPC."""

    operation_family: RunnerOperationFamily
    resource_kind: RunnerResourceKind
    result_schema: str
    output_mode: Literal["none", "command", "execution"]
    stop_ack_schema: str | None = None


RUNNER_COMMAND_PROTOCOLS: Mapping[RunnerCommandKind, RunnerCommandProtocol] = (
    MappingProxyType(
        {
            RunnerCommandKind.EXECUTE: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.EXECUTION,
                resource_kind=RunnerResourceKind.EXECUTION,
                result_schema="riftx.runner-result/execution-start/v1",
                output_mode="execution",
            ),
            RunnerCommandKind.CANCEL: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.EXECUTION,
                result_schema="riftx.runner-result/execution-stop/v1",
                output_mode="none",
                stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            ),
            RunnerCommandKind.TARGET_HTTP: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.TARGET_HTTP,
                resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
                result_schema="riftx.runner-result/target-http/v1",
                output_mode="command",
            ),
            RunnerCommandKind.TARGET_HTTP_CANCEL: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.TARGET_HTTP_INTENT,
                result_schema="riftx.runner-result/target-http-stop/v1",
                output_mode="none",
                stop_ack_schema=RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
            ),
            RunnerCommandKind.BROWSER: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.BROWSER,
                resource_kind=RunnerResourceKind.BROWSER_SESSION,
                result_schema="riftx.runner-result/browser/v1",
                output_mode="command",
            ),
            RunnerCommandKind.BROWSER_CLOSE: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.BROWSER_SESSION,
                result_schema="riftx.runner-result/browser-stop/v1",
                output_mode="none",
                stop_ack_schema=RUNNER_STOP_ACK_BROWSER_SCHEMA,
            ),
            RunnerCommandKind.TERMINAL_START: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.TERMINAL,
                resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                result_schema="riftx.runner-result/terminal-start/v1",
                output_mode="execution",
            ),
            RunnerCommandKind.TERMINAL_WRITE: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.TERMINAL,
                resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                result_schema="riftx.runner-result/terminal-operation/v1",
                output_mode="none",
            ),
            RunnerCommandKind.TERMINAL_RESIZE: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.TERMINAL,
                resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                result_schema="riftx.runner-result/terminal-operation/v1",
                output_mode="none",
            ),
            RunnerCommandKind.TERMINAL_INTERRUPT: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.TERMINAL,
                resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                result_schema="riftx.runner-result/terminal-operation/v1",
                output_mode="none",
            ),
            RunnerCommandKind.TERMINAL_CLOSE: RunnerCommandProtocol(
                operation_family=RunnerOperationFamily.SAFETY_STOP,
                resource_kind=RunnerResourceKind.TERMINAL_SESSION,
                result_schema="riftx.runner-result/terminal-stop/v1",
                output_mode="none",
                stop_ack_schema=RUNNER_STOP_ACK_TERMINAL_SCHEMA,
            ),
        }
    )
)

_BROWSER_NON_CLOSE_OPERATIONS = frozenset(
    {"open", "observe", "act", "takeover", "release"}
)


class RunnerPrincipal(DomainModel):
    """Immutable identity of one owner generation for a logical Runner node."""

    model_config = ConfigDict(frozen=True)

    instance_id: str = Field(min_length=1, max_length=64)
    epoch: int = Field(ge=1)


def _legacy_principal() -> RunnerPrincipal:
    """Keep direct credential construction valid without sharing an identity."""

    return RunnerPrincipal(instance_id=new_id(), epoch=1)


class RunnerCredential(DomainModel):
    node_id: str = Field(min_length=1, max_length=64)
    principal: RunnerPrincipal = Field(default_factory=_legacy_principal)
    token_hash: str = Field(min_length=64, max_length=64)
    token_prefix: str = Field(min_length=1, max_length=16)
    protocol_capabilities: tuple[str, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)
    rotated_at: AwareDatetime = Field(default_factory=utc_now)
    revoked_at: AwareDatetime | None = None


class RunnerOutputContract(DomainModel):
    """Typed output and stop-ack limits that a payload cannot weaken."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["riftx.runner-output-contract/v1"] = (
        RUNNER_OUTPUT_CONTRACT_SCHEMA_VERSION
    )
    max_result_bytes: int = Field(default=64 * 1024, ge=0, le=1024 * 1024)
    max_output_bytes: int = Field(default=0, ge=0, le=100_000_000)
    allowed_streams: tuple[Literal["command", "stdout", "stderr"], ...] = ()
    result_schema: str = Field(default="riftx.runner-result/none", min_length=1, max_length=128)
    stop_ack_schema: str | None = Field(default=None, min_length=1, max_length=128)
    contract_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )

    @model_validator(mode="after")
    def validate_contract(self) -> RunnerOutputContract:
        normalized_streams = tuple(sorted(set(self.allowed_streams)))
        if normalized_streams != self.allowed_streams:
            object.__setattr__(self, "allowed_streams", normalized_streams)
        if not normalized_streams and self.max_output_bytes != 0:
            raise ValueError("Runner output bytes require at least one allowed stream")
        if self.stop_ack_schema is not None and self.max_result_bytes == 0:
            raise ValueError("stop acknowledgements require a bounded result body")
        expected = runner_output_contract_digest(self)
        if self.contract_digest:
            if not hmac.compare_digest(self.contract_digest, expected):
                raise ValueError("Runner output contract digest does not match")
        else:
            object.__setattr__(self, "contract_digest", expected)
        return self


class RunnerEffectBinding(DomainModel):
    """Immutable authority binding shared by every command callback boundary."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    schema_version: Literal["riftx.runner-effect-binding/v1"] = (
        RUNNER_EFFECT_BINDING_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1, max_length=64)
    run_kind: RunKind
    node_id: str = Field(min_length=1, max_length=64)
    target: RunnerPrincipal
    origin: RunnerCommandOrigin
    operation_family: RunnerOperationFamily
    execution_id: str | None = Field(default=None, min_length=1, max_length=128)
    resource_kind: RunnerResourceKind
    resource_id: str = Field(min_length=1, max_length=128)
    audit_id: str | None = Field(default=None, min_length=1, max_length=128)
    plan_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    binding_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_binding(self) -> RunnerEffectBinding:
        if self.run_kind is RunKind.GENERAL:
            if self.audit_id is not None or self.plan_digest is not None:
                raise ValueError("General Runner effect binding cannot carry Audit ownership")
        elif self.run_kind is RunKind.CODE_AUDIT:
            if self.audit_id is None or self.plan_digest is None:
                raise ValueError("Code Audit Runner effect binding requires Audit plan ownership")
        else:  # pragma: no cover - enum is closed, retained as a fail-closed guard
            raise ValueError("unknown Run kind in Runner effect binding")
        if self.resource_kind is RunnerResourceKind.EXECUTION:
            if self.execution_id is None or self.resource_id != self.execution_id:
                raise ValueError(
                    "execution Runner effect binding requires matching execution identity"
                )
        expected = runner_effect_binding_digest(self)
        if self.binding_digest:
            if not hmac.compare_digest(self.binding_digest, expected):
                raise ValueError("Runner effect binding digest does not match")
        else:
            object.__setattr__(self, "binding_digest", expected)
        return self


class RunnerCommandOwnership(DomainModel):
    """Immutable command envelope bound to one verified effect identity."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["riftx.runner-command-ownership/v1"] = (
        RUNNER_COMMAND_OWNERSHIP_SCHEMA_VERSION
    )
    command_id: str = Field(min_length=1, max_length=64)
    effect_binding: RunnerEffectBinding
    operation: RunnerCommandKind
    operation_family: RunnerOperationFamily
    payload_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    output_contract: RunnerOutputContract
    output_contract_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    envelope_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_envelope(self) -> RunnerCommandOwnership:
        if self.operation_family is not self.effect_binding.operation_family:
            raise ValueError("Runner command family does not match its effect binding")
        contract_digest = self.output_contract.contract_digest
        if self.output_contract_digest:
            if not hmac.compare_digest(self.output_contract_digest, contract_digest):
                raise ValueError("Runner command output contract digest does not match")
        else:
            object.__setattr__(self, "output_contract_digest", contract_digest)
        expected = runner_command_envelope_digest(self)
        if self.envelope_digest:
            if not hmac.compare_digest(self.envelope_digest, expected):
                raise ValueError("Runner command envelope digest does not match")
        else:
            object.__setattr__(self, "envelope_digest", expected)
        return self


class RunnerCommand(DomainModel):
    id: str = Field(default_factory=new_id)
    node_id: str = Field(min_length=1, max_length=64)
    kind: RunnerCommandKind
    idempotency_key: str = Field(min_length=1, max_length=255)
    # Legacy rows remain readable only in explicit quarantine. New commands
    # are admitted by RunnerControlService with a verified typed envelope.
    target: RunnerPrincipal | None = None
    ownership: RunnerCommandOwnership | None = Field(default=None, frozen=True)
    ownership_state: RunnerCommandOwnershipState = RunnerCommandOwnershipState.QUARANTINED
    quarantine_reason: str = Field(
        default="legacy_ownership_missing",
        max_length=255,
    )
    payload: dict[str, object] = Field(default_factory=dict)
    status: RunnerCommandStatus = RunnerCommandStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    lease_id: str | None = None
    lease_expires_at: AwareDatetime | None = None
    result: dict[str, object] = Field(default_factory=dict)
    error: str = ""
    state_version: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_ownership_state(self) -> RunnerCommand:
        if self.ownership_state is RunnerCommandOwnershipState.VERIFIED:
            if self.ownership is None:
                raise ValueError("verified Runner command requires ownership")
            if self.target is None:
                raise ValueError("verified Runner command requires a target principal")
            binding = self.ownership.effect_binding
            if self.ownership.command_id != self.id:
                raise ValueError("Runner command ID does not match immutable ownership")
            if self.node_id != binding.node_id or self.target != binding.target:
                raise ValueError("Runner command target does not match immutable ownership")
            if self.kind is not self.ownership.operation:
                raise ValueError("Runner command kind does not match immutable ownership")
            expected_payload_digest = runner_payload_digest(self.payload)
            if not hmac.compare_digest(
                self.ownership.payload_digest,
                expected_payload_digest,
            ):
                raise ValueError("Runner command payload digest does not match")
            if self.quarantine_reason:
                raise ValueError("verified Runner command cannot carry a quarantine reason")
        elif not self.quarantine_reason:
            raise ValueError("quarantined Runner command requires a reason")
        return self


class RunnerStopReceipt(DomainModel):
    """Immutable proof that one typed Runner resource was affirmatively stopped."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    schema_version: Literal["riftx.runner-stop-receipt/v1"] = (
        RUNNER_STOP_RECEIPT_SCHEMA_VERSION
    )
    command_id: str = Field(min_length=1, max_length=64)
    effect_binding_id: str = Field(min_length=1, max_length=64)
    envelope_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    binding_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    operation: RunnerCommandKind
    operation_family: RunnerOperationFamily
    resource_kind: RunnerResourceKind
    resource_id: str = Field(min_length=1, max_length=128)
    execution_id: str | None = Field(default=None, min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=64)
    principal: RunnerPrincipal
    ack_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime = Field(default_factory=utc_now)


def runner_payload_digest(payload: dict[str, object]) -> str:
    return _domain_digest("riftx.runner-command-payload/v1", payload)


def runner_effect_binding_digest(binding: RunnerEffectBinding) -> str:
    payload = {
        "schema_version": binding.schema_version,
        "run_id": binding.run_id,
        "run_kind": binding.run_kind.value,
        "node_id": binding.node_id,
        "target": binding.target.model_dump(mode="json"),
        "origin": binding.origin.value,
        "operation_family": binding.operation_family.value,
        "execution_id": binding.execution_id,
        "resource_kind": binding.resource_kind.value,
        "resource_id": binding.resource_id,
        "audit_id": binding.audit_id,
        "plan_digest": binding.plan_digest,
    }
    return _domain_digest("riftx.runner-effect-binding/v1", payload)


def runner_output_contract_digest(contract: RunnerOutputContract) -> str:
    return _domain_digest(
        "riftx.runner-output-contract/v1",
        {
            "schema_version": contract.schema_version,
            "max_result_bytes": contract.max_result_bytes,
            "max_output_bytes": contract.max_output_bytes,
            "allowed_streams": list(contract.allowed_streams),
            "result_schema": contract.result_schema,
            "stop_ack_schema": contract.stop_ack_schema,
        },
    )


def runner_command_envelope_digest(ownership: RunnerCommandOwnership) -> str:
    return _domain_digest(
        "riftx.runner-command-envelope/v1",
        {
            "schema_version": ownership.schema_version,
            "command_id": ownership.command_id,
            "effect_binding_id": ownership.effect_binding.id,
            "binding_digest": ownership.effect_binding.binding_digest,
            "operation": ownership.operation.value,
            "operation_family": ownership.operation_family.value,
            "payload_digest": ownership.payload_digest,
            "output_contract_digest": ownership.output_contract_digest,
        },
    )


def runner_stop_ack_digest(result: dict[str, object]) -> str:
    return _domain_digest("riftx.runner-stop-ack/v1", result)


def runner_command_protocol(kind: RunnerCommandKind) -> RunnerCommandProtocol:
    """Return the closed protocol entry for a Runner command kind."""

    return RUNNER_COMMAND_PROTOCOLS[kind]


def runner_command_payload_binding_invalid_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    *,
    authoritative_execution_key: str | None = None,
) -> tuple[str, ...]:
    """Pure command-shape and immutable-binding validation shared by both peers."""

    invalid: list[str] = []
    protocol = runner_command_protocol(kind)
    if binding.operation_family is not protocol.operation_family:
        invalid.append("binding.operation_family")
    if binding.resource_kind is not protocol.resource_kind:
        invalid.append("binding.resource_kind")

    if kind in {RunnerCommandKind.EXECUTE, RunnerCommandKind.CANCEL}:
        _validate_execution_payload_fields(
            kind,
            binding,
            payload,
            invalid,
            authoritative_execution_key=authoritative_execution_key,
        )
    elif kind in {
        RunnerCommandKind.TERMINAL_START,
        RunnerCommandKind.TERMINAL_WRITE,
        RunnerCommandKind.TERMINAL_RESIZE,
        RunnerCommandKind.TERMINAL_INTERRUPT,
        RunnerCommandKind.TERMINAL_CLOSE,
    }:
        _validate_terminal_payload_fields(
            kind,
            binding,
            payload,
            invalid,
            authoritative_execution_key=authoritative_execution_key,
        )
    elif kind in {
        RunnerCommandKind.TARGET_HTTP,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
    }:
        _validate_target_http_payload_fields(kind, binding, payload, invalid)
    elif kind in {RunnerCommandKind.BROWSER, RunnerCommandKind.BROWSER_CLOSE}:
        _validate_browser_payload_fields(kind, binding, payload, invalid)
    else:  # pragma: no cover - registry parity tests close this enum
        invalid.append("operation")
    return tuple(sorted(set(invalid)))


def runner_success_result_invalid_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    result: dict[str, object],
) -> tuple[str, ...]:
    """Validate the minimum successful result body for every command kind."""

    invalid: list[str] = []
    if kind is RunnerCommandKind.EXECUTE:
        _validate_execution_start_result(binding, payload, result, invalid)
    elif kind is RunnerCommandKind.TERMINAL_START:
        raw_result = result.get("result")
        if not isinstance(raw_result, dict):
            invalid.append("result")
        else:
            _validate_terminal_start_result(binding, raw_result, invalid)
    elif kind in {
        RunnerCommandKind.TERMINAL_WRITE,
        RunnerCommandKind.TERMINAL_RESIZE,
        RunnerCommandKind.TERMINAL_INTERRUPT,
    }:
        _validate_terminal_operation_result(kind, binding, result, invalid)
    elif kind is RunnerCommandKind.TARGET_HTTP:
        _validate_target_http_result(payload, result, invalid)
    elif kind is RunnerCommandKind.BROWSER:
        _validate_browser_result(binding, result, invalid, require_closed=False)
    elif kind in {RunnerCommandKind.CANCEL, RunnerCommandKind.TERMINAL_CLOSE}:
        _validate_execution_stop_result(kind, binding, payload, result, invalid)
    elif kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        _validate_target_http_stop_result(binding, result, invalid)
    elif kind is RunnerCommandKind.BROWSER_CLOSE:
        _validate_browser_result(binding, result, invalid, require_closed=True)
    else:  # pragma: no cover - registry parity tests close this enum
        invalid.append("result")
    return tuple(sorted(set(invalid)))


def _validate_execution_payload_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    invalid: list[str],
    *,
    authoritative_execution_key: str | None,
) -> None:
    if binding.execution_id != binding.resource_id:
        invalid.append("binding.execution_id")
    if payload.get("execution_id") != binding.resource_id:
        invalid.append("execution_id")
    if kind is RunnerCommandKind.EXECUTE:
        request = payload.get("request")
        if not isinstance(request, dict):
            invalid.append("request")
            return
        if request.get("run_id") != binding.run_id:
            invalid.append("request.run_id")
        if request.get("node_id") != binding.node_id:
            invalid.append("request.node_id")
        if request.get("runner_principal") != binding.target.model_dump(mode="json"):
            invalid.append("request.runner_principal")
        request_execution_id = request.get("execution_id")
        if request_execution_id not in {None, binding.execution_id}:
            invalid.append("request.execution_id")
        execution_key = request.get("execution_key")
        if not _is_non_empty_string(execution_key):
            invalid.append("request.execution_key")
        elif (
            authoritative_execution_key is not None
            and execution_key != authoritative_execution_key
        ):
            invalid.append("request.execution_key")
        return

    execution_key = payload.get("execution_key")
    if not _is_non_empty_string(execution_key):
        invalid.append("execution_key")
    elif (
        authoritative_execution_key is not None
        and execution_key != authoritative_execution_key
    ):
        invalid.append("execution_key")
    if "request" in payload:
        invalid.append("request")


def _validate_terminal_payload_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    invalid: list[str],
    *,
    authoritative_execution_key: str | None,
) -> None:
    if binding.execution_id is None:
        invalid.append("binding.execution_id")
    if payload.get("session_id") != binding.resource_id:
        invalid.append("session_id")
    if payload.get("execution_id") != binding.execution_id:
        invalid.append("execution_id")

    if kind is RunnerCommandKind.TERMINAL_START:
        request = payload.get("request")
        if not isinstance(request, dict):
            invalid.append("request")
            return
        if request.get("run_id") != binding.run_id:
            invalid.append("request.run_id")
        if request.get("node_id") != binding.node_id:
            invalid.append("request.node_id")
        if request.get("runner_principal") != binding.target.model_dump(mode="json"):
            invalid.append("request.runner_principal")
        request_session_id = request.get("session_id")
        if request_session_id not in {None, binding.resource_id}:
            invalid.append("request.session_id")
        request_execution_id = request.get("execution_id")
        if request_execution_id not in {None, binding.execution_id}:
            invalid.append("request.execution_id")
        request_execution_key = request.get("execution_key")
        resolved_execution_key = (
            request_execution_key
            if _is_non_empty_string(request_execution_key)
            else f"terminal:{binding.resource_id}"
        )
        if request_execution_key is not None and not _is_non_empty_string(
            request_execution_key
        ):
            invalid.append("request.execution_key")
        if (
            authoritative_execution_key is not None
            and resolved_execution_key != authoritative_execution_key
        ):
            invalid.append("request.execution_key")
        return

    if kind is RunnerCommandKind.TERMINAL_CLOSE:
        raw_execution_key = payload.get("execution_key")
        execution_key = (
            raw_execution_key
            if _is_non_empty_string(raw_execution_key)
            else f"terminal:{binding.resource_id}"
        )
        if raw_execution_key is not None and not _is_non_empty_string(
            raw_execution_key
        ):
            invalid.append("execution_key")
        if (
            authoritative_execution_key is not None
            and execution_key != authoritative_execution_key
        ):
            invalid.append("execution_key")
        if "request" in payload:
            invalid.append("request")
        return

    if not _is_non_empty_string(payload.get("operation_id")):
        invalid.append("operation_id")
    if kind is RunnerCommandKind.TERMINAL_WRITE:
        if not _is_non_empty_string(payload.get("data")):
            invalid.append("data")
    elif kind is RunnerCommandKind.TERMINAL_RESIZE:
        if not _is_positive_int(payload.get("cols")):
            invalid.append("cols")
        if not _is_positive_int(payload.get("rows")):
            invalid.append("rows")


def _validate_target_http_payload_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    invalid: list[str],
) -> None:
    if binding.execution_id is not None:
        invalid.append("binding.execution_id")
    if kind is RunnerCommandKind.TARGET_HTTP:
        launch = payload.get("launch")
        if not isinstance(launch, dict):
            invalid.append("launch")
            return
        if launch.get("run_id") != binding.run_id:
            invalid.append("launch.run_id")
        if launch.get("node_id") != binding.node_id:
            invalid.append("launch.node_id")
        if launch.get("tool_call_id") != binding.resource_id:
            invalid.append("launch.tool_call_id")
        if not _is_non_empty_string(launch.get("session_id")):
            invalid.append("launch.session_id")
        if not isinstance(launch.get("scope"), dict):
            invalid.append("launch.scope")
        request = launch.get("request")
        if not isinstance(request, dict):
            invalid.append("launch.request")
        elif not _is_non_empty_string(request.get("execution_key")):
            invalid.append("launch.request.execution_key")
        if "tool_call_ids" in payload or "run_id" in payload:
            invalid.append("launch")
        return

    if "launch" in payload:
        invalid.append("launch")
    if payload.get("run_id") != binding.run_id:
        invalid.append("run_id")
    if payload.get("tool_call_ids") != [binding.resource_id]:
        invalid.append("tool_call_ids")


def _validate_browser_payload_fields(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    invalid: list[str],
) -> None:
    if binding.execution_id is not None:
        invalid.append("binding.execution_id")
    operation = payload.get("operation")
    if kind is RunnerCommandKind.BROWSER_CLOSE:
        if operation != "close":
            invalid.append("operation")
    elif operation not in _BROWSER_NON_CLOSE_OPERATIONS:
        invalid.append("operation")

    raw_command = payload.get("command")
    if not isinstance(raw_command, dict):
        invalid.append("command")
        return
    if raw_command.get("session_id") != binding.resource_id:
        invalid.append("command.session_id")
    raw_session = raw_command.get("session")
    if raw_session is not None and not isinstance(raw_session, dict):
        invalid.append("command.session")
        raw_session = None
    raw_run_id = raw_command.get("run_id")
    raw_node_id = raw_command.get("node_id")
    if isinstance(raw_session, dict):
        if raw_session.get("id") != binding.resource_id:
            invalid.append("command.session.id")
        if raw_run_id is None:
            raw_run_id = raw_session.get("run_id")
        if raw_node_id is None:
            raw_node_id = raw_session.get("node_id")
    if raw_run_id != binding.run_id:
        invalid.append("command.run_id")
    if raw_node_id != binding.node_id:
        invalid.append("command.node_id")


def _validate_execution_start_result(
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    result: dict[str, object],
    invalid: list[str],
) -> None:
    if result.get("execution_id") != binding.resource_id:
        invalid.append("execution_id")
    if result.get("status") == "suppressed":
        if result.get("suppressed_by_cancellation") is not True:
            invalid.append("suppressed_by_cancellation")
        if result.get("physical_stop_confirmed") is not False:
            invalid.append("physical_stop_confirmed")
        return
    request = payload.get("request")
    expected_execution_key = request.get("execution_key") if isinstance(request, dict) else None
    if result.get("local_execution_id") != binding.resource_id:
        invalid.append("local_execution_id")
    if result.get("execution_key") != expected_execution_key:
        invalid.append("execution_key")
    if result.get("owner") != binding.target.model_dump(mode="json"):
        invalid.append("owner")
    if not _is_non_empty_string(result.get("status")):
        invalid.append("status")


def _validate_terminal_start_result(
    binding: RunnerEffectBinding,
    result: dict[str, object],
    invalid: list[str],
) -> None:
    if result.get("execution_id") != binding.execution_id:
        invalid.append("result.execution_id")
    if result.get("status") == "suppressed":
        if result.get("suppressed_by_cancellation") is not True:
            invalid.append("result.suppressed_by_cancellation")
        if result.get("physical_stop_confirmed") is not False:
            invalid.append("result.physical_stop_confirmed")
        return
    if result.get("session_id") != binding.resource_id:
        invalid.append("result.session_id")
    if not _is_non_empty_string(result.get("status")):
        invalid.append("result.status")
    if not isinstance(result.get("duplicate"), bool):
        invalid.append("result.duplicate")


def _validate_terminal_operation_result(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    result: dict[str, object],
    invalid: list[str],
) -> None:
    raw_result = result.get("result")
    if not isinstance(raw_result, dict):
        invalid.append("result")
        return
    if raw_result.get("session_id") != binding.resource_id:
        invalid.append("result.session_id")
    if not _is_non_empty_string(raw_result.get("operation_id")):
        invalid.append("result.operation_id")
    if not isinstance(raw_result.get("duplicate"), bool):
        invalid.append("result.duplicate")
    if kind is RunnerCommandKind.TERMINAL_WRITE:
        if not _is_non_negative_int(raw_result.get("bytes_written")):
            invalid.append("result.bytes_written")
    elif kind is RunnerCommandKind.TERMINAL_RESIZE:
        if not _is_positive_int(raw_result.get("cols")):
            invalid.append("result.cols")
        if not _is_positive_int(raw_result.get("rows")):
            invalid.append("result.rows")
    elif raw_result.get("interrupted") is not True:
        invalid.append("result.interrupted")


def _validate_target_http_result(
    payload: dict[str, object],
    result: dict[str, object],
    invalid: list[str],
) -> None:
    raw_result = result.get("result")
    if not isinstance(raw_result, dict):
        invalid.append("result")
        return
    launch = payload.get("launch")
    request = launch.get("request") if isinstance(launch, dict) else None
    expected_execution_key = request.get("execution_key") if isinstance(request, dict) else None
    if not _is_non_empty_string(raw_result.get("request_id")):
        invalid.append("result.request_id")
    if raw_result.get("execution_key") != expected_execution_key:
        invalid.append("result.execution_key")
    request_hash = raw_result.get("request_hash")
    if not (
        isinstance(request_hash, str)
        and len(request_hash) == 64
        and all(character in "0123456789abcdef" for character in request_hash)
    ):
        invalid.append("result.request_hash")
    status_code = raw_result.get("status_code")
    if not (_is_non_negative_int(status_code) and 100 <= status_code <= 599):
        invalid.append("result.status_code")
    if not _is_non_negative_int(raw_result.get("elapsed_ms")):
        invalid.append("result.elapsed_ms")
    if not _is_non_empty_string(raw_result.get("final_url")):
        invalid.append("result.final_url")
    if not isinstance(raw_result.get("truncated"), bool):
        invalid.append("result.truncated")


def _validate_browser_result(
    binding: RunnerEffectBinding,
    result: dict[str, object],
    invalid: list[str],
    *,
    require_closed: bool,
) -> None:
    raw_result = result.get("result")
    raw_session = raw_result.get("session") if isinstance(raw_result, dict) else None
    if not isinstance(raw_session, dict):
        invalid.append("result.session")
        return
    if raw_session.get("id") != binding.resource_id:
        invalid.append("result.session.id")
    if raw_session.get("run_id") != binding.run_id:
        invalid.append("result.session.run_id")
    if raw_session.get("node_id") != binding.node_id:
        invalid.append("result.session.node_id")
    status = raw_session.get("status")
    if require_closed:
        if status != "closed":
            invalid.append("result.session.status")
    elif not _is_non_empty_string(status):
        invalid.append("result.session.status")


def _validate_execution_stop_result(
    kind: RunnerCommandKind,
    binding: RunnerEffectBinding,
    payload: dict[str, object],
    result: dict[str, object],
    invalid: list[str],
) -> None:
    if result.get("execution_id") != binding.execution_id:
        invalid.append("execution_id")
    if result.get("local_execution_id") != binding.execution_id:
        invalid.append("local_execution_id")
    if result.get("execution_key") != payload.get("execution_key"):
        invalid.append("execution_key")
    if result.get("owner") != binding.target.model_dump(mode="json"):
        invalid.append("owner")
    if result.get("status") != "cancelled":
        invalid.append("status")
    if result.get("physical_stop_confirmed") is not True:
        invalid.append("physical_stop_confirmed")
    if (
        kind is RunnerCommandKind.TERMINAL_CLOSE
        and result.get("session_id") != binding.resource_id
    ):
        invalid.append("session_id")


def _validate_target_http_stop_result(
    binding: RunnerEffectBinding,
    result: dict[str, object],
    invalid: list[str],
) -> None:
    outcomes = result.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 1:
        invalid.append("outcomes")
        return
    outcome = outcomes[0]
    if not isinstance(outcome, dict):
        invalid.append("outcomes[0]")
        return
    if outcome.get("tool_call_id") != binding.resource_id:
        invalid.append("outcomes[0].tool_call_id")
    if outcome.get("confirmed") is not True:
        invalid.append("outcomes[0].confirmed")


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_non_negative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> bool:
    return _is_non_negative_int(value) and value > 0


def _domain_digest(domain: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()
