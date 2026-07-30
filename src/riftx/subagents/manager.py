"""Durable Subagent session scheduling with strict context and Tool isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from riftx.application.errors import EntityNotFoundError, RepositoryConflictError
from riftx.config import SubagentConfig
from riftx.domain import (
    MessageRole,
    MessageType,
    MessageVisibility,
    TranscriptMessageDraft,
)
from riftx.persistence.repositories import SQLAlchemyRunEventRepository
from riftx.persistence.runtime_repositories import SQLAlchemyAgentSessionRepository
from riftx.runtime.session import SessionManager
from riftx.runtime.types import AgentSession, SessionStatus
from riftx.tools import RESIDENT_TOOL_IDS, SUBAGENT_RESIDENT_TOOL_IDS, ToolContextManager

from .models import DelegationPacket, SubagentResult, SubagentStatus


class ResultMerger(Protocol):
    async def merge(self, run_id: str, result: SubagentResult) -> object: ...

_CLOSED = {
    SessionStatus.COMPLETED,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
}


class SubagentLimitError(RepositoryConflictError):
    """Raised when delegation would exceed a configured tree or run limit."""


@dataclass(frozen=True, slots=True)
class SubagentHandle:
    session: AgentSession
    delegation: DelegationPacket


class SubagentManager:
    def __init__(
        self,
        *,
        sessions: SessionManager,
        session_repository: SQLAlchemyAgentSessionRepository,
        tool_context: ToolContextManager,
        limits: SubagentConfig | None = None,
        events: SQLAlchemyRunEventRepository | None = None,
        result_merger: ResultMerger | None = None,
    ) -> None:
        self._sessions = sessions
        self._session_repository = session_repository
        self._tool_context = tool_context
        self._limits = limits or SubagentConfig()
        self._events = events
        self._result_merger = result_merger
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        *,
        parent_session_id: str,
        delegation: DelegationPacket,
        session_id: str | None = None,
    ) -> SubagentHandle:
        parent = await self._require_session(parent_session_id)
        if parent.parent_session_id is not None:
            raise SubagentLimitError("Subagents cannot delegate another Subagent")
        self._validate_tools(delegation)
        lock = self._run_locks.setdefault(parent.run_id, asyncio.Lock())
        async with lock:
            await self._enforce_run_limits(parent.run_id)
            child = await self._sessions.create_session(
                run_id=parent.run_id,
                model_profile=parent.model_profile,
                parent_session_id=parent.id,
                agent_type=f"subagent:{delegation.subagent_type}",
                session_id=session_id,
            )
            try:
                self._apply_tool_allowlist(child, delegation)
                await self._sessions.append_message(
                    child.id,
                    TranscriptMessageDraft(
                        agent_id=child.agent_type,
                        role=MessageRole.SYSTEM,
                        message_type=MessageType.SUBAGENT_DELEGATION,
                        content=delegation.task,
                        structured_content=delegation.model_dump(mode="json"),
                        visibility=MessageVisibility.SUBAGENT_PRIVATE,
                    ),
                )
            except Exception:
                await self._sessions.close_session(child.id, status=SessionStatus.FAILED)
                raise
        await self._append_event(
            parent.run_id,
            "subagent.started",
            {
                "task_id": delegation.task_id,
                "session_id": child.id,
                "parent_session_id": parent.id,
                "subagent_type": delegation.subagent_type,
            },
        )
        return SubagentHandle(child, delegation)

    async def recover(self, session_id: str) -> SubagentHandle:
        session = await self._require_session(session_id)
        if session.parent_session_id is None:
            raise RepositoryConflictError(f"session {session_id!r} is not a Subagent")
        delegation = await self._load_delegation(session_id)
        self._apply_tool_allowlist(session, delegation)
        return SubagentHandle(session, delegation)

    async def complete(self, session_id: str, result: SubagentResult) -> AgentSession:
        session = await self._require_session(session_id)
        if session.parent_session_id is None:
            raise RepositoryConflictError(f"session {session_id!r} is not a Subagent")
        delegation = await self._load_delegation(session.id)
        if result.task_id != delegation.task_id:
            raise RepositoryConflictError("Subagent Result belongs to another delegation task")
        parent = await self._require_session(session.parent_session_id)
        result_payload = result.model_dump(mode="json")
        loaded_child = await self._sessions.load_session(session.id)
        existing_result = next(
            (
                item
                for item in loaded_child.transcript
                if item.message_type is MessageType.SUBAGENT_RESULT
                and item.structured_content is not None
            ),
            None,
        )
        if existing_result is None:
            if session.status in _CLOSED:
                raise RepositoryConflictError(
                    f"closed Subagent session {session.id!r} has no Result Packet"
                )
            await self._sessions.append_message(
                session.id,
                TranscriptMessageDraft(
                    agent_id=session.agent_type,
                    role=MessageRole.ASSISTANT,
                    message_type=MessageType.SUBAGENT_RESULT,
                    content=result.summary,
                    structured_content=result_payload,
                    visibility=MessageVisibility.SUBAGENT_PRIVATE,
                ),
            )
        elif existing_result.structured_content != result_payload:
            raise RepositoryConflictError(
                f"Subagent session {session.id!r} already has another Result Packet"
            )
        primary_packet = result.primary_packet()
        primary_payload = primary_packet.model_dump(mode="json")
        loaded_parent = await self._sessions.load_session(parent.id)
        existing_primary = next(
            (
                item
                for item in loaded_parent.transcript
                if item.message_type is MessageType.SUBAGENT_RESULT
                and item.structured_content is not None
                and item.structured_content.get("task_id") == result.task_id
            ),
            None,
        )
        if existing_primary is not None and existing_primary.structured_content != primary_payload:
            raise RepositoryConflictError(
                f"Primary session {parent.id!r} already received another Result Packet"
            )
        if existing_primary is None and self._result_merger is not None:
            await self._result_merger.merge(session.run_id, result)
        target_status = _session_status(result.status)
        closed = await self._sessions.close_session(session.id, status=target_status)
        if existing_primary is None:
            await self._sessions.append_message(
                parent.id,
                TranscriptMessageDraft(
                    agent_id=parent.agent_type,
                    role=MessageRole.ASSISTANT,
                    message_type=MessageType.SUBAGENT_RESULT,
                    content=primary_packet.summary,
                    structured_content=primary_payload,
                    visibility=MessageVisibility.AGENT_ONLY,
                ),
            )
        await self._append_event(
            session.run_id,
            "subagent.completed",
            {
                "task_id": result.task_id,
                "session_id": session.id,
                "parent_session_id": parent.id,
                "status": result.status.value,
            },
        )
        return closed

    async def _enforce_run_limits(self, run_id: str) -> None:
        sessions = list(await self._session_repository.list_by_run(run_id))
        subagents = [session for session in sessions if session.parent_session_id is not None]
        active = [session for session in subagents if session.status not in _CLOSED]
        if len(active) >= self._limits.max_parallel_per_run:
            raise SubagentLimitError(
                f"Run {run_id!r} already has {len(active)} active Subagents"
            )
        if len(subagents) >= self._limits.max_total_per_run:
            raise SubagentLimitError(
                f"Run {run_id!r} already created {len(subagents)} Subagents"
            )

    def _validate_tools(self, delegation: DelegationPacket) -> None:
        residents = set(RESIDENT_TOOL_IDS)
        for tool_id in delegation.available_tool_ids:
            if tool_id not in residents:
                self._tool_context.index.schema(tool_id, require_available=True)

    def _apply_tool_allowlist(
        self,
        session: AgentSession,
        delegation: DelegationPacket,
    ) -> None:
        residents = set(RESIDENT_TOOL_IDS)
        registered = [
            tool_id for tool_id in delegation.available_tool_ids if tool_id not in residents
        ]
        allowed = list(
            dict.fromkeys(
                [
                    *SUBAGENT_RESIDENT_TOOL_IDS,
                    *delegation.available_tool_ids,
                ]
            )
        )
        self._tool_context.restrict_tools(
            allowed,
            run_id=session.run_id,
            session_id=session.id,
            agent_id=session.agent_type,
        )
        for tool_id in registered:
            self._tool_context.load_tool(
                tool_id,
                run_id=session.run_id,
                session_id=session.id,
                agent_id=session.agent_type,
            )

    async def _load_delegation(self, session_id: str) -> DelegationPacket:
        loaded = await self._sessions.load_session(session_id)
        message = next(
            (
                item
                for item in loaded.transcript
                if item.message_type is MessageType.SUBAGENT_DELEGATION
                and item.structured_content is not None
            ),
            None,
        )
        if message is None:
            raise RepositoryConflictError(
                f"Subagent session {session_id!r} has no Delegation Packet"
            )
        return DelegationPacket.model_validate(message.structured_content)

    async def _require_session(self, session_id: str) -> AgentSession:
        session = await self._session_repository.get(session_id)
        if session is None:
            raise EntityNotFoundError("AgentSession", session_id)
        return session

    async def _append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        if self._events is not None:
            await self._events.append(run_id, event_type, payload)


def _session_status(status: SubagentStatus) -> SessionStatus:
    if status is SubagentStatus.FAILED:
        return SessionStatus.FAILED
    if status is SubagentStatus.CANCELLED:
        return SessionStatus.CANCELLED
    return SessionStatus.COMPLETED
