"""Authorized Target HTTP metadata History/Inspector projection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.traffic import (
    TRAFFIC_SAFE_MEDIA_TYPES,
    InvalidTrafficCursorError,
    StaleTrafficCursorError,
    TrafficApprovalAvailability,
    TrafficApprovalReference,
    TrafficArtifactMetadata,
    TrafficArtifactPresence,
    TrafficArtifactSet,
    TrafficAvailability,
    TrafficBodyAvailability,
    TrafficBodyMetadata,
    TrafficBodySet,
    TrafficCreatedBy,
    TrafficCreatedByKind,
    TrafficExchangeDetail,
    TrafficExchangePage,
    TrafficExchangeSource,
    TrafficExchangeView,
    TrafficGovernance,
    TrafficLineage,
    TrafficMetadataCapability,
    TrafficPageKey,
    TrafficProjectionQuality,
    TrafficRedirectSummary,
    TrafficReplayReference,
    TrafficResponseMetadata,
    TrafficSafetyGateReference,
    TrafficScope,
    TrafficScopeDecision,
    TrafficScopeSource,
    TrafficSnapshot,
    TrafficSnapshotSource,
    TrafficSourceContractError,
    TrafficSourcePage,
    TrafficStatusClass,
    TrafficTlsAvailability,
    TrafficTlsSummary,
    TrafficUrlSummary,
)
from riftx.domain import ApprovalStatus, LocalPrincipal, NodeStatus

_MAX_LIMIT = 100
_MAX_CURSOR_LENGTH = 4096
_CURSOR_DOMAIN = b"riftx-traffic-cursor-v1\0"
_PRINCIPAL_DOMAIN = b"riftx-traffic-principal-v1\0"
_SNAPSHOT_DOMAIN = b"riftx-traffic-snapshot-v1\0"
_METHOD = re.compile(r"[A-Z][A-Z-]{0,31}")


class _TrafficReadRepository(Protocol):
    async def resolve_scope(self, run_id: str) -> TrafficScopeSource | None: ...

    async def list_page(
        self,
        scope: TrafficScopeSource,
        *,
        method: str | None,
        status_class: TrafficStatusClass | None,
        limit: int,
        after: TrafficPageKey | None,
        snapshot: TrafficPageKey | None,
    ) -> TrafficSourcePage: ...

    async def get(
        self,
        scope: TrafficScopeSource,
        exchange_id: str,
    ) -> TrafficExchangeSource | None: ...


class _TrafficObjectAuthorizer(Protocol):
    def require_traffic_metadata(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        parent_engagement_id: str,
        resource_engagement_id: str | None,
        capability: TrafficMetadataCapability,
    ) -> None: ...


class TrafficMetadataApplicationService:
    """Project safe metadata without touching Runner or Artifact body services."""

    def __init__(
        self,
        repository: _TrafficReadRepository,
        *,
        authorizer: _TrafficObjectAuthorizer,
        cursor_signing_key: bytes,
    ) -> None:
        if type(cursor_signing_key) is not bytes or len(cursor_signing_key) < 32:
            raise ValueError("Traffic cursor signing key must contain at least 32 bytes")
        self._repository = repository
        self._authorizer = authorizer
        self._cursor_signing_key = cursor_signing_key

    async def list(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        method: str | None = None,
        status_class: TrafficStatusClass | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> TrafficExchangePage:
        normalized_method = _normalize_method(method)
        normalized_status = _normalize_status_class(status_class)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"Traffic list limit must be between 1 and {_MAX_LIMIT}")

        scope = await self._require_scope(run_id, principal)
        after: TrafficPageKey | None = None
        requested_snapshot: TrafficPageKey | None = None
        expected_snapshot_id: str | None = None
        if cursor is not None:
            after, requested_snapshot, expected_snapshot_id = _decode_cursor(
                cursor,
                signing_key=self._cursor_signing_key,
                principal=principal,
                scope=scope,
                method=normalized_method,
                status_class=normalized_status,
                limit=limit,
            )

        source_page = await self._repository.list_page(
            scope,
            method=normalized_method,
            status_class=normalized_status,
            limit=limit,
            after=after,
            snapshot=requested_snapshot,
        )
        _validate_page_contract(
            source_page,
            scope=scope,
            limit=limit,
            after=after,
            requested_snapshot=requested_snapshot,
        )
        snapshot_id = _snapshot_id(
            scope=scope,
            method=normalized_method,
            status_class=normalized_status,
            snapshot=source_page.snapshot,
        )
        if expected_snapshot_id is not None and not hmac.compare_digest(
            expected_snapshot_id,
            snapshot_id,
        ):
            raise StaleTrafficCursorError()

        items = source_page.items[:limit]
        views = tuple(_safe_project(item) for item in items)
        has_more = source_page.has_more or len(source_page.items) > limit
        next_cursor = None
        if has_more and items and source_page.snapshot.boundary is not None:
            last = items[-1]
            next_cursor = _encode_cursor(
                signing_key=self._cursor_signing_key,
                principal=principal,
                scope=scope,
                method=normalized_method,
                status_class=normalized_status,
                limit=limit,
                after=TrafficPageKey(last.created_at, last.exchange_id),
                snapshot=source_page.snapshot.boundary,
                snapshot_id=snapshot_id,
            )
        partial_reasons = tuple(
            sorted({reason for item in views for reason in item.partial_reasons})
        )
        return TrafficExchangePage(
            scope=TrafficScope(run_id=scope.run_id, engagement_id=scope.engagement_id),
            snapshot=TrafficSnapshot(
                id=snapshot_id,
                created_through=(
                    source_page.snapshot.boundary.created_at
                    if source_page.snapshot.boundary is not None
                    else None
                ),
            ),
            items=views,
            truncated=False,
            has_more=has_more,
            next_cursor=next_cursor,
            partial=bool(partial_reasons),
            partial_reasons=partial_reasons,
        )

    async def get(
        self,
        run_id: str,
        exchange_id: str,
        *,
        principal: LocalPrincipal,
    ) -> TrafficExchangeDetail:
        scope = await self._require_scope(run_id, principal)
        source = await self._repository.get(scope, exchange_id)
        if source is None or source.exchange_id != exchange_id or source.run_id != run_id:
            raise _resource_not_accessible()
        self._authorize(principal, scope, source.run_id)
        return TrafficExchangeDetail(
            scope=TrafficScope(run_id=scope.run_id, engagement_id=scope.engagement_id),
            item=_safe_project(source),
        )

    async def _require_scope(
        self,
        run_id: str,
        principal: LocalPrincipal,
    ) -> TrafficScopeSource:
        scope = await self._repository.resolve_scope(run_id)
        if scope is None or scope.run_id != run_id or not scope.engagement_id:
            raise _resource_not_accessible()
        self._authorize(principal, scope, scope.run_id)
        return scope

    def _authorize(
        self,
        principal: LocalPrincipal,
        scope: TrafficScopeSource,
        resource_run_id: str | None,
    ) -> None:
        self._authorizer.require_traffic_metadata(
            principal,
            parent_run_id=scope.run_id,
            resource_run_id=resource_run_id,
            parent_engagement_id=scope.engagement_id,
            resource_engagement_id=(scope.engagement_id if resource_run_id is not None else None),
            capability=TrafficMetadataCapability.READ,
        )


def _safe_project(source: TrafficExchangeSource) -> TrafficExchangeView:
    try:
        return _project(source)
    except TrafficSourceContractError:
        raise
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        raise TrafficSourceContractError("Traffic source contract is invalid") from None


def _project(source: TrafficExchangeSource) -> TrafficExchangeView:
    _validate_source(source)
    partial_reasons: set[str] = {
        "replay_lineage_not_persisted",
        "retention_unmanaged",
        "safety_gate_not_implemented",
        "scope_decision_not_persisted",
    }

    safe_url = _safe_url(source)
    if safe_url is None:
        partial_reasons.add("url_metadata_unavailable")
        url_summary = TrafficUrlSummary(
            availability=TrafficAvailability.UNAVAILABLE,
            scheme=None,
            origin=None,
            path_shape=None,
            path_segment_count=None,
        )
    else:
        scheme, origin, path_shape, segment_count = safe_url
        url_summary = TrafficUrlSummary(
            availability=TrafficAvailability.AVAILABLE,
            scheme=scheme,
            origin=origin,
            path_shape=path_shape,
            path_segment_count=segment_count,
        )

    if source.safe_metadata_version == 1 and _safe_redirects(source):
        redirect = TrafficRedirectSummary(
            availability=TrafficAvailability.AVAILABLE,
            count=source.redirect_count,
            followed=(source.redirect_count or 0) > 0,
            origins=source.redirect_origins or (),
            partial=False,
        )
    else:
        partial_reasons.add("redirect_metadata_unavailable")
        redirect = TrafficRedirectSummary(
            availability=TrafficAvailability.UNAVAILABLE,
            count=None,
            followed=None,
            origins=(),
            partial=True,
        )

    request_body_state = _body_state(source.request_body_availability)
    if request_body_state is TrafficBodyAvailability.UNKNOWN:
        partial_reasons.add("request_body_availability_unknown")
    request_artifact = _artifact_metadata(
        source.request_artifact_ref,
        recorded=source.request_artifact_recorded,
        present=source.request_artifact_present,
    )
    response_artifact = _artifact_metadata(
        source.response_artifact_ref,
        recorded=source.response_artifact_recorded,
        present=source.response_artifact_present,
    )
    if request_artifact.presence is TrafficArtifactPresence.RECORDED_MISSING:
        partial_reasons.add("request_artifact_missing")
    if response_artifact.presence is TrafficArtifactPresence.RECORDED_MISSING:
        partial_reasons.add("response_artifact_missing")

    response_body_state = (
        TrafficBodyAvailability.PRESENT
        if response_artifact.presence is TrafficArtifactPresence.RECORDED_PRESENT
        else TrafficBodyAvailability.UNKNOWN
    )
    if response_body_state is TrafficBodyAvailability.UNKNOWN:
        partial_reasons.add("response_body_availability_unknown")

    resolved_node_status = _node_status(source.node_status)
    if source.node_status is None or (
        resolved_node_status is NodeStatus.UNKNOWN
        and source.node_status != NodeStatus.UNKNOWN.value
    ):
        partial_reasons.add("node_status_unavailable")
    elif resolved_node_status is NodeStatus.UNKNOWN:
        partial_reasons.add("node_status_unknown")

    if source.intent_lineage_exact:
        created_by = TrafficCreatedBy(
            availability=TrafficAvailability.AVAILABLE,
            kind=TrafficCreatedByKind.AGENT_RUNTIME,
        )
    else:
        partial_reasons.add("creator_lineage_unavailable")
        created_by = TrafficCreatedBy(
            availability=TrafficAvailability.UNAVAILABLE,
            kind=TrafficCreatedByKind.UNKNOWN,
        )

    approval = _approval(source)
    if approval.availability is TrafficApprovalAvailability.UNAVAILABLE:
        partial_reasons.add("approval_metadata_unavailable")

    content_type = source.content_type
    if source.content_type_redacted:
        partial_reasons.add("content_type_redacted")

    tls = _tls_summary(source, partial_reasons)

    reasons = tuple(sorted(partial_reasons))
    return TrafficExchangeView(
        exchange_id=source.exchange_id,
        request_id=source.exchange_id,
        execution_key=source.execution_key,
        canonical_request_digest=source.canonical_request_digest,
        lineage=TrafficLineage(
            run_id=source.run_id,
            session_id=source.session_id,
            tool_call_id=source.tool_call_id,
            node_id=source.node_id,
            node_status=resolved_node_status,
        ),
        method=source.method,
        url_summary=url_summary,
        tls=tls,
        response=TrafficResponseMetadata(
            status_code=source.status_code,
            status_class=_status_class(source.status_code),
            elapsed_ms=source.elapsed_ms,
            content_type=content_type,
            content_length=source.content_length,
            truncated=source.response_truncated,
        ),
        artifacts=TrafficArtifactSet(
            request=request_artifact,
            response=response_artifact,
        ),
        body=TrafficBodySet(
            request=TrafficBodyMetadata(
                availability=request_body_state,
                truncated=False,
            ),
            response=TrafficBodyMetadata(
                availability=response_body_state,
                truncated=source.response_truncated,
            ),
        ),
        redirect=redirect,
        replay_of=TrafficReplayReference(),
        created_by=created_by,
        created_at=source.created_at,
        scope_decision=TrafficScopeDecision(),
        approval=approval,
        safety_gate=TrafficSafetyGateReference(),
        governance=TrafficGovernance(),
        projection_quality=(
            TrafficProjectionQuality.PARTIAL if reasons else TrafficProjectionQuality.EXACT
        ),
        partial_reasons=reasons,
    )


def _approval(source: TrafficExchangeSource) -> TrafficApprovalReference:
    try:
        approval_status = ApprovalStatus(source.approval_status)
    except (TypeError, ValueError):
        approval_status = None
    if source.approval_reference_id is not None and approval_status is not None:
        return TrafficApprovalReference(
            availability=TrafficApprovalAvailability.AVAILABLE,
            reference_id=source.approval_reference_id,
            status=approval_status,
        )
    if source.intent_lineage_exact and source.intent_approval_level == "never":
        return TrafficApprovalReference(
            availability=TrafficApprovalAvailability.NOT_REQUIRED,
            reference_id=None,
            status=None,
        )
    return TrafficApprovalReference(
        availability=TrafficApprovalAvailability.UNAVAILABLE,
        reference_id=None,
        status=None,
    )


def _artifact_metadata(
    opaque_ref: str | None,
    *,
    recorded: bool,
    present: bool,
) -> TrafficArtifactMetadata:
    if not recorded:
        return TrafficArtifactMetadata(
            opaque_ref=None,
            presence=TrafficArtifactPresence.NOT_RECORDED,
        )
    return TrafficArtifactMetadata(
        opaque_ref=opaque_ref,
        presence=(
            TrafficArtifactPresence.RECORDED_PRESENT
            if present
            else TrafficArtifactPresence.RECORDED_MISSING
        ),
    )


def _body_state(value: str | None) -> TrafficBodyAvailability:
    try:
        return TrafficBodyAvailability(value)
    except (TypeError, ValueError):
        return TrafficBodyAvailability.UNKNOWN


def _safe_url(source: TrafficExchangeSource) -> tuple[str, str, str, int] | None:
    if source.safe_metadata_version != 1:
        return None
    if (
        source.url_scheme not in {"http", "https"}
        or source.url_origin is None
        or not _valid_origin(source.url_origin, expected_scheme=source.url_scheme)
        or source.url_path_shape not in {"/", "/…"}
        or type(source.url_path_segment_count) is not int
        or not 0 <= source.url_path_segment_count <= 4096
    ):
        return None
    return (
        source.url_scheme,
        source.url_origin,
        source.url_path_shape,
        source.url_path_segment_count,
    )


def _safe_redirects(source: TrafficExchangeSource) -> bool:
    count = source.redirect_count
    origins = source.redirect_origins
    return (
        type(count) is int
        and 0 <= count <= 10
        and origins is not None
        and len(origins) == count
        and all(isinstance(origin, str) and _valid_origin(origin) for origin in origins)
    )


def _node_status(value: str | None) -> NodeStatus:
    try:
        return NodeStatus(value)
    except (TypeError, ValueError):
        return NodeStatus.UNKNOWN


def _tls_summary(
    source: TrafficExchangeSource,
    partial_reasons: set[str],
) -> TrafficTlsSummary:
    if source.url_scheme == "http" and source.safe_metadata_version == 1:
        return TrafficTlsSummary(
            availability=TrafficTlsAvailability.NOT_APPLICABLE,
            verified=None,
            client_certificate_used=None,
        )
    if type(source.tls_verified) is bool and type(source.tls_client_certificate_used) is bool:
        return TrafficTlsSummary(
            availability=TrafficTlsAvailability.AVAILABLE,
            verified=source.tls_verified,
            client_certificate_used=source.tls_client_certificate_used,
        )
    partial_reasons.add("tls_metadata_unavailable")
    return TrafficTlsSummary(
        availability=TrafficTlsAvailability.UNAVAILABLE,
        verified=None,
        client_certificate_used=None,
    )


def _status_class(status_code: int) -> TrafficStatusClass:
    return {
        1: TrafficStatusClass.INFORMATIONAL,
        2: TrafficStatusClass.SUCCESS,
        3: TrafficStatusClass.REDIRECT,
        4: TrafficStatusClass.CLIENT_ERROR,
        5: TrafficStatusClass.SERVER_ERROR,
    }[status_code // 100]


def _normalize_method(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _METHOD.fullmatch(value) is None:
        raise ValueError("Traffic method filter is invalid")
    return value


def _normalize_status_class(value: TrafficStatusClass | None) -> TrafficStatusClass | None:
    if value is None:
        return None
    try:
        return TrafficStatusClass(value)
    except (TypeError, ValueError):
        raise ValueError("Traffic status class filter is invalid") from None


def _validate_source(source: TrafficExchangeSource) -> None:
    try:
        TrafficLineage(
            run_id=source.run_id,
            session_id=source.session_id,
            tool_call_id=source.tool_call_id,
            node_id=source.node_id,
            node_status=_node_status(source.node_status),
        )
        if _METHOD.fullmatch(source.method) is None:
            raise ValueError
        _safe_reference(source.execution_key, maximum=255)
        if not re.fullmatch(r"[0-9a-f]{64}", source.canonical_request_digest):
            raise ValueError
        if not 100 <= source.status_code <= 599 or source.elapsed_ms < 0:
            raise ValueError
        if source.content_length is not None and source.content_length < 0:
            raise ValueError
        if source.content_type is not None and source.content_type not in TRAFFIC_SAFE_MEDIA_TYPES:
            raise ValueError
        if source.created_at.tzinfo is None or source.created_at.utcoffset() is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise TrafficSourceContractError("Traffic source contract is invalid") from None


def _validate_page_contract(
    page: TrafficSourcePage,
    *,
    scope: TrafficScopeSource,
    limit: int,
    after: TrafficPageKey | None,
    requested_snapshot: TrafficPageKey | None,
) -> None:
    try:
        if (
            type(page.has_more) is not bool
            or len(page.items) > limit + 1
            or type(page.snapshot.total) is not int
            or page.snapshot.total < 0
        ):
            raise ValueError
        if requested_snapshot != page.snapshot.boundary:
            if requested_snapshot is not None:
                raise ValueError
        keys = [(item.created_at.astimezone(UTC), item.exchange_id) for item in page.items]
        if any(previous <= current for previous, current in zip(keys, keys[1:], strict=False)):
            raise ValueError
        if len({item.exchange_id for item in page.items}) != len(page.items):
            raise ValueError
        for item in page.items:
            if item.run_id != scope.run_id:
                raise ValueError
            _validate_source(item)
        boundary = page.snapshot.boundary
        if boundary is None:
            if page.snapshot.total != 0 or page.items or page.has_more:
                raise ValueError
        else:
            boundary_key = (boundary.created_at.astimezone(UTC), boundary.exchange_id)
            if any(key > boundary_key for key in keys):
                raise ValueError
        if after is not None:
            after_key = (after.created_at.astimezone(UTC), after.exchange_id)
            if any(key >= after_key for key in keys):
                raise ValueError
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise TrafficSourceContractError("Traffic page contract is invalid") from None


def _snapshot_id(
    *,
    scope: TrafficScopeSource,
    method: str | None,
    status_class: TrafficStatusClass | None,
    snapshot: TrafficSnapshotSource,
) -> str:
    body = {
        "boundary": _key_payload(snapshot.boundary),
        "engagement_id": scope.engagement_id,
        "method": method,
        "run_id": scope.run_id,
        "status_class": status_class.value if status_class is not None else None,
        "total": snapshot.total,
        "version": 1,
    }
    return hashlib.sha256(_SNAPSHOT_DOMAIN + _canonical_json(body)).hexdigest()


def _encode_cursor(
    *,
    signing_key: bytes,
    principal: LocalPrincipal,
    scope: TrafficScopeSource,
    method: str | None,
    status_class: TrafficStatusClass | None,
    limit: int,
    after: TrafficPageKey,
    snapshot: TrafficPageKey,
    snapshot_id: str,
) -> str:
    body = {
        "after": _key_payload(after),
        "engagement_id": scope.engagement_id,
        "filter": {
            "method": method,
            "status_class": status_class.value if status_class is not None else None,
        },
        "limit": limit,
        "principal": _principal_binding(signing_key, principal),
        "run_id": scope.run_id,
        "snapshot": _key_payload(snapshot),
        "snapshot_id": snapshot_id,
        "version": 1,
    }
    encoded = _canonical_json(body)
    signature = hmac.new(signing_key, _CURSOR_DOMAIN + encoded, hashlib.sha256).hexdigest()
    envelope = _canonical_json({"body": body, "signature": signature})
    return base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    signing_key: bytes,
    principal: LocalPrincipal,
    scope: TrafficScopeSource,
    method: str | None,
    status_class: TrafficStatusClass | None,
    limit: int,
) -> tuple[TrafficPageKey, TrafficPageKey, str]:
    try:
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise ValueError
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4),
            altchars=b"-_",
            validate=True,
        )
        envelope = json.loads(raw, object_pairs_hook=_reject_duplicates)
        if not isinstance(envelope, dict) or set(envelope) != {"body", "signature"}:
            raise ValueError
        body = envelope["body"]
        signature = envelope["signature"]
        if not isinstance(body, dict) or set(body) != {
            "after",
            "engagement_id",
            "filter",
            "limit",
            "principal",
            "run_id",
            "snapshot",
            "snapshot_id",
            "version",
        }:
            raise ValueError
        expected = hmac.new(
            signing_key,
            _CURSOR_DOMAIN + _canonical_json(body),
            hashlib.sha256,
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError
        expected_filter = {
            "method": method,
            "status_class": status_class.value if status_class is not None else None,
        }
        if (
            type(body["version"]) is not int
            or body["version"] != 1
            or body["run_id"] != scope.run_id
            or body["engagement_id"] != scope.engagement_id
            or body["filter"] != expected_filter
            or type(body["limit"]) is not int
            or body["limit"] != limit
            or body["principal"] != _principal_binding(signing_key, principal)
            or not isinstance(body["snapshot_id"], str)
            or re.fullmatch(r"[0-9a-f]{64}", body["snapshot_id"]) is None
        ):
            raise ValueError
        after = _parse_key(body["after"])
        snapshot = _parse_key(body["snapshot"])
        if (after.created_at, after.exchange_id) > (
            snapshot.created_at,
            snapshot.exchange_id,
        ):
            raise ValueError
        return after, snapshot, body["snapshot_id"]
    except (
        json.JSONDecodeError,
        OverflowError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ):
        raise InvalidTrafficCursorError() from None


def _principal_binding(key: bytes, principal: LocalPrincipal) -> str:
    payload = _canonical_json(
        {
            "id": principal.id,
            "namespace": principal.namespace_id,
            "profile": principal.profile.value,
        }
    )
    return hmac.new(key, _PRINCIPAL_DOMAIN + payload, hashlib.sha256).hexdigest()


def _key_payload(key: TrafficPageKey | None) -> dict[str, str] | None:
    if key is None:
        return None
    return {
        "created_at": key.created_at.astimezone(UTC).isoformat(),
        "exchange_id": key.exchange_id,
    }


def _parse_key(value: object) -> TrafficPageKey:
    if not isinstance(value, dict) or set(value) != {"created_at", "exchange_id"}:
        raise ValueError
    created_at_raw = value["created_at"]
    exchange_id = value["exchange_id"]
    if not isinstance(created_at_raw, str) or len(created_at_raw) > 64:
        raise ValueError
    if not isinstance(exchange_id, str) or len(exchange_id) > 256:
        raise ValueError
    created_at = datetime.fromisoformat(created_at_raw)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError
    _safe_reference(exchange_id, maximum=256)
    return TrafficPageKey(created_at=created_at, exchange_id=exchange_id)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate cursor field")
        value[key] = item
    return value


def _safe_reference(value: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError
    return value


def _valid_origin(value: str, *, expected_scheme: str | None = None) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or (expected_scheme is not None and parsed.scheme != expected_scheme)
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    rendered = f"{parsed.scheme}://{host}"
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        rendered = f"{rendered}:{port}"
    return rendered == value


def _resource_not_accessible() -> ResourceNotAccessibleError:
    return ResourceNotAccessibleError(
        "resource_not_accessible",
        "The requested resource was not found",
    )


__all__ = ["TrafficMetadataApplicationService"]
