from __future__ import annotations

import importlib
from types import MappingProxyType, SimpleNamespace

import pytest

import riftx.application.run_kind_effects as run_kind_effects
from riftx.api.policy import ROUTE_POLICIES, RouteAuthorization, RouteEffect, RoutePolicy
from riftx.application.errors import ApplicationConflictError
from riftx.application.run_kind_effects import (
    API_ROUTE_EFFECT_BINDINGS,
    MANAGED_EFFECT_ENTRYPOINTS,
    MANAGED_EFFECT_TYPES,
    RUN_KIND_EFFECT_POLICIES,
    AuditAlternativeDisposition,
    EffectEntrypointSurface,
    EffectMode,
    EffectOrigin,
    EffectOwnerKind,
    GlobalEffectOwnership,
    LegacyRunnerCommandEffectOwnership,
    ManagedEffectEntrypoint,
    ManagedEffectType,
    ManagedOutOfScopeMethod,
    OperationEffect,
    OwnershipClaim,
    OwnershipResolverKind,
    PolicyDenialReason,
    PreflightJobEffectOwnership,
    RunEffectFamily,
    RunEffectOperation,
    RunEffectOwnership,
    RunKindEffectInventoryError,
    RunKindEffectPolicy,
    RunKindEffectPolicyDenied,
    global_effect_ownership_for_local_principal,
    require_run_kind_effect_policy,
    resolve_run_kind_effect_policy,
    validate_api_route_effect_inventory,
    validate_managed_effect_inventory,
)
from riftx.application.workflow_router import RunWorkflowControlRouter
from riftx.domain import (
    LocalPrincipal,
    OperatorCapability,
    RunKind,
    RunnerCommandOrigin,
    RunnerOperationFamily,
)

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_GLOBAL_OWNER = GlobalEffectOwnership(administrative_scope_digest=_DIGEST)


class _InventoryAsyncCanary:
    async def mutate(self) -> None:
        return None


class _InventorySyncCanary:
    def mutate(self) -> None:
        return None


class _RouterRuns:
    def __init__(
        self,
        kind: RunKind,
        *,
        workflow_id: str | None = "historical-prefix-general-run",
    ) -> None:
        self.kind = kind
        self.workflow_id = workflow_id

    async def get_kind(self, run_id: str) -> RunKind | None:
        del run_id
        return self.kind

    async def get(self, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=run_id,
            kind=self.kind,
            temporal_workflow_id=self.workflow_id,
        )


class _RouterAudits:
    async def get(self, audit_id: str) -> None:
        del audit_id
        return None


class _GeneralWorkflowSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.workflow_ids: list[str | None] = []

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self.calls.append(("pause", run_id))
        self.workflow_ids.append(workflow_id)


def _run_owner(run_kind: RunKind | str, **overrides: object) -> RunEffectOwnership:
    values: dict[str, object] = {
        "run_id": "run-owner",
        "run_kind": run_kind,
        "execution_id": "execution-owner",
        "resource_kind": "test-resource",
        "resource_id": "resource-owner",
        "node_id": "node-owner",
        "runner_principal": ("runner-owner", 1),
        "runner_command_id": "command-owner",
    }
    if run_kind == RunKind.CODE_AUDIT:
        values.update(audit_id="audit-owner", plan_digest=_DIGEST)
    values.update(overrides)
    return RunEffectOwnership(**values)  # type: ignore[arg-type]


def _preflight_owner() -> PreflightJobEffectOwnership:
    return PreflightJobEffectOwnership(
        preflight_job_id="preflight-owner",
        operator_principal_id="operator-owner",
        authorization_scope_digest=_DIGEST,
        request_digest=_OTHER_DIGEST,
        node_id="node-owner",
    )


def test_api_route_inventory_has_an_exact_run_kind_policy_for_every_route() -> None:
    validate_api_route_effect_inventory(ROUTE_POLICIES)

    assert set(API_ROUTE_EFFECT_BINDINGS) == set(ROUTE_POLICIES)
    for route_name, route_policy in ROUTE_POLICIES.items():
        binding = API_ROUTE_EFFECT_BINDINGS[route_name]
        policy = RUN_KIND_EFFECT_POLICIES[(binding.operation, binding.origin)]
        assert policy.required_effect.value == route_policy.effect.value
        assert policy.audit_alternative.disposition in AuditAlternativeDisposition


def test_local_principal_administrative_scope_is_canonical_and_identity_bound() -> None:
    principal = LocalPrincipal(
        id="operator-1",
        namespace_id="local-installation",
        capabilities=frozenset(
            {
                OperatorCapability.READ,
                OperatorCapability.CONTROL,
            }
        ),
    )

    first = global_effect_ownership_for_local_principal(principal)
    repeated = global_effect_ownership_for_local_principal(
        principal.model_copy(
            update={"capabilities": frozenset(reversed(tuple(principal.capabilities)))}
        )
    )
    foreign = global_effect_ownership_for_local_principal(
        principal.model_copy(update={"id": "operator-2"})
    )

    assert first == repeated
    assert first.administrative_scope_digest != foreign.administrative_scope_digest
    assert len(first.administrative_scope_digest) == 64


def test_api_route_inventory_fails_when_any_new_route_is_not_registered() -> None:
    augmented = {
        **ROUTE_POLICIES,
        "new_effect_canary": RoutePolicy(
            authorization=RouteAuthorization.LOCAL_OPERATOR,
            effect=RouteEffect.HOST_EXECUTION,
        ),
    }

    with pytest.raises(RunKindEffectInventoryError, match="new_effect_canary"):
        validate_api_route_effect_inventory(augmented)


def test_api_route_inventory_rejects_effect_or_origin_drift() -> None:
    effect_drift = {
        **ROUTE_POLICIES,
        "cancel_run": RoutePolicy(
            authorization=RouteAuthorization.LOCAL_OPERATOR,
            effect=RouteEffect.HOST_CONTROL,
        ),
    }
    with pytest.raises(RunKindEffectInventoryError, match="cancel_run:effect"):
        validate_api_route_effect_inventory(effect_drift)

    origin_drift = {
        **ROUTE_POLICIES,
        "cancel_run": RoutePolicy(
            authorization=RouteAuthorization.ADMIN_TOKEN,
            effect=RouteEffect.WORKFLOW_CONTROL,
        ),
    }
    with pytest.raises(RunKindEffectInventoryError, match="cancel_run:authorization"):
        validate_api_route_effect_inventory(origin_drift)


def test_unknown_operation_origin_kind_effect_and_mode_fail_closed_without_reflection() -> None:
    canary = "do-not-reflect-owner-secret"

    cases = (
        (
            lambda: resolve_run_kind_effect_policy(canary, EffectOrigin.LOCAL_OPERATOR_API),
            PolicyDenialReason.UNKNOWN_OPERATION,
        ),
        (
            lambda: resolve_run_kind_effect_policy(RunEffectOperation.CANCEL_RUN, canary),
            PolicyDenialReason.UNKNOWN_ORIGIN,
        ),
        (
            lambda: resolve_run_kind_effect_policy(
                RunEffectOperation.CANCEL_RUN,
                EffectOrigin.ADMIN_API,
            ),
            PolicyDenialReason.UNREGISTERED_OPERATION_ORIGIN,
        ),
        (
            lambda: require_run_kind_effect_policy(
                RunEffectOperation.CANCEL_RUN,
                EffectOrigin.LOCAL_OPERATOR_API,
                ownership=_run_owner(canary),
                effect=OperationEffect.WORKFLOW_CONTROL,
                mode=EffectMode.NORMAL,
            ),
            PolicyDenialReason.UNKNOWN_RUN_KIND,
        ),
        (
            lambda: require_run_kind_effect_policy(
                RunEffectOperation.CANCEL_RUN,
                EffectOrigin.LOCAL_OPERATOR_API,
                ownership=_run_owner(RunKind.GENERAL),
                effect=canary,
                mode=EffectMode.NORMAL,
            ),
            PolicyDenialReason.UNKNOWN_EFFECT,
        ),
        (
            lambda: require_run_kind_effect_policy(
                RunEffectOperation.CANCEL_RUN,
                EffectOrigin.LOCAL_OPERATOR_API,
                ownership=_run_owner(RunKind.GENERAL),
                effect=OperationEffect.WORKFLOW_CONTROL,
                mode=canary,
            ),
            PolicyDenialReason.UNKNOWN_MODE,
        ),
    )

    for invoke, reason in cases:
        with pytest.raises(RunKindEffectPolicyDenied) as captured:
            invoke()
        assert captured.value.reason is reason
        assert canary not in str(captured.value)


def test_unknown_owner_kind_fails_closed_without_reflection() -> None:
    canary = "do-not-reflect-owner-kind"

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.UPDATE_TOOL,
            EffectOrigin.ADMIN_API,
            ownership=SimpleNamespace(owner_kind=canary),
            effect=OperationEffect.DURABLE_WRITE,
            mode=EffectMode.GLOBAL,
        )

    assert captured.value.reason is PolicyDenialReason.UNKNOWN_OWNER_KIND
    assert canary not in str(captured.value)


@pytest.mark.parametrize("owner_kind", tuple(EffectOwnerKind))
def test_known_owner_discriminant_requires_the_exact_python_variant(
    owner_kind: EffectOwnerKind,
) -> None:
    fabricated = SimpleNamespace(
        owner_kind=owner_kind,
        run_id="fabricated-run",
        run_kind="fabricated-kind",
    )

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.UPDATE_TOOL,
            EffectOrigin.ADMIN_API,
            ownership=fabricated,
            effect=OperationEffect.DURABLE_WRITE,
            mode=EffectMode.GLOBAL,
        )

    assert captured.value.reason is PolicyDenialReason.OWNERSHIP_VARIANT_INVALID


def test_global_and_run_operations_never_fallback_across_owner_roots() -> None:
    for wrong_owner in (_run_owner(RunKind.GENERAL), _preflight_owner()):
        with pytest.raises(RunKindEffectPolicyDenied) as captured:
            require_run_kind_effect_policy(
                RunEffectOperation.UPDATE_TOOL,
                EffectOrigin.ADMIN_API,
                ownership=wrong_owner,
                effect=OperationEffect.DURABLE_WRITE,
                mode=EffectMode.GLOBAL,
            )
        assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    for wrong_owner in (_GLOBAL_OWNER, _preflight_owner()):
        with pytest.raises(RunKindEffectPolicyDenied) as captured:
            require_run_kind_effect_policy(
                RunEffectOperation.CANCEL_RUN,
                EffectOrigin.LOCAL_OPERATOR_API,
                ownership=wrong_owner,
                effect=OperationEffect.WORKFLOW_CONTROL,
                mode=EffectMode.NORMAL,
            )
        assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH


def test_preflight_routes_and_services_use_only_their_exact_independent_owner() -> None:
    create_operations = (
        (
            RunEffectOperation.CREATE_AUDIT_PREFLIGHT,
            EffectOrigin.LOCAL_OPERATOR_API,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CREATE,
            EffectOrigin.APPLICATION_SERVICE,
        ),
    )
    for operation, origin in create_operations:
        policy = require_run_kind_effect_policy(
            operation,
            origin,
            ownership=_GLOBAL_OWNER,
            effect=OperationEffect.HOST_EXECUTION,
            mode=EffectMode.GLOBAL,
        )
        assert policy.family is RunEffectFamily.RUN_LIFECYCLE
        assert policy.owner_kind is EffectOwnerKind.GLOBAL
        assert policy.allowed_run_kinds == frozenset()
        assert policy.ownership_resolver is OwnershipResolverKind.NONE
        assert not policy.required_claims.intersection(
            {
                OwnershipClaim.RUN_ID,
                OwnershipClaim.RUN_KIND,
                OwnershipClaim.AUDIT_ID,
                OwnershipClaim.PLAN_DIGEST,
            }
        )
        assert policy.audit_alternative.disposition is AuditAlternativeDisposition.NOT_RUN_SCOPED
        for wrong_owner in (_preflight_owner(), _run_owner(RunKind.CODE_AUDIT)):
            with pytest.raises(RunKindEffectPolicyDenied) as captured:
                require_run_kind_effect_policy(
                    operation,
                    origin,
                    ownership=wrong_owner,
                    effect=OperationEffect.HOST_EXECUTION,
                    mode=EffectMode.GLOBAL,
                )
            assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    owned_operations = (
        (
            RunEffectOperation.GET_AUDIT_PREFLIGHT,
            EffectOrigin.LOCAL_OPERATOR_API,
            OperationEffect.READ_ONLY,
            EffectMode.READ_ONLY,
        ),
        (
            RunEffectOperation.CANCEL_AUDIT_PREFLIGHT,
            EffectOrigin.LOCAL_OPERATOR_API,
            OperationEffect.HOST_CONTROL,
            EffectMode.NORMAL,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_GET,
            EffectOrigin.APPLICATION_SERVICE,
            OperationEffect.READ_ONLY,
            EffectMode.READ_ONLY,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CANCEL,
            EffectOrigin.APPLICATION_SERVICE,
            OperationEffect.HOST_CONTROL,
            EffectMode.NORMAL,
        ),
    )
    for operation, origin, effect, mode in owned_operations:
        policy = require_run_kind_effect_policy(
            operation,
            origin,
            ownership=_preflight_owner(),
            effect=effect,
            mode=mode,
        )
        assert policy.family is RunEffectFamily.RUN_LIFECYCLE
        assert policy.owner_kind is EffectOwnerKind.PREFLIGHT_JOB
        assert policy.allowed_run_kinds == frozenset()
        assert policy.ownership_resolver is OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
        assert not policy.required_claims.intersection(
            {
                OwnershipClaim.RUN_ID,
                OwnershipClaim.RUN_KIND,
                OwnershipClaim.AUDIT_ID,
                OwnershipClaim.PLAN_DIGEST,
            }
        )
        assert policy.audit_alternative.disposition is AuditAlternativeDisposition.NOT_RUN_SCOPED
        for wrong_owner in (_GLOBAL_OWNER, _run_owner(RunKind.CODE_AUDIT)):
            with pytest.raises(RunKindEffectPolicyDenied) as captured:
                require_run_kind_effect_policy(
                    operation,
                    origin,
                    ownership=wrong_owner,
                    effect=effect,
                    mode=mode,
                )
            assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    assert API_ROUTE_EFFECT_BINDINGS["create_audit_preflight"].operation is (
        RunEffectOperation.CREATE_AUDIT_PREFLIGHT
    )
    assert API_ROUTE_EFFECT_BINDINGS["get_audit_preflight"].operation is (
        RunEffectOperation.GET_AUDIT_PREFLIGHT
    )
    assert API_ROUTE_EFFECT_BINDINGS["cancel_audit_preflight"].operation is (
        RunEffectOperation.CANCEL_AUDIT_PREFLIGHT
    )
    assert API_ROUTE_EFFECT_BINDINGS["issue_audit_preflight_plan"].operation is (
        RunEffectOperation.ISSUE_AUDIT_PREFLIGHT_PLAN
    )
    managed = {
        (entrypoint.qualified_name, entrypoint.operation, entrypoint.origin)
        for entrypoint in MANAGED_EFFECT_ENTRYPOINTS
    }
    assert {
        (
            "riftx.application.services.audit_preflight:"
            "AuditPreflightApplicationService.create_authorized",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CREATE,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight:"
            "AuditPreflightApplicationService.get_authorized",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_GET,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight:"
            "AuditPreflightApplicationService.cancel_authorized",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_CANCEL,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight_plan:"
            "AuditPreflightPlanApplicationService.issue_authorized",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_PLAN_ISSUE,
            EffectOrigin.APPLICATION_SERVICE,
        ),
    } <= managed


def test_preflight_runner_repository_and_reconciler_effects_are_exactly_owned() -> None:
    callback_operations = (
        (
            RunEffectOperation.POLL_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.RENEW_AUDIT_PREFLIGHT_LEASE,
            EffectOrigin.RUNNER_API,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.START_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.FINISH_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.STOP_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            RunEffectFamily.SAFETY_STOP,
            EffectMode.STOP_PROOF,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_POLL,
            EffectOrigin.APPLICATION_SERVICE,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_RENEW,
            EffectOrigin.APPLICATION_SERVICE,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_START,
            EffectOrigin.APPLICATION_SERVICE,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_FINISH,
            EffectOrigin.APPLICATION_SERVICE,
            RunEffectFamily.RUNNER_COMMAND,
            EffectMode.OWNERSHIP_CALLBACK,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_STOP,
            EffectOrigin.APPLICATION_SERVICE,
            RunEffectFamily.SAFETY_STOP,
            EffectMode.STOP_PROOF,
        ),
    )
    for operation, origin, family, mode in callback_operations:
        policy = require_run_kind_effect_policy(
            operation,
            origin,
            ownership=_preflight_owner(),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=mode,
        )
        assert policy.family is family
        assert policy.owner_kind is EffectOwnerKind.PREFLIGHT_JOB
        assert policy.allowed_run_kinds == frozenset()
        assert policy.ownership_resolver is OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
        assert policy.audit_alternative.disposition is AuditAlternativeDisposition.NOT_RUN_SCOPED
        for wrong_owner in (_GLOBAL_OWNER, _run_owner(RunKind.CODE_AUDIT)):
            with pytest.raises(RunKindEffectPolicyDenied) as captured:
                require_run_kind_effect_policy(
                    operation,
                    origin,
                    ownership=wrong_owner,
                    effect=OperationEffect.RUNNER_CALLBACK,
                    mode=mode,
                )
            assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    mutation_operations = (
        (
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
            EffectMode.NORMAL,
            RunEffectFamily.RUN_LIFECYCLE,
        ),
        (
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
            EffectMode.RECONCILE,
            RunEffectFamily.SAFETY_STOP,
        ),
        (
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
            EffectMode.RECONCILE,
            RunEffectFamily.SAFETY_STOP,
        ),
        (
            RunEffectOperation.CONTROL_PLANE_AUDIT_PREFLIGHT_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
            EffectMode.RECONCILE,
            RunEffectFamily.SAFETY_STOP,
        ),
    )
    for operation, origin, mode, family in mutation_operations:
        policy = require_run_kind_effect_policy(
            operation,
            origin,
            ownership=_preflight_owner(),
            effect=OperationEffect.DURABLE_WRITE,
            mode=mode,
        )
        assert policy.family is family
        assert policy.owner_kind is EffectOwnerKind.PREFLIGHT_JOB
        assert policy.allowed_run_kinds == frozenset()
        assert policy.ownership_resolver is OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
        assert policy.audit_alternative.disposition is AuditAlternativeDisposition.NOT_RUN_SCOPED
        for wrong_owner in (_GLOBAL_OWNER, _run_owner(RunKind.CODE_AUDIT)):
            with pytest.raises(RunKindEffectPolicyDenied) as captured:
                require_run_kind_effect_policy(
                    operation,
                    origin,
                    ownership=wrong_owner,
                    effect=OperationEffect.DURABLE_WRITE,
                    mode=mode,
                )
            assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    with pytest.raises(RunKindEffectPolicyDenied) as effect_mismatch:
        require_run_kind_effect_policy(
            RunEffectOperation.START_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            ownership=_preflight_owner(),
            effect=OperationEffect.HOST_EXECUTION,
            mode=EffectMode.OWNERSHIP_CALLBACK,
        )
    assert effect_mismatch.value.reason is PolicyDenialReason.EFFECT_MISMATCH

    with pytest.raises(RunKindEffectPolicyDenied) as mode_mismatch:
        require_run_kind_effect_policy(
            RunEffectOperation.STOP_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.RUNNER_API,
            ownership=_preflight_owner(),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.OWNERSHIP_CALLBACK,
        )
    assert mode_mismatch.value.reason is PolicyDenialReason.MODE_MISMATCH

    with pytest.raises(RunKindEffectPolicyDenied) as origin_mismatch:
        resolve_run_kind_effect_policy(
            RunEffectOperation.POLL_AUDIT_PREFLIGHT_JOB,
            EffectOrigin.APPLICATION_SERVICE,
        )
    assert origin_mismatch.value.reason is PolicyDenialReason.UNREGISTERED_OPERATION_ORIGIN


def test_preflight_effect_inventory_covers_runner_repository_and_reconciler() -> None:
    route_operations = {
        "poll_audit_preflight_job": RunEffectOperation.POLL_AUDIT_PREFLIGHT_JOB,
        "renew_audit_preflight_lease": RunEffectOperation.RENEW_AUDIT_PREFLIGHT_LEASE,
        "start_audit_preflight_job": RunEffectOperation.START_AUDIT_PREFLIGHT_JOB,
        "finish_audit_preflight_job": RunEffectOperation.FINISH_AUDIT_PREFLIGHT_JOB,
        "stop_audit_preflight_job": RunEffectOperation.STOP_AUDIT_PREFLIGHT_JOB,
    }
    for route_name, operation in route_operations.items():
        binding = API_ROUTE_EFFECT_BINDINGS[route_name]
        assert binding.operation is operation
        assert binding.origin is EffectOrigin.RUNNER_API

    expected_entrypoints = {
        (
            "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService.poll",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_POLL,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight_runner:"
            "AuditPreflightRunnerService.renew_lease",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_RENEW,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService.start",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_START,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService.finish",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_FINISH,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService.stop",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RUNNER_STOP,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            "riftx.api.runtime:ControlPlane._reconcile_audit_preflight_jobs",
            RunEffectOperation.CONTROL_PLANE_AUDIT_PREFLIGHT_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
        ),
    }
    expected_entrypoints.update(
        (
            "riftx.application.services.audit_preflight_runner:"
            f"AuditPreflightRunnerService.{method}",
            RunEffectOperation.SERVICE_AUDIT_PREFLIGHT_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
        )
        for method in (
            "reconcile_batch",
            "mark_expired_outcome_unknown",
            "expire_pending_never_created",
            "converge_finish_receipt",
            "converge_stop_receipt",
        )
    )
    expected_entrypoints.update(
        (
            f"riftx.persistence.audit_preflight:SQLAlchemyAuditPreflightRepository.{method}",
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
            EffectOrigin.APPLICATION_SERVICE,
        )
        for method in ("create", "claim_next", "compare_and_set")
    )
    expected_entrypoints.add(
        (
            "riftx.persistence.audit_preflight:"
            "SQLAlchemyAuditPreflightRepository.compare_and_set_reconciliation",
            RunEffectOperation.PERSIST_AUDIT_PREFLIGHT_MUTATION,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
        )
    )
    actual_entrypoints = {
        (entrypoint.qualified_name, entrypoint.operation, entrypoint.origin)
        for entrypoint in MANAGED_EFFECT_ENTRYPOINTS
    }
    assert expected_entrypoints <= actual_entrypoints

    managed_types = {item.qualified_name: item for item in MANAGED_EFFECT_TYPES}
    runner_type = managed_types[
        "riftx.application.services.audit_preflight_runner:AuditPreflightRunnerService"
    ]
    assert runner_type.read_only_methods == frozenset({"authenticate"})
    repository_type = managed_types[
        "riftx.persistence.audit_preflight:SQLAlchemyAuditPreflightRepository"
    ]
    assert repository_type.read_only_methods == frozenset(
        {
            "get_owner_binding",
            "get_idempotency_binding",
            "get",
            "get_reconciliation_candidate",
            "get_replayable_claim",
            "list_reconciliation_candidates",
        }
    )


def test_preflight_owner_is_independent_and_never_resolves_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_claims = frozenset(
        {
            OwnershipClaim.PREFLIGHT_JOB_ID,
            OwnershipClaim.OPERATOR_PRINCIPAL_ID,
            OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST,
            OwnershipClaim.REQUEST_DIGEST,
            OwnershipClaim.NODE_ID,
        }
    )
    template = RUN_KIND_EFFECT_POLICIES[
        (RunEffectOperation.SERVICE_TOOL_UPDATE, EffectOrigin.APPLICATION_SERVICE)
    ]
    preflight_policy = RunKindEffectPolicy(
        operation=RunEffectOperation.SERVICE_TOOL_UPDATE,
        origin=EffectOrigin.APPLICATION_SERVICE,
        family=RunEffectFamily.ADMINISTRATION,
        owner_kind=EffectOwnerKind.PREFLIGHT_JOB,
        allowed_run_kinds=frozenset(),
        required_effect=OperationEffect.DURABLE_WRITE,
        ownership_resolver=OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE,
        required_claims=required_claims,
        effect_mode=EffectMode.NORMAL,
        audit_alternative=template.audit_alternative,
    )
    monkeypatch.setattr(
        run_kind_effects,
        "RUN_KIND_EFFECT_POLICIES",
        MappingProxyType(
            {
                (
                    RunEffectOperation.SERVICE_TOOL_UPDATE,
                    EffectOrigin.APPLICATION_SERVICE,
                ): preflight_policy
            }
        ),
    )

    owner = _preflight_owner()
    assert not hasattr(owner, "run_id")
    assert not hasattr(owner, "run_kind")
    accepted = require_run_kind_effect_policy(
        RunEffectOperation.SERVICE_TOOL_UPDATE,
        EffectOrigin.APPLICATION_SERVICE,
        ownership=owner,
        effect=OperationEffect.DURABLE_WRITE,
        mode=EffectMode.NORMAL,
    )
    assert accepted is preflight_policy

    for wrong_owner in (_GLOBAL_OWNER, _run_owner(RunKind.GENERAL)):
        with pytest.raises(RunKindEffectPolicyDenied) as captured:
            require_run_kind_effect_policy(
                RunEffectOperation.SERVICE_TOOL_UPDATE,
                EffectOrigin.APPLICATION_SERVICE,
                ownership=wrong_owner,
                effect=OperationEffect.DURABLE_WRITE,
                mode=EffectMode.NORMAL,
            )
        assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH


def test_wrong_owner_is_rejected_before_any_run_kind_interpretation() -> None:
    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.CANCEL_RUN,
            EffectOrigin.LOCAL_OPERATOR_API,
            ownership=_GLOBAL_OWNER,
            effect=OperationEffect.WORKFLOW_CONTROL,
            mode=EffectMode.NORMAL,
        )
    assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    fabricated_run = SimpleNamespace(
        owner_kind=EffectOwnerKind.RUN,
        run_kind="unknown-run-kind",
    )
    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.CANCEL_RUN,
            EffectOrigin.LOCAL_OPERATOR_API,
            ownership=fabricated_run,
            effect=OperationEffect.WORKFLOW_CONTROL,
            mode=EffectMode.NORMAL,
        )
    assert captured.value.reason is PolicyDenialReason.OWNERSHIP_VARIANT_INVALID


def test_required_ownership_claims_cannot_be_omitted() -> None:
    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.RUNNER_COMMAND_FINISH,
            EffectOrigin.RUNNER_COMMAND,
            ownership=_run_owner(RunKind.GENERAL, runner_principal=None),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.OWNERSHIP_CALLBACK,
        )

    assert captured.value.reason is PolicyDenialReason.OWNERSHIP_CLAIM_MISSING


async def test_workflow_router_uses_policy_before_general_protocol_dispatch() -> None:
    general = _GeneralWorkflowSpy()
    router = RunWorkflowControlRouter(
        runs=_RouterRuns(RunKind.CODE_AUDIT),  # type: ignore[arg-type]
        audits=_RouterAudits(),  # type: ignore[arg-type]
        general=general,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await router.pause("audit-run")

    assert captured.value.code == "run_kind_operation_unsupported"
    assert general.calls == []


async def test_workflow_router_passes_exact_persisted_general_workflow_id() -> None:
    general = _GeneralWorkflowSpy()
    router = RunWorkflowControlRouter(
        runs=_RouterRuns(
            RunKind.GENERAL,
            workflow_id="historical-prefix-general-run",
        ),  # type: ignore[arg-type]
        audits=_RouterAudits(),  # type: ignore[arg-type]
        general=general,  # type: ignore[arg-type]
    )

    await router.pause("general-run")

    assert general.calls == [("pause", "general-run")]
    assert general.workflow_ids == ["historical-prefix-general-run"]


async def test_workflow_router_fails_closed_for_legacy_empty_workflow_id() -> None:
    general = _GeneralWorkflowSpy()
    router = RunWorkflowControlRouter(
        runs=_RouterRuns(RunKind.GENERAL, workflow_id=None),  # type: ignore[arg-type]
        audits=_RouterAudits(),  # type: ignore[arg-type]
        general=general,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await router.pause("general-run")

    assert captured.value.code == "workflow_identity_missing"
    assert general.calls == []


async def test_workflow_router_policy_denial_has_zero_dispatch_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_policy(*_: object, **__: object) -> None:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.OWNERSHIP_CLAIM_MISSING)

    monkeypatch.setattr(
        run_kind_effects,
        "require_run_kind_effect_policy",
        deny_policy,
    )
    general = _GeneralWorkflowSpy()
    router = RunWorkflowControlRouter(
        runs=_RouterRuns(RunKind.GENERAL),  # type: ignore[arg-type]
        audits=_RouterAudits(),  # type: ignore[arg-type]
        general=general,  # type: ignore[arg-type]
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await router.pause("general-run")

    assert captured.value.code == "run_kind_effect_policy_denied"
    assert general.calls == []


def test_generic_cancel_cannot_bypass_audit_host_control() -> None:
    generic = require_run_kind_effect_policy(
        RunEffectOperation.CANCEL_RUN,
        EffectOrigin.LOCAL_OPERATOR_API,
        ownership=_run_owner(RunKind.GENERAL),
        effect=OperationEffect.WORKFLOW_CONTROL,
        mode=EffectMode.NORMAL,
    )
    assert generic.allowed_run_kinds == frozenset({RunKind.GENERAL})
    assert generic.audit_alternative.operation is RunEffectOperation.CANCEL_AUDIT

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.CANCEL_RUN,
            EffectOrigin.LOCAL_OPERATOR_API,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.WORKFLOW_CONTROL,
            mode=EffectMode.NORMAL,
        )
    assert captured.value.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.CANCEL_AUDIT,
            EffectOrigin.LOCAL_OPERATOR_API,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.WORKFLOW_CONTROL,
            mode=EffectMode.NORMAL,
        )
    assert captured.value.reason is PolicyDenialReason.EFFECT_MISMATCH

    audit = require_run_kind_effect_policy(
        RunEffectOperation.CANCEL_AUDIT,
        EffectOrigin.LOCAL_OPERATOR_API,
        ownership=_run_owner(RunKind.CODE_AUDIT),
        effect=OperationEffect.HOST_CONTROL,
        mode=EffectMode.NORMAL,
    )
    assert audit.ownership_resolver is OwnershipResolverKind.AUDIT_ID


def test_generic_cleanup_cannot_dispatch_code_audit_workflow_finalization() -> None:
    generic = require_run_kind_effect_policy(
        RunEffectOperation.SERVICE_RUN_CLEANUP,
        EffectOrigin.SAFETY_RECONCILER,
        ownership=_run_owner(RunKind.GENERAL),
        effect=OperationEffect.HOST_CONTROL,
        mode=EffectMode.SAFETY_REDUCE_ONLY,
    )
    assert generic.allowed_run_kinds == frozenset({RunKind.GENERAL})
    assert generic.audit_alternative.operation is RunEffectOperation.SERVICE_AUDIT_RECONCILE

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUN_CLEANUP,
            EffectOrigin.SAFETY_RECONCILER,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.HOST_CONTROL,
            mode=EffectMode.SAFETY_REDUCE_ONLY,
        )

    assert captured.value.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED


def test_read_allowlist_keeps_only_safe_generic_audit_projections() -> None:
    for operation, resolver in (
        (RunEffectOperation.GET_RUN, OwnershipResolverKind.RUN_ID),
        (RunEffectOperation.GET_EXECUTION, OwnershipResolverKind.EXECUTION_ID),
        (RunEffectOperation.GET_ARTIFACT, OwnershipResolverKind.ARTIFACT_ID),
        (RunEffectOperation.LIST_EVENTS, OwnershipResolverKind.RUN_ID),
    ):
        policy = require_run_kind_effect_policy(
            operation,
            EffectOrigin.LOCAL_OPERATOR_API,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.READ_ONLY,
            mode=EffectMode.READ_ONLY,
        )
        assert policy.ownership_resolver is resolver
        assert policy.audit_alternative.disposition is AuditAlternativeDisposition.SAFE_PROJECTION

    for operation in (
        RunEffectOperation.GET_FINDING,
        RunEffectOperation.GET_REPORT,
        RunEffectOperation.LIST_APPROVALS,
        RunEffectOperation.GET_RUN_ACTION,
        RunEffectOperation.GET_RUN_GRAPH,
        RunEffectOperation.GET_RUN_METRICS,
        RunEffectOperation.GET_TARGET_HTTP_EXCHANGE,
        RunEffectOperation.GET_TERMINAL,
        RunEffectOperation.GET_BROWSER,
        RunEffectOperation.GET_RUN_CONTEXT,
        RunEffectOperation.GET_MEMORY,
        RunEffectOperation.CONNECTOR_WEBUI,
    ):
        with pytest.raises(RunKindEffectPolicyDenied) as captured:
            require_run_kind_effect_policy(
                operation,
                EffectOrigin.LOCAL_OPERATOR_API,
                ownership=_run_owner(RunKind.CODE_AUDIT),
                effect=OperationEffect.READ_ONLY,
                mode=EffectMode.READ_ONLY,
            )
        assert captured.value.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED


def test_effect_modes_do_not_promote_callback_or_safety_capabilities() -> None:
    callback = RunEffectOperation.RUNNER_COMMAND_FINISH
    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            callback,
            EffectOrigin.RUNNER_COMMAND,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    assert captured.value.reason is PolicyDenialReason.MODE_MISMATCH

    stop_proof = require_run_kind_effect_policy(
        RunEffectOperation.RUNNER_COMMAND_STOP_ACK,
        EffectOrigin.RUNNER_COMMAND,
        ownership=_run_owner(RunKind.CODE_AUDIT),
        effect=OperationEffect.RUNNER_CALLBACK,
        mode=EffectMode.STOP_PROOF,
    )
    assert stop_proof.family is RunEffectFamily.SAFETY_STOP
    assert stop_proof.ownership_resolver is OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.SAFETY_STOP_RUN,
            EffectOrigin.SAFETY_RECONCILER,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.HOST_CONTROL,
            mode=EffectMode.NORMAL,
        )
    assert captured.value.reason is PolicyDenialReason.MODE_MISMATCH


def test_legacy_runner_stop_owner_is_exact_and_never_falls_back() -> None:
    with pytest.raises(ValueError, match="ownership-missing quarantine"):
        LegacyRunnerCommandEffectOwnership(
            node_id="legacy-node",
            runner_principal=object(),
            runner_command_id="legacy-command",
            lease_identity="legacy-lease",
            quarantine_state="replaced",
        )

    owner = LegacyRunnerCommandEffectOwnership(
        node_id="legacy-node",
        runner_principal=SimpleNamespace(instance_id="legacy-runner", epoch=1),
        runner_command_id="legacy-command",
        lease_identity="legacy-lease",
        quarantine_state="quarantined:legacy_ownership_missing",
    )
    for operation, origin in (
        (
            RunEffectOperation.FINISH_LEGACY_RUNNER_COMMAND,
            EffectOrigin.RUNNER_API,
        ),
        (
            RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
        ),
        (
            RunEffectOperation.RUNNER_COMMAND_LEGACY_STOP_ACK,
            EffectOrigin.RUNNER_COMMAND,
        ),
    ):
        policy = require_run_kind_effect_policy(
            operation,
            origin,
            ownership=owner,
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
        assert policy.owner_kind is EffectOwnerKind.LEGACY_RUNNER_COMMAND
        assert not policy.allowed_run_kinds
        assert policy.ownership_resolver is OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE

    with pytest.raises(RunKindEffectPolicyDenied) as normal_stop:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUNNER_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=owner,
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    assert normal_stop.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    with pytest.raises(RunKindEffectPolicyDenied) as run_fallback:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=_run_owner(RunKind.GENERAL),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    assert run_fallback.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH

    forged_variant = SimpleNamespace(
        owner_kind=EffectOwnerKind.LEGACY_RUNNER_COMMAND,
        node_id="legacy-node",
        runner_principal=object(),
        runner_command_id="legacy-command",
        lease_identity="legacy-lease",
    )
    with pytest.raises(RunKindEffectPolicyDenied) as variant:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=forged_variant,
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    assert variant.value.reason is PolicyDenialReason.OWNERSHIP_VARIANT_INVALID

    with pytest.raises(RunKindEffectPolicyDenied) as unknown:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=SimpleNamespace(owner_kind="future-owner"),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    assert unknown.value.reason is PolicyDenialReason.UNKNOWN_OWNER_KIND


def test_global_operations_reject_fabricated_run_kind_context() -> None:
    policy = require_run_kind_effect_policy(
        RunEffectOperation.UPDATE_TOOL,
        EffectOrigin.ADMIN_API,
        ownership=_GLOBAL_OWNER,
        effect=OperationEffect.DURABLE_WRITE,
        mode=EffectMode.GLOBAL,
    )
    assert not policy.allowed_run_kinds
    assert policy.audit_alternative.disposition is AuditAlternativeDisposition.NOT_RUN_SCOPED

    with pytest.raises(RunKindEffectPolicyDenied) as captured:
        require_run_kind_effect_policy(
            RunEffectOperation.UPDATE_TOOL,
            EffectOrigin.ADMIN_API,
            ownership=_run_owner(RunKind.CODE_AUDIT),
            effect=OperationEffect.DURABLE_WRITE,
            mode=EffectMode.GLOBAL,
        )
    assert captured.value.reason is PolicyDenialReason.OWNER_KIND_MISMATCH


def test_every_catalog_rule_declares_owner_and_resolver_claim_invariants() -> None:
    owner_claims = {
        EffectOwnerKind.GLOBAL: frozenset({OwnershipClaim.ADMINISTRATIVE_SCOPE_DIGEST}),
        EffectOwnerKind.RUN: frozenset({OwnershipClaim.RUN_ID, OwnershipClaim.RUN_KIND}),
        EffectOwnerKind.PREFLIGHT_JOB: frozenset(
            {
                OwnershipClaim.PREFLIGHT_JOB_ID,
                OwnershipClaim.OPERATOR_PRINCIPAL_ID,
                OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST,
                OwnershipClaim.REQUEST_DIGEST,
                OwnershipClaim.NODE_ID,
            }
        ),
        EffectOwnerKind.LEGACY_RUNNER_COMMAND: frozenset(
            {
                OwnershipClaim.NODE_ID,
                OwnershipClaim.RUNNER_PRINCIPAL,
                OwnershipClaim.RUNNER_COMMAND_ID,
                OwnershipClaim.LEASE_IDENTITY,
                OwnershipClaim.QUARANTINE_STATE,
            }
        ),
    }
    resolver_claims = {
        OwnershipResolverKind.NONE: frozenset(),
        OwnershipResolverKind.REQUEST_RUN_KIND: frozenset({OwnershipClaim.RUN_KIND}),
        OwnershipResolverKind.RUN_QUERY: frozenset(),
        OwnershipResolverKind.RUN_ID: frozenset({OwnershipClaim.RUN_ID}),
        OwnershipResolverKind.AUDIT_CONTRACT: frozenset(),
        OwnershipResolverKind.AUDIT_QUERY: frozenset(),
        OwnershipResolverKind.AUDIT_ID: frozenset({OwnershipClaim.RUN_ID, OwnershipClaim.AUDIT_ID}),
        OwnershipResolverKind.EXECUTION_ID: frozenset({OwnershipClaim.EXECUTION_ID}),
        OwnershipResolverKind.APPROVAL_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.ARTIFACT_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.FINDING_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.REPORT_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.MEMORY_SCOPE: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.TERMINAL_SESSION_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.BROWSER_SESSION_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.TARGET_HTTP_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.CONNECTOR_RUN_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.CONTEXT_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.TOOL_CALL_INTENT_ID: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.CHILD_RUN_BINDING: frozenset(
            {OwnershipClaim.RESOURCE_KIND, OwnershipClaim.RESOURCE_ID}
        ),
        OwnershipResolverKind.NODE_PRINCIPAL: frozenset(
            {OwnershipClaim.NODE_ID, OwnershipClaim.RUNNER_PRINCIPAL}
        ),
        OwnershipResolverKind.RUNNER_COMMAND_ENVELOPE: frozenset(
            {
                OwnershipClaim.NODE_ID,
                OwnershipClaim.RUNNER_PRINCIPAL,
                OwnershipClaim.RUNNER_COMMAND_ID,
                OwnershipClaim.RESOURCE_KIND,
                OwnershipClaim.RESOURCE_ID,
            }
        ),
        OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE: frozenset(
            {
                OwnershipClaim.NODE_ID,
                OwnershipClaim.RUNNER_PRINCIPAL,
                OwnershipClaim.RUNNER_COMMAND_ID,
                OwnershipClaim.LEASE_IDENTITY,
                OwnershipClaim.QUARANTINE_STATE,
            }
        ),
        OwnershipResolverKind.EXECUTION_OWNERSHIP_ENVELOPE: frozenset(
            {
                OwnershipClaim.EXECUTION_ID,
                OwnershipClaim.NODE_ID,
                OwnershipClaim.RUNNER_PRINCIPAL,
            }
        ),
        OwnershipResolverKind.AUDIT_OWNERSHIP_ENVELOPE: frozenset(
            {OwnershipClaim.AUDIT_ID, OwnershipClaim.PLAN_DIGEST}
        ),
        OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE: frozenset(
            {
                OwnershipClaim.PREFLIGHT_JOB_ID,
                OwnershipClaim.OPERATOR_PRINCIPAL_ID,
                OwnershipClaim.AUTHORIZATION_SCOPE_DIGEST,
                OwnershipClaim.REQUEST_DIGEST,
                OwnershipClaim.NODE_ID,
            }
        ),
    }
    assert set(owner_claims) == set(EffectOwnerKind)
    assert set(resolver_claims) == set(OwnershipResolverKind)

    for key, policy in RUN_KIND_EFFECT_POLICIES.items():
        assert key == (policy.operation, policy.origin)
        assert owner_claims[policy.owner_kind] <= policy.required_claims
        assert resolver_claims[policy.ownership_resolver] <= policy.required_claims
        if policy.owner_kind is EffectOwnerKind.RUN:
            assert policy.allowed_run_kinds
            assert policy.effect_mode is not EffectMode.GLOBAL
        elif policy.owner_kind is EffectOwnerKind.GLOBAL:
            assert not policy.allowed_run_kinds
            assert policy.effect_mode is EffectMode.GLOBAL
            assert (
                policy.ownership_resolver is not OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
            )
        elif policy.owner_kind is EffectOwnerKind.PREFLIGHT_JOB:
            assert not policy.allowed_run_kinds
            assert policy.ownership_resolver is OwnershipResolverKind.PREFLIGHT_JOB_OWNER_ENVELOPE
            assert policy.effect_mode is not EffectMode.GLOBAL
        else:
            assert not policy.allowed_run_kinds
            assert policy.ownership_resolver is OwnershipResolverKind.LEGACY_RUNNER_STOP_LEASE
            assert policy.effect_mode is EffectMode.STOP_PROOF


def test_managed_service_callback_and_reconciler_inventory_is_registered() -> None:
    validate_managed_effect_inventory()

    assert len(MANAGED_EFFECT_TYPES) == len(
        {managed_type.qualified_name for managed_type in MANAGED_EFFECT_TYPES}
    )
    for entrypoint in MANAGED_EFFECT_ENTRYPOINTS:
        assert (entrypoint.operation, entrypoint.origin) in RUN_KIND_EFFECT_POLICIES


def test_public_web_services_allow_code_audit_without_widening_target_or_browser() -> None:
    for operation in (
        RunEffectOperation.SERVICE_WEB_FETCH,
        RunEffectOperation.SERVICE_WEB_SEARCH,
        RunEffectOperation.SERVICE_WEB_RESEARCH,
        RunEffectOperation.SERVICE_MCP_INVOKE,
    ):
        policy = RUN_KIND_EFFECT_POLICIES[(operation, EffectOrigin.APPLICATION_SERVICE)]
        assert policy.allowed_run_kinds == frozenset(
            {RunKind.GENERAL, RunKind.CODE_AUDIT}
        )
        assert policy.required_effect is OperationEffect.HOST_EXECUTION
        assert policy.effect_mode is EffectMode.NORMAL

    for operation in (
        RunEffectOperation.SERVICE_BROWSER_OPEN,
        RunEffectOperation.SERVICE_TARGET_HTTP_EXECUTE,
    ):
        policy = RUN_KIND_EFFECT_POLICIES[(operation, EffectOrigin.APPLICATION_SERVICE)]
        assert policy.allowed_run_kinds == frozenset({RunKind.GENERAL})


def test_workflow_signal_outbox_inventory_covers_every_managed_boundary() -> None:
    expected = {
        (
            "riftx.application.services.workflow_signals:"
            "WorkflowSignalOutboxApplicationService.create"
        ): (
            RunEffectOperation.SERVICE_WORKFLOW_SIGNAL_CREATE,
            EffectOrigin.APPLICATION_SERVICE,
            OperationEffect.DURABLE_WRITE,
            EffectMode.NORMAL,
        ),
        ("riftx.application.services.workflow_signals:WorkflowSignalDispatcher.dispatch_batch"): (
            RunEffectOperation.WORKFLOW_SIGNAL_DISPATCH,
            EffectOrigin.SAFETY_RECONCILER,
            OperationEffect.WORKFLOW_CONTROL,
            EffectMode.RECONCILE,
        ),
        ("riftx.application.services.workflow_signals:WorkflowSignalReconciler.reconcile_batch"): (
            RunEffectOperation.WORKFLOW_SIGNAL_RECONCILE,
            EffectOrigin.SAFETY_RECONCILER,
            OperationEffect.DURABLE_WRITE,
            EffectMode.RECONCILE,
        ),
        "riftx.temporal.workflow_signal_transport:RoutedWorkflowSignalTransport.send": (
            RunEffectOperation.WORKFLOW_SIGNAL_TRANSPORT_SEND,
            EffectOrigin.SAFETY_RECONCILER,
            OperationEffect.WORKFLOW_CONTROL,
            EffectMode.RECONCILE,
        ),
        ("riftx.temporal.workflow_signal_transport:TemporalWorkflowSignalOutcomeProbe.observe"): (
            RunEffectOperation.WORKFLOW_SIGNAL_OUTCOME_PROBE,
            EffectOrigin.SAFETY_RECONCILER,
            OperationEffect.READ_ONLY,
            EffectMode.READ_ONLY,
        ),
        "riftx.api.runtime:ControlPlane._reconcile_workflow_signals": (
            RunEffectOperation.CONTROL_PLANE_WORKFLOW_SIGNAL_RECONCILE,
            EffectOrigin.CONTROL_PLANE_RECONCILER,
            OperationEffect.WORKFLOW_CONTROL,
            EffectMode.RECONCILE,
        ),
        "riftx.temporal.worker_runtime:TemporalWorkerRuntime._workflow_signal_loop": (
            RunEffectOperation.WORKER_WORKFLOW_SIGNAL_RECONCILE,
            EffectOrigin.WORKER_RECONCILER,
            OperationEffect.WORKFLOW_CONTROL,
            EffectMode.RECONCILE,
        ),
    }
    entrypoints = {item.qualified_name: item for item in MANAGED_EFFECT_ENTRYPOINTS}

    for qualified_name, (operation, origin, effect, mode) in expected.items():
        entrypoint = entrypoints[qualified_name]
        assert (entrypoint.operation, entrypoint.origin) == (operation, origin)
        assert entrypoint.surface is (
            EffectEntrypointSurface.SERVICE
            if operation is RunEffectOperation.SERVICE_WORKFLOW_SIGNAL_CREATE
            else EffectEntrypointSurface.RECONCILER
        )
        policy = RUN_KIND_EFFECT_POLICIES[(operation, origin)]
        assert policy.allowed_run_kinds == frozenset(
            {RunKind.GENERAL, RunKind.CODE_AUDIT}
        )
        assert policy.required_effect is effect
        assert policy.effect_mode is mode

    managed_types = {item.qualified_name for item in MANAGED_EFFECT_TYPES}
    assert {
        "riftx.application.services.workflow_signals:WorkflowSignalOutboxApplicationService",
        "riftx.application.services.workflow_signals:WorkflowSignalDispatcher",
        "riftx.application.services.workflow_signals:WorkflowSignalReconciler",
        "riftx.temporal.workflow_signal_transport:RoutedWorkflowSignalTransport",
        "riftx.temporal.workflow_signal_transport:TemporalWorkflowSignalOutcomeProbe",
    } <= managed_types


def test_managed_inventory_rejects_an_unregistered_entrypoint() -> None:
    unregistered = ManagedEffectEntrypoint(
        qualified_name="riftx.example:Canary.mutate",
        operation=RunEffectOperation.AUDIT_ARTIFACT_INGEST,
        origin=EffectOrigin.APPLICATION_SERVICE,
    )

    with pytest.raises(RunKindEffectInventoryError, match="riftx.example:Canary.mutate"):
        validate_managed_effect_inventory((*MANAGED_EFFECT_ENTRYPOINTS, unregistered))


def test_managed_inventory_rejects_a_new_unclassified_public_async_method() -> None:
    managed_type = ManagedEffectType(qualified_name=f"{__name__}:_InventoryAsyncCanary")

    with pytest.raises(RunKindEffectInventoryError, match="unclassified=.*mutate"):
        validate_managed_effect_inventory((), (managed_type,))


def test_managed_inventory_rejects_duplicate_and_overlapping_classifications() -> None:
    qualified_method = f"{__name__}:_InventoryAsyncCanary.mutate"
    entrypoint = ManagedEffectEntrypoint(
        qualified_name=qualified_method,
        operation=RunEffectOperation.SERVICE_TOOL_UPDATE,
        origin=EffectOrigin.APPLICATION_SERVICE,
    )
    managed_name = f"{__name__}:_InventoryAsyncCanary"

    with pytest.raises(RunKindEffectInventoryError, match="duplicates=.*mutate"):
        validate_managed_effect_inventory(
            (entrypoint, entrypoint),
            (ManagedEffectType(qualified_name=managed_name),),
        )

    with pytest.raises(RunKindEffectInventoryError, match="multiple_categories"):
        validate_managed_effect_inventory(
            (entrypoint,),
            (
                ManagedEffectType(
                    qualified_name=managed_name,
                    read_only_methods=frozenset({"mutate"}),
                ),
            ),
        )

    out_of_scope = ManagedOutOfScopeMethod("mutate", "test-only process root")
    with pytest.raises(RunKindEffectInventoryError, match="multiple_categories"):
        validate_managed_effect_inventory(
            (entrypoint,),
            (
                ManagedEffectType(
                    qualified_name=managed_name,
                    out_of_scope_methods=(out_of_scope,),
                ),
            ),
        )

    with pytest.raises(RunKindEffectInventoryError, match="multiple_categories"):
        validate_managed_effect_inventory(
            (),
            (
                ManagedEffectType(
                    qualified_name=managed_name,
                    read_only_methods=frozenset({"mutate"}),
                    out_of_scope_methods=(out_of_scope,),
                ),
            ),
        )


def test_managed_inventory_rejects_missing_and_non_async_symbols() -> None:
    missing_entrypoint = ManagedEffectEntrypoint(
        qualified_name=f"{__name__}:_MissingInventoryCanary.mutate",
        operation=RunEffectOperation.SERVICE_TOOL_UPDATE,
        origin=EffectOrigin.APPLICATION_SERVICE,
    )
    with pytest.raises(RunKindEffectInventoryError, match="unresolved=.*Missing"):
        validate_managed_effect_inventory((missing_entrypoint,), ())

    sync_entrypoint = ManagedEffectEntrypoint(
        qualified_name=f"{__name__}:_InventorySyncCanary.mutate",
        operation=RunEffectOperation.SERVICE_TOOL_UPDATE,
        origin=EffectOrigin.APPLICATION_SERVICE,
    )
    with pytest.raises(RunKindEffectInventoryError, match="unresolved=.*Sync"):
        validate_managed_effect_inventory((sync_entrypoint,), ())

    missing_type = ManagedEffectType(qualified_name=f"{__name__}:_MissingInventoryCanary")
    with pytest.raises(RunKindEffectInventoryError, match="unresolved=.*Missing"):
        validate_managed_effect_inventory((), (missing_type,))


def test_every_managed_entrypoint_resolves_to_a_callable_symbol() -> None:
    for entrypoint in MANAGED_EFFECT_ENTRYPOINTS:
        module_name, qualified_name = entrypoint.qualified_name.split(":", maxsplit=1)
        target: object = importlib.import_module(module_name)
        for segment in qualified_name.split("."):
            target = getattr(target, segment)
        assert callable(target), entrypoint.qualified_name


def test_catalog_and_route_bindings_are_immutable() -> None:
    assert isinstance(RUN_KIND_EFFECT_POLICIES, MappingProxyType)
    assert isinstance(API_ROUTE_EFFECT_BINDINGS, MappingProxyType)

    with pytest.raises(TypeError):
        RUN_KIND_EFFECT_POLICIES[
            (RunEffectOperation.CANCEL_RUN, EffectOrigin.LOCAL_OPERATOR_API)
        ] = SimpleNamespace()  # type: ignore[index]


def test_runner_ownership_enums_are_strict_catalog_subsets() -> None:
    assert {item.value for item in RunnerCommandOrigin} <= {item.value for item in EffectOrigin}
    assert {item.value for item in RunnerOperationFamily} <= {
        item.value for item in RunEffectFamily
    }
