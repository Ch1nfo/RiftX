"""Lifecycle service for durable Agent Sessions and their transcripts."""

from __future__ import annotations

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.domain import AgentMessage, TranscriptMessageDraft
from riftx.domain.base import new_id
from riftx.persistence.repositories import SQLAlchemyRunRepository
from riftx.persistence.runtime_repositories import (
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyProviderStateRepository,
)
from riftx.persistence.transcript_repositories import SQLAlchemyTranscriptRepository
from riftx.runtime.types import AgentSession, RuntimeStateMachine, SessionStatus

from .types import LoadedSession

_TERMINAL_STATUSES = {
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
}
_RESUMABLE_STATUSES = {
    SessionStatus.SUSPENDED,
    SessionStatus.COMPACTING,
    SessionStatus.WAITING_APPROVAL,
    SessionStatus.WAITING_USER,
}


class SessionManager:
    """Own Session lifecycle while keeping provider state an optional optimization."""

    def __init__(
        self,
        *,
        run_repository: SQLAlchemyRunRepository,
        session_repository: SQLAlchemyAgentSessionRepository,
        transcript_repository: SQLAlchemyTranscriptRepository,
        provider_state_repository: SQLAlchemyProviderStateRepository,
    ) -> None:
        self._runs = run_repository
        self._sessions = session_repository
        self._transcript = transcript_repository
        self._provider_states = provider_state_repository
        self._state_machine = RuntimeStateMachine()

    async def create_session(
        self,
        *,
        run_id: str,
        model_profile: str,
        parent_session_id: str | None = None,
        agent_type: str = "primary",
        session_id: str | None = None,
    ) -> AgentSession:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if parent_session_id is not None:
            parent = await self._sessions.get(parent_session_id)
            if parent is None:
                raise EntityNotFoundError("AgentSession", parent_session_id)
            if parent.run_id != run_id:
                raise RepositoryConflictError(
                    f"parent session {parent_session_id!r} belongs to another run"
                )
        agent_session = AgentSession(
            id=session_id or new_id(),
            run_id=run_id,
            parent_session_id=parent_session_id,
            agent_type=agent_type,
            model_profile=model_profile,
        )
        self._state_machine.transition_session(agent_session, SessionStatus.ACTIVE)
        return await self._sessions.create(agent_session)

    async def load_session(self, session_id: str) -> LoadedSession:
        agent_session = await self._require_session(session_id)
        transcript = await self._transcript.list_by_session(session_id)
        provider_state = None
        if agent_session.provider_state_id is not None:
            provider_state = await self._provider_states.get(agent_session.provider_state_id)
        return LoadedSession(
            session=agent_session,
            transcript=transcript,
            provider_state=provider_state,
        )

    async def suspend_session(
        self,
        session_id: str,
        *,
        provider_state_id: str | None = None,
    ) -> AgentSession:
        agent_session = await self._require_session(session_id)
        if agent_session.status in _TERMINAL_STATUSES:
            raise RepositoryConflictError(f"agent session {session_id!r} is already closed")
        if provider_state_id is not None:
            state = await self._provider_states.get(provider_state_id)
            if state is None:
                raise EntityNotFoundError("ProviderState", provider_state_id)
            if state.session_id != session_id:
                raise RepositoryConflictError(
                    f"provider state {provider_state_id!r} belongs to another session"
                )
            agent_session.provider_state_id = provider_state_id
        if agent_session.status is SessionStatus.SUSPENDED:
            return await self._sessions.save(agent_session)
        if agent_session.status is not SessionStatus.ACTIVE:
            raise RepositoryConflictError(
                f"agent session {session_id!r} cannot suspend from {agent_session.status.value!r}"
            )
        self._state_machine.transition_session(agent_session, SessionStatus.SUSPENDED)
        return await self._sessions.save(agent_session)

    async def resume_session(self, session_id: str) -> LoadedSession:
        agent_session = await self._require_session(session_id)
        if agent_session.status in _TERMINAL_STATUSES:
            raise RepositoryConflictError(f"agent session {session_id!r} is already closed")
        if agent_session.status in _RESUMABLE_STATUSES:
            self._state_machine.transition_session(agent_session, SessionStatus.ACTIVE)
            await self._sessions.save(agent_session)
        elif agent_session.status is not SessionStatus.ACTIVE:
            raise RepositoryConflictError(
                f"agent session {session_id!r} cannot resume from {agent_session.status.value!r}"
            )
        return await self.load_session(session_id)

    async def close_session(
        self,
        session_id: str,
        *,
        status: SessionStatus = SessionStatus.COMPLETED,
    ) -> AgentSession:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("close status must be completed, failed, or cancelled")
        agent_session = await self._require_session(session_id)
        if agent_session.status in _TERMINAL_STATUSES:
            if agent_session.status is not status:
                raise RepositoryConflictError(
                    f"agent session {session_id!r} is already closed as "
                    f"{agent_session.status.value!r}"
                )
            return agent_session
        if agent_session.status is SessionStatus.CREATED:
            self._state_machine.transition_session(agent_session, SessionStatus.ACTIVE)
        elif agent_session.status in _RESUMABLE_STATUSES:
            self._state_machine.transition_session(agent_session, SessionStatus.ACTIVE)
        self._state_machine.transition_session(agent_session, status)
        return await self._sessions.save(agent_session)

    async def append_message(
        self,
        session_id: str,
        draft: TranscriptMessageDraft,
        *,
        expected_last_sequence: int | None = None,
    ) -> AgentMessage:
        return await self._transcript.append(
            session_id,
            draft,
            expected_last_sequence=expected_last_sequence,
        )

    async def _require_session(self, session_id: str) -> AgentSession:
        agent_session = await self._sessions.get(session_id)
        if agent_session is None:
            raise EntityNotFoundError("AgentSession", session_id)
        return agent_session
