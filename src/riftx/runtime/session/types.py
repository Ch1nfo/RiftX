"""Session lifecycle request results."""

from pydantic import Field

from riftx.domain import AgentMessage
from riftx.domain.base import DomainModel
from riftx.runtime.types import AgentSession, ProviderState


class LoadedSession(DomainModel):
    """Durable session state plus its complete transcript and optional provider state."""

    session: AgentSession
    transcript: list[AgentMessage] = Field(default_factory=list)
    provider_state: ProviderState | None = None
