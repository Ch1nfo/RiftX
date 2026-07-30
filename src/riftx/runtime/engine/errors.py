"""Stable Agent Engine boundary errors."""


class AgentEngineError(RuntimeError):
    """Base class for provider adapter failures."""


class InvalidProviderStateError(AgentEngineError):
    """A persisted provider state cannot be deserialized safely."""


class ProviderStateSerializationError(AgentEngineError):
    """The active provider state cannot be serialized for suspension."""
