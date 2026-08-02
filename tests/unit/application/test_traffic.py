from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.services.traffic import TrafficMetadataApplicationService
from riftx.application.traffic import (
    InvalidTrafficCursorError,
    StaleTrafficCursorError,
    TrafficBodyAvailability,
    TrafficExchangeSource,
    TrafficMetadataCapability,
    TrafficPageKey,
    TrafficScopeSource,
    TrafficSnapshotSource,
    TrafficSourceContractError,
    TrafficSourcePage,
    TrafficStatusClass,
)
from riftx.domain import LocalPrincipal, OperatorCapability

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
PRINCIPAL = LocalPrincipal(
    id="local-principal:v1:traffic-test",
    capabilities=frozenset({OperatorCapability.READ}),
)


def _source(index: int, **changes: object) -> TrafficExchangeSource:
    source = TrafficExchangeSource(
        exchange_id=f"exchange-{index}",
        execution_key=f"execution-key-{index}",
        run_id="run-traffic",
        session_id="session-traffic",
        tool_call_id=f"intent-{index}",
        node_id="node-traffic",
        node_status="online",
        method="GET",
        canonical_request_digest=f"{index + 1:064x}",
        safe_metadata_version=1,
        url_scheme="https",
        url_origin="https://target.example",
        url_path_shape="/…",
        url_path_segment_count=2,
        redirect_count=2,
        redirect_origins=("https://redirect.example", "https://target.example"),
        request_body_availability="absent",
        status_code=200,
        elapsed_ms=10,
        content_type="application/json",
        content_type_redacted=False,
        content_length=999,
        response_truncated=False,
        tls_verified=True,
        tls_client_certificate_used=False,
        request_artifact_ref=None,
        request_artifact_recorded=False,
        request_artifact_present=False,
        response_artifact_ref=f"traffic-artifact:v1:{index + 11:064x}",
        response_artifact_recorded=True,
        response_artifact_present=True,
        intent_lineage_exact=True,
        intent_approval_level="never",
        approval_reference_id=None,
        approval_status=None,
        created_at=NOW - timedelta(minutes=index),
    )
    return replace(source, **changes)


class RecordingAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, str | None, TrafficMetadataCapability]] = []

    def require_traffic_metadata(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: TrafficMetadataCapability,
    ) -> None:
        assert OperatorCapability.READ in principal.capabilities
        self.calls.append(
            (
                parent_run_id,
                resource_run_id,
                parent_engagement_id,
                resource_engagement_id,
                capability,
            )
        )
        if resource_run_id != parent_run_id or resource_engagement_id != parent_engagement_id:
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )


class MemoryRepository:
    def __init__(self, items: list[TrafficExchangeSource]) -> None:
        self.items = items
        self.snapshot_total_delta = 0
        self.scope = TrafficScopeSource(
            run_id="run-traffic",
            engagement_id="engagement-traffic",
        )

    async def resolve_scope(self, run_id: str) -> TrafficScopeSource | None:
        if run_id != self.scope.run_id:
            return None
        return self.scope

    async def list_page(
        self,
        scope: TrafficScopeSource,
        *,
        method: str | None,
        status_class: TrafficStatusClass | None,
        limit: int,
        after: TrafficPageKey | None,
        snapshot: TrafficPageKey | None,
    ) -> TrafficSourcePage:
        del scope
        filtered = [
            item
            for item in self.items
            if (method is None or item.method == method)
            and (
                status_class is None
                or status_class is TrafficStatusClass.SUCCESS
                and 200 <= item.status_code <= 299
            )
        ]
        filtered.sort(key=lambda item: (item.created_at, item.exchange_id), reverse=True)
        boundary = snapshot or (
            TrafficPageKey(filtered[0].created_at, filtered[0].exchange_id) if filtered else None
        )
        if boundary is not None:
            boundary_tuple = (boundary.created_at, boundary.exchange_id)
            filtered = [
                item for item in filtered if (item.created_at, item.exchange_id) <= boundary_tuple
            ]
        if after is not None:
            after_tuple = (after.created_at, after.exchange_id)
            filtered = [
                item for item in filtered if (item.created_at, item.exchange_id) < after_tuple
            ]
        return TrafficSourcePage(
            snapshot=TrafficSnapshotSource(
                boundary=boundary,
                total=len(self.items) + self.snapshot_total_delta,
            ),
            items=tuple(filtered[: limit + 1]),
            has_more=len(filtered) > limit,
        )

    async def get(
        self,
        scope: TrafficScopeSource,
        exchange_id: str,
    ) -> TrafficExchangeSource | None:
        return next(
            (
                item
                for item in self.items
                if item.exchange_id == exchange_id and item.run_id == scope.run_id
            ),
            None,
        )


def _service(
    repository: MemoryRepository,
) -> tuple[TrafficMetadataApplicationService, RecordingAuthorizer]:
    authorizer = RecordingAuthorizer()
    return (
        TrafficMetadataApplicationService(
            repository,
            authorizer=authorizer,
            cursor_signing_key=b"traffic-cursor-test-key-0000000001",
        ),
        authorizer,
    )


async def test_history_projects_only_safe_metadata_and_stable_cursor_pages() -> None:
    repository = MemoryRepository([_source(0), _source(1), _source(2)])
    service, authorizer = _service(repository)

    first = await service.list("run-traffic", principal=PRINCIPAL, limit=2)
    assert [item.exchange_id for item in first.items] == ["exchange-0", "exchange-1"]
    assert first.has_more is True
    assert first.next_cursor is not None
    item = first.items[0]
    assert item.request_id == item.exchange_id
    assert item.url_summary.model_dump(mode="json") == {
        "availability": "available",
        "scheme": "https",
        "origin": "https://target.example",
        "path_shape": "/…",
        "path_segment_count": 2,
        "redacted": True,
    }
    assert item.redirect.origins == (
        "https://redirect.example",
        "https://target.example",
    )
    assert item.body.response.availability is TrafficBodyAvailability.PRESENT
    assert item.body.response.revealable is False
    assert item.artifacts.response.opaque_ref != "artifact-response"
    assert item.replay_of.request_id is None
    assert item.governance.reveal_capability.value == "disabled"
    assert item.projection_quality.value == "partial"
    serialized = first.model_dump_json()
    for forbidden in (
        "authorization",
        "cookie",
        "body_excerpt",
        "client_cert_ref",
        "request_hash",
        "artifact-response",
    ):
        assert forbidden not in serialized.lower()

    second = await service.list(
        "run-traffic",
        principal=PRINCIPAL,
        limit=2,
        cursor=first.next_cursor,
    )
    assert [item.exchange_id for item in second.items] == ["exchange-2"]
    assert second.has_more is False
    assert second.next_cursor is None
    assert all(call[-1] is TrafficMetadataCapability.READ for call in authorizer.calls)


async def test_cursor_binds_principal_parent_filter_limit_and_snapshot() -> None:
    repository = MemoryRepository([_source(0), _source(1)])
    service, _ = _service(repository)
    first = await service.list(
        "run-traffic",
        principal=PRINCIPAL,
        method="GET",
        status_class=TrafficStatusClass.SUCCESS,
        limit=1,
    )
    assert first.next_cursor is not None

    tampered = f"{first.next_cursor[:-1]}A"
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=tampered,
        )
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="POST",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=first.next_cursor,
        )
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.CLIENT_ERROR,
            limit=1,
            cursor=first.next_cursor,
        )
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=2,
            cursor=first.next_cursor,
        )
    other = PRINCIPAL.model_copy(update={"id": "local-principal:v1:other"})
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=other,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=first.next_cursor,
        )

    repository.scope = TrafficScopeSource(
        run_id="run-other",
        engagement_id="engagement-traffic",
    )
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-other",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=first.next_cursor,
        )

    repository.scope = TrafficScopeSource(
        run_id="run-traffic",
        engagement_id="engagement-other",
    )
    with pytest.raises(InvalidTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=first.next_cursor,
        )

    repository.scope = TrafficScopeSource(
        run_id="run-traffic",
        engagement_id="engagement-traffic",
    )
    repository.snapshot_total_delta = -1
    with pytest.raises(StaleTrafficCursorError):
        await service.list(
            "run-traffic",
            principal=PRINCIPAL,
            method="GET",
            status_class=TrafficStatusClass.SUCCESS,
            limit=1,
            cursor=first.next_cursor,
        )


async def test_lost_is_exact_but_missing_node_and_legacy_fields_are_explicit_partial() -> None:
    lost = _source(0, node_status="lost")
    legacy = _source(
        1,
        node_status=None,
        safe_metadata_version=None,
        url_scheme=None,
        url_origin=None,
        url_path_shape=None,
        url_path_segment_count=None,
        redirect_count=None,
        redirect_origins=None,
        request_body_availability=None,
        response_artifact_ref=None,
        response_artifact_recorded=False,
        response_artifact_present=False,
        content_type=None,
        content_type_redacted=True,
        tls_verified=None,
        tls_client_certificate_used=None,
    )
    service, _ = _service(MemoryRepository([lost, legacy]))
    page = await service.list("run-traffic", principal=PRINCIPAL)
    views = {item.exchange_id: item for item in page.items}

    assert views["exchange-0"].lineage.node_status.value == "lost"
    assert "node_status_unavailable" not in views["exchange-0"].partial_reasons
    legacy_view = views["exchange-1"]
    assert legacy_view.lineage.node_status.value == "unknown"
    assert legacy_view.url_summary.availability.value == "unavailable"
    assert legacy_view.redirect.availability.value == "unavailable"
    assert legacy_view.body.response.availability.value == "unknown"
    assert "node_status_unavailable" in legacy_view.partial_reasons
    assert "content_type_redacted" in legacy_view.partial_reasons


async def test_unknown_or_foreign_detail_is_uniformly_inaccessible() -> None:
    foreign = _source(0, run_id="run-foreign")
    service, _ = _service(MemoryRepository([foreign]))

    for exchange_id in ("missing", "exchange-0"):
        with pytest.raises(ResourceNotAccessibleError) as captured:
            await service.get("run-traffic", exchange_id, principal=PRINCIPAL)
        assert captured.value.code == "resource_not_accessible"


@pytest.mark.parametrize("execution_key", ["execution-key-legacy", "执行键-审计-01"])
async def test_execution_key_preserves_safe_legacy_and_unicode_values(
    execution_key: str,
) -> None:
    service, _ = _service(MemoryRepository([_source(0, execution_key=execution_key)]))

    page = await service.list("run-traffic", principal=PRINCIPAL)

    assert page.items[0].execution_key == execution_key


@pytest.mark.parametrize("execution_key", ["execution-key\nsecret", "execution-key\u200bsecret"])
async def test_execution_key_rejects_unicode_control_characters(
    execution_key: str,
) -> None:
    service, _ = _service(MemoryRepository([_source(0, execution_key=execution_key)]))

    with pytest.raises(TrafficSourceContractError):
        await service.list("run-traffic", principal=PRINCIPAL)


async def test_alternative_repository_cannot_bypass_content_type_allowlist() -> None:
    content_type_canary = "application/x-riftx-secret-canary"
    service, _ = _service(
        MemoryRepository(
            [
                _source(
                    0,
                    content_type=content_type_canary,
                    content_type_redacted=False,
                )
            ]
        )
    )

    with pytest.raises(TrafficSourceContractError) as captured:
        await service.list("run-traffic", principal=PRINCIPAL)

    assert content_type_canary not in str(captured.value)


async def test_orphan_lineage_approval_and_missing_artifacts_are_explicit_partial() -> None:
    orphan_intent = _source(
        0,
        intent_lineage_exact=False,
        intent_approval_level=None,
        request_artifact_ref=f"traffic-artifact:v1:{101:064x}",
        request_artifact_recorded=True,
        request_artifact_present=False,
        response_artifact_ref=f"traffic-artifact:v1:{102:064x}",
        response_artifact_recorded=True,
        response_artifact_present=False,
    )
    orphan_approval = _source(
        1,
        intent_lineage_exact=True,
        intent_approval_level="sensitive",
        approval_reference_id=None,
        approval_status=None,
    )
    service, _ = _service(MemoryRepository([orphan_intent, orphan_approval]))

    page = await service.list("run-traffic", principal=PRINCIPAL)
    views = {item.exchange_id: item for item in page.items}

    intent_view = views["exchange-0"]
    assert intent_view.created_by.availability.value == "unavailable"
    assert intent_view.created_by.kind.value == "unknown"
    assert intent_view.approval.availability.value == "unavailable"
    assert intent_view.artifacts.request.presence.value == "recorded_missing"
    assert intent_view.artifacts.response.presence.value == "recorded_missing"
    assert intent_view.body.response.availability.value == "unknown"
    assert {
        "approval_metadata_unavailable",
        "creator_lineage_unavailable",
        "request_artifact_missing",
        "response_artifact_missing",
        "response_body_availability_unknown",
    } <= set(intent_view.partial_reasons)

    approval_view = views["exchange-1"]
    assert approval_view.created_by.availability.value == "available"
    assert approval_view.approval.availability.value == "unavailable"
    assert "approval_metadata_unavailable" in approval_view.partial_reasons
