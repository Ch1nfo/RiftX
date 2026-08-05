"""Durable idempotency records for authorized Target HTTP exchanges."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from riftx.application.errors import RepositoryConflictError
from riftx.application.traffic import (
    TRAFFIC_SAFE_MEDIA_TYPES,
    TrafficExchangeSource,
    TrafficPageKey,
    TrafficScopeSource,
    TrafficSnapshotSource,
    TrafficSourceContractError,
    TrafficSourcePage,
    TrafficStatusClass,
)
from riftx.domain.base import utc_now
from riftx.target_http.models import TargetHttpResult, TargetHttpSubmission
from riftx.target_http.redaction import safe_redirect_metadata, safe_url_metadata

from .orm import (
    ArtifactRecord,
    NodeRecord,
    RunRecord,
    RuntimeApprovalRequestRecord,
    TargetHttpRequestRecord,
    ToolCallIntentRecord,
)
from .repositories import SessionFactory

_SAFE_READ_METADATA_KEY = "_riftx_safe_read_metadata_v1"
_DIGEST_DOMAIN = b"riftx-traffic-request-digest-v1\0"
_ARTIFACT_REF_DOMAIN = b"riftx-traffic-artifact-ref-v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_METHOD = re.compile(r"[A-Z][A-Z-]{0,31}")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,127}")


class SQLAlchemyTargetHttpRequestRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_by_execution_key(self, execution_key: str) -> TargetHttpResult | None:
        statement = select(TargetHttpRequestRecord).where(
            TargetHttpRequestRecord.execution_key == execution_key
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _target_result(row.result_json) if row is not None else None

    async def get_for_run(
        self,
        run_id: str,
        request_id: str,
    ) -> TargetHttpResult | None:
        statement = select(TargetHttpRequestRecord).where(
            TargetHttpRequestRecord.id == request_id,
            TargetHttpRequestRecord.run_id == run_id,
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _target_result(row.result_json) if row is not None else None

    async def create(
        self,
        submission: TargetHttpSubmission,
        result: TargetHttpResult,
    ) -> TargetHttpResult:
        if submission.request.execution_key != result.execution_key:
            raise ValueError("Target HTTP result execution key does not match its request")
        result_payload = result.model_dump(mode="json")
        result_payload[_SAFE_READ_METADATA_KEY] = _safe_read_metadata(submission, result)
        row = TargetHttpRequestRecord(
            id=result.request_id,
            execution_key=result.execution_key,
            run_id=submission.run_id,
            session_id=submission.session_id,
            tool_call_id=submission.tool_call_id,
            node_id=submission.node_id,
            method=submission.request.method,
            url=submission.request.url,
            request_json=submission.request.runner_payload(),
            result_json=result_payload,
            request_artifact_id=result.request_artifact_id,
            response_artifact_id=result.response_artifact_id,
            created_at=utc_now(),
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get_by_execution_key(result.execution_key)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not record Target HTTP request {result.request_id!r}"
            ) from exc
        return result


class SQLAlchemyTrafficMetadataReadRepository:
    """Bounded SQL projection that cannot select raw Traffic payload columns."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        digest_key: bytes,
        artifact_reference_key: bytes,
    ) -> None:
        if type(digest_key) is not bytes or len(digest_key) < 32:
            raise ValueError("Traffic digest key must contain at least 32 bytes")
        if type(artifact_reference_key) is not bytes or len(artifact_reference_key) < 32:
            raise ValueError("Traffic Artifact reference key must contain at least 32 bytes")
        if hmac.compare_digest(digest_key, artifact_reference_key):
            raise ValueError("Traffic digest and Artifact reference keys must be distinct")
        self._session_factory = session_factory
        self._digest_key = digest_key
        self._artifact_reference_key = artifact_reference_key

    async def resolve_scope(self, run_id: str) -> TrafficScopeSource | None:
        statement = select(RunRecord.id, RunRecord.engagement_id).where(RunRecord.id == run_id)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return TrafficScopeSource(run_id=row.id, engagement_id=row.engagement_id)

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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Traffic list limit must be between 1 and 100")
        filters = _traffic_filters(scope.run_id, method=method, status_class=status_class)
        async with self._session_factory() as session, session.begin():
            boundary = snapshot
            if boundary is None:
                boundary_row = (
                    await session.execute(
                        select(
                            TargetHttpRequestRecord.created_at,
                            TargetHttpRequestRecord.id,
                        )
                        .where(*filters)
                        .order_by(
                            TargetHttpRequestRecord.created_at.desc(),
                            TargetHttpRequestRecord.id.desc(),
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if boundary_row is not None:
                    boundary = TrafficPageKey(
                        created_at=boundary_row.created_at,
                        exchange_id=boundary_row.id,
                    )
            if boundary is None:
                return TrafficSourcePage(
                    snapshot=TrafficSnapshotSource(boundary=None, total=0),
                    items=(),
                    has_more=False,
                )

            bounded = (*filters, _at_or_before(boundary))
            total = int(
                await session.scalar(select(func.count(TargetHttpRequestRecord.id)).where(*bounded))
                or 0
            )
            item_filters = list(bounded)
            if after is not None:
                item_filters.append(_before(after))
            rows = (
                (
                    await session.execute(
                        _traffic_select()
                        .where(*item_filters)
                        .order_by(
                            TargetHttpRequestRecord.created_at.desc(),
                            TargetHttpRequestRecord.id.desc(),
                        )
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        items = tuple(self._source(row) for row in rows)
        return TrafficSourcePage(
            snapshot=TrafficSnapshotSource(boundary=boundary, total=total),
            items=items,
            has_more=len(items) > limit,
        )

    async def get(
        self,
        scope: TrafficScopeSource,
        exchange_id: str,
    ) -> TrafficExchangeSource | None:
        statement = _traffic_select().where(
            TargetHttpRequestRecord.run_id == scope.run_id,
            TargetHttpRequestRecord.id == exchange_id,
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).mappings().one_or_none()
        return self._source(row) if row is not None else None

    def _source(self, row: Mapping[str, object]) -> TrafficExchangeSource:
        request_hash = _required_string(row, "request_hash")
        if _SHA256.fullmatch(request_hash) is None:
            raise TrafficSourceContractError("Traffic request digest source is invalid")
        run_id = _required_string(row, "run_id")
        exchange_id = _required_string(row, "exchange_id")
        digest_payload = "\0".join((run_id, exchange_id, request_hash)).encode("utf-8")
        canonical_digest = hmac.new(
            self._digest_key,
            _DIGEST_DOMAIN + digest_payload,
            hashlib.sha256,
        ).hexdigest()

        request_artifact_id = _optional_string(row, "request_artifact_id")
        response_artifact_id = _optional_string(row, "response_artifact_id")
        raw_content_type = _optional_string(row, "content_type")
        content_type, content_type_redacted = _safe_content_type(raw_content_type)
        redirect_origins = _safe_origin_tuple(row.get("redirect_origins"))
        return TrafficExchangeSource(
            exchange_id=exchange_id,
            execution_key=_required_string(row, "execution_key"),
            run_id=run_id,
            session_id=_required_string(row, "session_id"),
            tool_call_id=_required_string(row, "tool_call_id"),
            node_id=_required_string(row, "node_id"),
            node_status=_optional_string(row, "node_status"),
            method=_required_string(row, "method"),
            canonical_request_digest=canonical_digest,
            safe_metadata_version=_optional_int(row, "safe_metadata_version"),
            url_scheme=_optional_string(row, "url_scheme"),
            url_origin=_optional_string(row, "url_origin"),
            url_path_shape=_optional_string(row, "url_path_shape"),
            url_path_segment_count=_optional_int(row, "url_path_segment_count"),
            redirect_count=_optional_int(row, "redirect_count"),
            redirect_origins=redirect_origins,
            request_body_availability=_optional_string(row, "request_body_availability"),
            status_code=_required_int(row, "status_code"),
            elapsed_ms=_required_int(row, "elapsed_ms"),
            content_type=content_type,
            content_type_redacted=content_type_redacted,
            content_length=_optional_int(row, "content_length"),
            response_truncated=_required_bool(row, "response_truncated"),
            tls_verified=_optional_bool(row, "tls_verified"),
            tls_client_certificate_used=_optional_bool(
                row,
                "tls_client_certificate_used",
            ),
            request_artifact_ref=self._artifact_ref(request_artifact_id),
            request_artifact_recorded=request_artifact_id is not None,
            request_artifact_present=row.get("request_artifact_present") is not None,
            response_artifact_ref=self._artifact_ref(response_artifact_id),
            response_artifact_recorded=response_artifact_id is not None,
            response_artifact_present=row.get("response_artifact_present") is not None,
            intent_lineage_exact=row.get("intent_id") is not None,
            intent_approval_level=_optional_string(row, "intent_approval_level"),
            approval_reference_id=_optional_string(row, "approval_reference_id"),
            approval_status=_optional_string(row, "approval_status"),
            created_at=_required_datetime(row, "created_at"),
        )

    def _artifact_ref(self, artifact_id: str | None) -> str | None:
        if artifact_id is None:
            return None
        digest = hmac.new(
            self._artifact_reference_key,
            _ARTIFACT_REF_DOMAIN + artifact_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"traffic-artifact:v1:{digest}"


def _traffic_select():
    request_artifact = aliased(ArtifactRecord, name="traffic_request_artifact")
    response_artifact = aliased(ArtifactRecord, name="traffic_response_artifact")
    result = TargetHttpRequestRecord.result_json
    safe = result[_SAFE_READ_METADATA_KEY]
    return (
        select(
            TargetHttpRequestRecord.id.label("exchange_id"),
            TargetHttpRequestRecord.execution_key.label("execution_key"),
            TargetHttpRequestRecord.run_id.label("run_id"),
            TargetHttpRequestRecord.session_id.label("session_id"),
            TargetHttpRequestRecord.tool_call_id.label("tool_call_id"),
            TargetHttpRequestRecord.node_id.label("node_id"),
            TargetHttpRequestRecord.method.label("method"),
            TargetHttpRequestRecord.request_artifact_id.label("request_artifact_id"),
            TargetHttpRequestRecord.response_artifact_id.label("response_artifact_id"),
            TargetHttpRequestRecord.created_at.label("created_at"),
            result["request_hash"].as_string().label("request_hash"),
            result["status_code"].as_integer().label("status_code"),
            result["elapsed_ms"].as_integer().label("elapsed_ms"),
            result["content_type"].as_string().label("content_type"),
            result["content_length"].as_integer().label("content_length"),
            result["truncated"].as_boolean().label("response_truncated"),
            result["tls_summary"]["verified"].as_boolean().label("tls_verified"),
            result["tls_summary"]["client_certificate_used"]
            .as_boolean()
            .label("tls_client_certificate_used"),
            safe["version"].as_integer().label("safe_metadata_version"),
            safe["url"]["scheme"].as_string().label("url_scheme"),
            safe["url"]["origin"].as_string().label("url_origin"),
            safe["url"]["path_shape"].as_string().label("url_path_shape"),
            safe["url"]["path_segment_count"].as_integer().label("url_path_segment_count"),
            safe["redirect"]["count"].as_integer().label("redirect_count"),
            safe["redirect"]["origins"].as_json().label("redirect_origins"),
            safe["request_body_availability"].as_string().label("request_body_availability"),
            NodeRecord.status.label("node_status"),
            ToolCallIntentRecord.id.label("intent_id"),
            ToolCallIntentRecord.approval_level.label("intent_approval_level"),
            RuntimeApprovalRequestRecord.id.label("approval_reference_id"),
            RuntimeApprovalRequestRecord.status.label("approval_status"),
            request_artifact.id.label("request_artifact_present"),
            response_artifact.id.label("response_artifact_present"),
        )
        .select_from(TargetHttpRequestRecord)
        .outerjoin(NodeRecord, NodeRecord.id == TargetHttpRequestRecord.node_id)
        .outerjoin(
            ToolCallIntentRecord,
            and_(
                ToolCallIntentRecord.id == TargetHttpRequestRecord.tool_call_id,
                ToolCallIntentRecord.run_id == TargetHttpRequestRecord.run_id,
                ToolCallIntentRecord.session_id == TargetHttpRequestRecord.session_id,
            ),
        )
        .outerjoin(
            RuntimeApprovalRequestRecord,
            and_(
                RuntimeApprovalRequestRecord.tool_call_intent_id == ToolCallIntentRecord.id,
                RuntimeApprovalRequestRecord.run_id == TargetHttpRequestRecord.run_id,
                RuntimeApprovalRequestRecord.session_id == TargetHttpRequestRecord.session_id,
            ),
        )
        .outerjoin(
            request_artifact,
            and_(
                request_artifact.id == TargetHttpRequestRecord.request_artifact_id,
                request_artifact.run_id == TargetHttpRequestRecord.run_id,
            ),
        )
        .outerjoin(
            response_artifact,
            and_(
                response_artifact.id == TargetHttpRequestRecord.response_artifact_id,
                response_artifact.run_id == TargetHttpRequestRecord.run_id,
            ),
        )
    )


def _traffic_filters(
    run_id: str,
    *,
    method: str | None,
    status_class: TrafficStatusClass | None,
) -> tuple[object, ...]:
    filters: list[object] = [TargetHttpRequestRecord.run_id == run_id]
    if method is not None:
        filters.append(TargetHttpRequestRecord.method == method)
    if status_class is not None:
        lower, upper = {
            TrafficStatusClass.INFORMATIONAL: (100, 199),
            TrafficStatusClass.SUCCESS: (200, 299),
            TrafficStatusClass.REDIRECT: (300, 399),
            TrafficStatusClass.CLIENT_ERROR: (400, 499),
            TrafficStatusClass.SERVER_ERROR: (500, 599),
        }[status_class]
        status = TargetHttpRequestRecord.result_json["status_code"].as_integer()
        filters.extend((status >= lower, status <= upper))
    return tuple(filters)


def _at_or_before(key: TrafficPageKey):
    return or_(
        TargetHttpRequestRecord.created_at < key.created_at,
        and_(
            TargetHttpRequestRecord.created_at == key.created_at,
            TargetHttpRequestRecord.id <= key.exchange_id,
        ),
    )


def _before(key: TrafficPageKey):
    return or_(
        TargetHttpRequestRecord.created_at < key.created_at,
        and_(
            TargetHttpRequestRecord.created_at == key.created_at,
            TargetHttpRequestRecord.id < key.exchange_id,
        ),
    )


def _safe_read_metadata(
    submission: TargetHttpSubmission,
    result: TargetHttpResult,
) -> dict[str, object]:
    return {
        "version": 1,
        "url": safe_url_metadata(submission.request.url),
        "redirect": safe_redirect_metadata(result.redirect_chain),
        "request_body_availability": (
            "present"
            if submission.request.body is not None or submission.request.json_body is not None
            else "absent"
        ),
    }


def _target_result(value: object) -> TargetHttpResult:
    if not isinstance(value, dict):
        raise TrafficSourceContractError("Target HTTP result payload is invalid")
    payload = dict(value)
    payload.pop(_SAFE_READ_METADATA_KEY, None)
    return TargetHttpResult.model_validate(payload)


def _safe_content_type(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    base = value.split(";", 1)[0].strip().lower()
    if _MEDIA_TYPE.fullmatch(base) is None or base not in TRAFFIC_SAFE_MEDIA_TYPES:
        return None, True
    return base, base != value


def _safe_origin_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _required_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise TrafficSourceContractError(f"Traffic metadata field {key} is invalid")
    return value


def _optional_string(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    return value


def _required_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int:
        raise TrafficSourceContractError(f"Traffic metadata field {key} is invalid")
    return value


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    return value if type(value) is int else None


def _required_bool(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if type(value) is not bool:
        raise TrafficSourceContractError(f"Traffic metadata field {key} is invalid")
    return value


def _optional_bool(row: Mapping[str, object], key: str) -> bool | None:
    value = row.get(key)
    return value if type(value) is bool else None


def _required_datetime(row: Mapping[str, object], key: str):
    value = row.get(key)
    if not hasattr(value, "tzinfo") or value.tzinfo is None or value.utcoffset() is None:
        raise TrafficSourceContractError(f"Traffic metadata field {key} is invalid")
    return value
