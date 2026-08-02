from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event

from riftx.application.traffic import TrafficPageKey, TrafficScopeSource, TrafficStatusClass
from riftx.persistence import Database
from riftx.persistence.orm import (
    ArtifactRecord,
    EngagementRecord,
    RunRecord,
    TargetHttpRequestRecord,
)
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
    SQLAlchemyTrafficMetadataReadRepository,
)
from riftx.target_http.models import TargetHttpRequest, TargetHttpResult, TargetHttpSubmission

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
DIGEST_KEY = b"traffic-persistence-digest-key-0001"
ARTIFACT_KEY = b"traffic-persistence-artifact-key-01"
SECRET_CANARIES = (
    "userinfo-canary",
    "signed-query-canary",
    "authorization-canary",
    "cookie-canary",
    "request-body-canary",
    "response-body-canary",
    "content-type-parameter-canary",
    "proxy-canary",
    "client-certificate-canary",
    "/private/artifact-path-canary",
)


async def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'traffic.db'}")
    await database.create_schema()
    async with database.session_factory() as session, session.begin():
        session.add(
            EngagementRecord(
                id="engagement-traffic",
                name="Traffic persistence",
                description="",
                authorization_reference=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add(
            RunRecord(
                id="run-traffic",
                engagement_id="engagement-traffic",
                node_id="node-traffic",
                objective="Traffic projection",
                success_criteria_json=[],
                entry_points_json=[],
                scope_json={},
                status="running",
                approval_mode="manual",
                model_profile=None,
                workspace_path="/workspace/traffic",
                temporal_workflow_id=None,
                created_at=NOW,
                started_at=NOW,
                finished_at=None,
            )
        )
    return database


def _submission(index: int, *, method: str = "GET") -> TargetHttpSubmission:
    request = TargetHttpRequest(
        execution_key=f"execution-key-{index:03d}",
        method=method,
        url=(
            "https://userinfo-canary:password@Target.Example.:443/secret/path"
            "?X-Amz-Signature=signed-query-canary#fragment"
        ),
        headers={"Authorization": "Bearer authorization-canary"},
        cookies={"session": "cookie-canary"},
        body="request-body-canary" if method == "POST" else None,
        proxy="http://proxy-canary.invalid",
        client_cert_ref="client-certificate-canary",
    )
    return TargetHttpSubmission(
        run_id="run-traffic",
        session_id="session-traffic",
        tool_call_id=f"intent-{index:03d}",
        node_id="node-traffic",
        request=request,
    )


def _result(
    submission: TargetHttpSubmission, index: int, *, status_code: int = 200
) -> TargetHttpResult:
    request = submission.request
    return TargetHttpResult(
        request_id=f"exchange-{index:03d}",
        execution_key=request.execution_key,
        request_hash=request.fingerprint,
        status_code=status_code,
        response_headers={
            "set-cookie": "cookie-canary",
            "authorization": "authorization-canary",
        },
        elapsed_ms=index,
        content_type="text/plain; secret=content-type-parameter-canary",
        content_length=999,
        body_excerpt="response-body-canary",
        request_artifact_id="artifact-request",
        response_artifact_id="artifact-response",
        tls_summary={"verified": True, "client_certificate_used": True},
        final_url=("https://userinfo-canary@target.example/final?signature=signed-query-canary"),
        redirect_chain=[
            "https://userinfo-canary@redirect.example/one?sig=signed-query-canary",
            "https://target.example/two?token=signed-query-canary",
        ],
        truncated=True,
    )


def _read_repository(database: Database) -> SQLAlchemyTrafficMetadataReadRepository:
    return SQLAlchemyTrafficMetadataReadRepository(
        database.session_factory,
        digest_key=DIGEST_KEY,
        artifact_reference_key=ARTIFACT_KEY,
    )


async def test_safe_metadata_projection_never_selects_or_returns_raw_payloads(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    writer = SQLAlchemyTargetHttpRequestRepository(database.session_factory)
    submission = _submission(0, method="POST")
    await writer.create(submission, _result(submission, 0))
    async with database.session_factory() as session, session.begin():
        session.add_all(
            [
                ArtifactRecord(
                    id="artifact-request",
                    run_id="run-traffic",
                    execution_id=None,
                    name="legacy-any-name.bin",
                    path="/private/artifact-path-canary",
                    mime_type="application/octet-stream",
                    sha256="a" * 64,
                    size=100,
                    description="authorization-canary",
                    created_at=NOW,
                ),
                ArtifactRecord(
                    id="artifact-response",
                    run_id="run-traffic",
                    execution_id=None,
                    name="legacy-response.bin",
                    path="/private/artifact-path-canary",
                    mime_type="application/octet-stream",
                    sha256="b" * 64,
                    size=200,
                    description="cookie-canary",
                    created_at=NOW,
                ),
            ]
        )

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_sql)
    repository = _read_repository(database)
    scope = await repository.resolve_scope("run-traffic")
    assert scope == TrafficScopeSource(
        run_id="run-traffic",
        engagement_id="engagement-traffic",
    )
    assert scope is not None
    page = await repository.list_page(
        scope,
        method=None,
        status_class=None,
        limit=50,
        after=None,
        snapshot=None,
    )
    event.remove(database.engine.sync_engine, "before_cursor_execute", record_sql)

    assert len(statements) == 4  # scope + boundary + count + one fixed item SELECT
    item = page.items[0]
    assert item.url_origin == "https://target.example"
    assert item.url_path_shape == "/…"
    assert item.url_path_segment_count == 2
    assert item.redirect_count == 2
    assert item.redirect_origins == (
        "https://redirect.example",
        "https://target.example",
    )
    assert item.request_body_availability == "present"
    assert item.content_type == "text/plain"
    assert item.content_type_redacted is True
    assert item.tls_verified is True
    assert item.tls_client_certificate_used is True
    assert item.canonical_request_digest != submission.request.fingerprint
    assert item.request_artifact_ref != "artifact-request"
    assert item.response_artifact_ref != "artifact-response"
    assert item.request_artifact_present is True
    assert item.response_artifact_present is True

    serialized = repr(page)
    for canary in SECRET_CANARIES:
        assert canary not in serialized
    item_sql = statements[-1].lower()
    for forbidden_select in (
        "target_http_requests.request_json",
        "target_http_requests.url",
        "artifacts.path",
        "artifacts.name",
        "artifacts.description",
    ):
        assert forbidden_select not in item_sql
    assert "json_extract(target_http_requests.result_json" in item_sql
    assert "target_http_requests.result_json as" not in item_sql
    await database.dispose()


async def test_legacy_sensitive_row_is_partial_without_reading_raw_url_or_payload(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    async with database.session_factory() as session, session.begin():
        session.add(
            TargetHttpRequestRecord(
                id="exchange-legacy",
                execution_key="execution-key-legacy",
                run_id="run-traffic",
                session_id="session-legacy",
                tool_call_id="intent-legacy",
                node_id="node-legacy",
                method="GET",
                url=("https://userinfo-canary@legacy.example/private?sig=signed-query-canary"),
                request_json={
                    "headers": {"authorization": "authorization-canary"},
                    "body_text": "request-body-canary",
                    "client_cert_ref": "client-certificate-canary",
                },
                result_json={
                    "request_id": "exchange-legacy",
                    "execution_key": "execution-key-legacy",
                    "request_hash": "c" * 64,
                    "status_code": 200,
                    "response_headers": {"set-cookie": "cookie-canary"},
                    "elapsed_ms": 4,
                    "content_type": "text/plain; secret=content-type-parameter-canary",
                    "content_length": 10,
                    "body_excerpt": "response-body-canary",
                    "final_url": "https://legacy.example/?sig=signed-query-canary",
                    "redirect_chain": ["https://redirect.example/?sig=signed-query-canary"],
                    "truncated": False,
                },
                request_artifact_id=None,
                response_artifact_id=None,
                created_at=NOW,
            )
        )
    repository = _read_repository(database)
    scope = await repository.resolve_scope("run-traffic")
    assert scope is not None
    item = await repository.get(scope, "exchange-legacy")
    assert item is not None
    assert item.safe_metadata_version is None
    assert item.url_origin is None
    assert item.redirect_origins is None
    assert item.request_body_availability is None
    assert item.content_type == "text/plain"
    assert item.content_type_redacted is True
    for canary in SECRET_CANARIES:
        assert canary not in repr(item)
    await database.dispose()


async def test_syntactically_valid_unapproved_content_type_is_value_free(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    writer = SQLAlchemyTargetHttpRequestRepository(database.session_factory)
    submission = _submission(0)
    content_type_canary = "application/x-riftx-secret-canary"
    result = _result(submission, 0).model_copy(update={"content_type": content_type_canary})
    await writer.create(submission, result)

    repository = _read_repository(database)
    scope = await repository.resolve_scope("run-traffic")
    assert scope is not None
    page = await repository.list_page(
        scope,
        method=None,
        status_class=None,
        limit=10,
        after=None,
        snapshot=None,
    )

    assert len(page.items) == 1
    assert page.items[0].content_type is None
    assert page.items[0].content_type_redacted is True
    assert content_type_canary not in repr(page)
    await database.dispose()


async def test_pagination_filter_and_query_count_are_constant_for_large_history(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    writer = SQLAlchemyTargetHttpRequestRepository(database.session_factory)
    for index in range(105):
        method = "POST" if index % 2 else "GET"
        submission = _submission(index, method=method)
        await writer.create(
            submission,
            _result(
                submission,
                index,
                status_code=201 if method == "POST" else 404,
            ),
        )
    repository = _read_repository(database)
    scope = await repository.resolve_scope("run-traffic")
    assert scope is not None

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_sql)
    first = await repository.list_page(
        scope,
        method="POST",
        status_class=TrafficStatusClass.SUCCESS,
        limit=20,
        after=None,
        snapshot=None,
    )
    event.remove(database.engine.sync_engine, "before_cursor_execute", record_sql)
    assert len(statements) == 3
    assert len(first.items) == 21
    assert first.has_more is True
    assert all(item.method == "POST" and item.status_code == 201 for item in first.items)

    visible = list(first.items[:20])
    after = visible[-1]
    second = await repository.list_page(
        scope,
        method="POST",
        status_class=TrafficStatusClass.SUCCESS,
        limit=100,
        after=type(first.snapshot.boundary)(after.created_at, after.exchange_id),
        snapshot=first.snapshot.boundary,
    )
    visible.extend(second.items[:100])
    assert len({item.exchange_id for item in visible}) == len(visible) == 52
    await database.dispose()


async def test_tied_timestamp_history_over_one_thousand_pages_without_gaps(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    total = 1_003
    tied_at = NOW + timedelta(hours=1)
    async with database.session_factory() as session, session.begin():
        session.add_all(
            [
                TargetHttpRequestRecord(
                    id=f"exchange-tied-{index:04d}",
                    execution_key=f"execution-key-tied-{index:04d}",
                    run_id="run-traffic",
                    session_id="session-tied",
                    tool_call_id=f"intent-tied-{index:04d}",
                    node_id="node-tied",
                    method="GET",
                    url="https://target.example/",
                    request_json={},
                    result_json={
                        "request_hash": f"{index + 1:064x}",
                        "status_code": 200,
                        "elapsed_ms": index,
                        "content_type": "application/json",
                        "content_length": 0,
                        "truncated": False,
                        "tls_summary": {
                            "verified": True,
                            "client_certificate_used": False,
                        },
                        "_riftx_safe_read_metadata_v1": {
                            "version": 1,
                            "url": {
                                "scheme": "https",
                                "origin": "https://target.example",
                                "path_shape": "/",
                                "path_segment_count": 0,
                            },
                            "redirect": {"count": 0, "origins": []},
                            "request_body_availability": "absent",
                        },
                    },
                    request_artifact_id=None,
                    response_artifact_id=None,
                    created_at=tied_at,
                )
                for index in range(total)
            ]
        )

    repository = _read_repository(database)
    scope = await repository.resolve_scope("run-traffic")
    assert scope is not None
    snapshot = None
    after = None
    seen: list[str] = []
    while True:
        page = await repository.list_page(
            scope,
            method=None,
            status_class=None,
            limit=100,
            after=after,
            snapshot=snapshot,
        )
        if snapshot is None:
            snapshot = page.snapshot.boundary
            assert page.snapshot.total == total
        else:
            assert page.snapshot.boundary == snapshot
        visible = page.items[:100]
        seen.extend(item.exchange_id for item in visible)
        if not page.has_more:
            break
        last = visible[-1]
        after = TrafficPageKey(last.created_at, last.exchange_id)

    expected = [f"exchange-tied-{index:04d}" for index in reversed(range(total))]
    assert seen == expected
    assert len(seen) == len(set(seen)) == total
    await database.dispose()
