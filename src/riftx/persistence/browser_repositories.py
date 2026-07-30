"""SQLAlchemy persistence for managed browser sessions and observations."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import (
    BrowserAction,
    BrowserActionStatus,
    BrowserActionType,
    BrowserMode,
    BrowserObservation,
    BrowserOwner,
    BrowserPage,
    BrowserPageStatus,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTakeoverSummary,
)

from .orm import (
    BrowserActionRecord,
    BrowserObservationRecord,
    BrowserPageRecord,
    BrowserSessionRecord,
    BrowserTakeoverSummaryRecord,
)
from .repositories import SessionFactory


class SQLAlchemyBrowserRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create_session(self, item: BrowserSession) -> BrowserSession:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_session_to_record(item))
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create browser session {item.id!r}"
            ) from exc
        return item

    async def get_session(self, session_id: str) -> BrowserSession | None:
        async with self._session_factory() as session:
            row = await session.get(BrowserSessionRecord, session_id)
        return _session_from_record(row) if row is not None else None

    async def save_session(self, item: BrowserSession) -> BrowserSession:
        async with self._session_factory() as session, session.begin():
            row = await session.get(BrowserSessionRecord, item.id)
            if row is None:
                raise EntityNotFoundError("BrowserSession", item.id)
            _apply_session(item, row)
            await session.flush()
        return item

    async def list_sessions_for_run(self, run_id: str) -> Sequence[BrowserSession]:
        statement = (
            select(BrowserSessionRecord)
            .where(BrowserSessionRecord.run_id == run_id)
            .order_by(BrowserSessionRecord.created_at, BrowserSessionRecord.id)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_session_from_record(row) for row in rows]

    async def save_pages(self, pages: Sequence[BrowserPage]) -> list[BrowserPage]:
        async with self._session_factory() as session, session.begin():
            for item in pages:
                row = await session.get(BrowserPageRecord, item.id)
                if row is None:
                    session.add(_page_to_record(item))
                else:
                    row.url = item.url
                    row.title = item.title
                    row.status = item.status.value
                    row.last_observation_version = max(
                        row.last_observation_version, item.last_observation_version
                    )
                    row.closed_at = item.closed_at
            await session.flush()
        return list(pages)

    async def get_page(self, page_id: str) -> BrowserPage | None:
        async with self._session_factory() as session:
            row = await session.get(BrowserPageRecord, page_id)
        return _page_from_record(row) if row is not None else None

    async def list_pages(self, session_id: str) -> Sequence[BrowserPage]:
        statement = (
            select(BrowserPageRecord)
            .where(BrowserPageRecord.browser_session_id == session_id)
            .order_by(BrowserPageRecord.created_at, BrowserPageRecord.id)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_page_from_record(row) for row in rows]

    async def create_observation(self, item: BrowserObservation) -> BrowserObservation:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_observation_to_record(item))
                page = await session.get(BrowserPageRecord, item.page_id)
                if page is None:
                    raise EntityNotFoundError("BrowserPage", item.page_id)
                page.url = item.url
                page.title = item.title
                page.last_observation_version = item.observation_version
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get_observation_version(
                item.browser_session_id, item.observation_version
            )
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not create browser observation {item.id!r}"
            ) from exc
        return item

    async def get_observation(self, observation_id: str) -> BrowserObservation | None:
        async with self._session_factory() as session:
            row = await session.get(BrowserObservationRecord, observation_id)
        return _observation_from_record(row) if row is not None else None

    async def get_observation_version(
        self, session_id: str, version: int
    ) -> BrowserObservation | None:
        statement = select(BrowserObservationRecord).where(
            BrowserObservationRecord.browser_session_id == session_id,
            BrowserObservationRecord.observation_version == version,
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _observation_from_record(row) if row is not None else None

    async def latest_observation(
        self, session_id: str, page_id: str | None = None
    ) -> BrowserObservation | None:
        statement = select(BrowserObservationRecord).where(
            BrowserObservationRecord.browser_session_id == session_id
        )
        if page_id is not None:
            statement = statement.where(BrowserObservationRecord.page_id == page_id)
        statement = statement.order_by(
            BrowserObservationRecord.observation_version.desc()
        ).limit(1)
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _observation_from_record(row) if row is not None else None

    async def observations_after(
        self, session_id: str, version: int, *, limit: int = 100
    ) -> Sequence[BrowserObservation]:
        statement = (
            select(BrowserObservationRecord)
            .where(
                BrowserObservationRecord.browser_session_id == session_id,
                BrowserObservationRecord.observation_version > version,
            )
            .order_by(BrowserObservationRecord.observation_version)
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        return [_observation_from_record(row) for row in rows]

    async def create_action(self, item: BrowserAction) -> BrowserAction:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(_action_to_record(item))
                await session.flush()
        except IntegrityError as exc:
            existing = await self.get_action(item.browser_session_id, item.action_key)
            if existing is not None:
                return existing
            raise RepositoryConflictError(
                f"could not create browser action {item.id!r}"
            ) from exc
        return item

    async def get_action(self, session_id: str, action_key: str) -> BrowserAction | None:
        statement = select(BrowserActionRecord).where(
            BrowserActionRecord.browser_session_id == session_id,
            BrowserActionRecord.action_key == action_key,
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return _action_from_record(row) if row is not None else None

    async def save_action(self, item: BrowserAction) -> BrowserAction:
        async with self._session_factory() as session, session.begin():
            row = await session.get(BrowserActionRecord, item.id)
            if row is None:
                raise EntityNotFoundError("BrowserAction", item.id)
            row.status = item.status.value
            row.result_observation_id = item.result_observation_id
            row.download_artifact_id = item.download_artifact_id
            row.error = item.error
            row.completed_at = item.completed_at
            await session.flush()
        return item

    async def create_takeover_summary(
        self, item: BrowserTakeoverSummary
    ) -> BrowserTakeoverSummary:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    BrowserTakeoverSummaryRecord(
                        id=item.id,
                        run_id=item.run_id,
                        browser_session_id=item.browser_session_id,
                        summary_json=item.model_dump(mode="json"),
                        released_at=item.released_at,
                    )
                )
                await session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(
                f"could not create browser takeover summary {item.id!r}"
            ) from exc
        return item


def _session_to_record(item: BrowserSession) -> BrowserSessionRecord:
    return BrowserSessionRecord(
        id=item.id,
        run_id=item.run_id,
        agent_session_id=item.agent_session_id,
        node_id=item.node_id,
        mode=item.mode.value,
        status=item.status.value,
        owner=item.owner.value,
        browser_type=item.browser_type,
        profile_id=item.profile_id,
        profile_path=item.profile_path,
        cdp_endpoint=item.cdp_endpoint,
        current_page_id=item.current_page_id,
        page_ids_json=item.page_ids,
        takeover_started_at=item.takeover_started_at,
        takeover_observation_version=item.takeover_observation_version,
        created_at=item.created_at,
        closed_at=item.closed_at,
    )


def _apply_session(item: BrowserSession, row: BrowserSessionRecord) -> None:
    row.status = item.status.value
    row.owner = item.owner.value
    row.profile_path = item.profile_path
    row.current_page_id = item.current_page_id
    row.page_ids_json = item.page_ids
    row.takeover_started_at = item.takeover_started_at
    row.takeover_observation_version = item.takeover_observation_version
    row.closed_at = item.closed_at


def _session_from_record(row: BrowserSessionRecord) -> BrowserSession:
    return BrowserSession(
        id=row.id,
        run_id=row.run_id,
        agent_session_id=row.agent_session_id,
        node_id=row.node_id,
        mode=BrowserMode(row.mode),
        status=BrowserSessionStatus(row.status),
        owner=BrowserOwner(row.owner),
        browser_type=row.browser_type,
        profile_id=row.profile_id,
        profile_path=row.profile_path,
        cdp_endpoint=row.cdp_endpoint,
        current_page_id=row.current_page_id,
        page_ids=list(row.page_ids_json or []),
        takeover_started_at=row.takeover_started_at,
        takeover_observation_version=row.takeover_observation_version,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _page_to_record(item: BrowserPage) -> BrowserPageRecord:
    return BrowserPageRecord(
        id=item.id,
        browser_session_id=item.browser_session_id,
        url=item.url,
        title=item.title,
        status=item.status.value,
        last_observation_version=item.last_observation_version,
        created_at=item.created_at,
        closed_at=item.closed_at,
    )


def _page_from_record(row: BrowserPageRecord) -> BrowserPage:
    return BrowserPage(
        id=row.id,
        browser_session_id=row.browser_session_id,
        url=row.url,
        title=row.title,
        status=BrowserPageStatus(row.status),
        last_observation_version=row.last_observation_version,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _observation_to_record(item: BrowserObservation) -> BrowserObservationRecord:
    return BrowserObservationRecord(
        id=item.id,
        browser_session_id=item.browser_session_id,
        page_id=item.page_id,
        url=item.url,
        title=item.title,
        visible_text_excerpt=item.visible_text_excerpt,
        headings_json=item.headings,
        interactive_elements_json=[
            value.model_dump(mode="json") for value in item.interactive_elements
        ],
        forms_json=[value.model_dump(mode="json") for value in item.forms],
        alerts_json=item.alerts,
        console_errors_json=item.console_errors,
        network_summary_json=[
            value.model_dump(mode="json") for value in item.recent_network_summary
        ],
        screenshot_artifact_id=item.screenshot_artifact_id,
        network_artifact_id=item.network_artifact_id,
        dom_artifact_id=item.dom_artifact_id,
        observation_version=item.observation_version,
        content_trust=item.content_trust,
        created_at=item.created_at,
    )


def _observation_from_record(row: BrowserObservationRecord) -> BrowserObservation:
    return BrowserObservation(
        id=row.id,
        browser_session_id=row.browser_session_id,
        page_id=row.page_id,
        url=row.url,
        title=row.title,
        visible_text_excerpt=row.visible_text_excerpt,
        headings=list(row.headings_json or []),
        interactive_elements=row.interactive_elements_json or [],
        forms=row.forms_json or [],
        alerts=list(row.alerts_json or []),
        console_errors=list(row.console_errors_json or []),
        recent_network_summary=row.network_summary_json or [],
        screenshot_artifact_id=row.screenshot_artifact_id,
        network_artifact_id=row.network_artifact_id,
        dom_artifact_id=row.dom_artifact_id,
        observation_version=row.observation_version,
        content_trust=row.content_trust,
        created_at=row.created_at,
    )


def _action_to_record(item: BrowserAction) -> BrowserActionRecord:
    return BrowserActionRecord(
        id=item.id,
        action_key=item.action_key,
        browser_session_id=item.browser_session_id,
        page_id=item.page_id,
        observation_version=item.observation_version,
        action=item.action.value,
        element_ref=item.element_ref,
        value=item.value,
        url=item.url,
        options_json=item.options,
        status=item.status.value,
        result_observation_id=item.result_observation_id,
        download_artifact_id=item.download_artifact_id,
        error=item.error,
        created_at=item.created_at,
        completed_at=item.completed_at,
    )


def _action_from_record(row: BrowserActionRecord) -> BrowserAction:
    return BrowserAction(
        id=row.id,
        action_key=row.action_key,
        browser_session_id=row.browser_session_id,
        page_id=row.page_id,
        observation_version=row.observation_version,
        action=BrowserActionType(row.action),
        element_ref=row.element_ref,
        value=row.value,
        url=row.url,
        options=row.options_json or {},
        status=BrowserActionStatus(row.status),
        result_observation_id=row.result_observation_id,
        download_artifact_id=row.download_artifact_id,
        error=row.error,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )
