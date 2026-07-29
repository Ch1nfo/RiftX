"""Errors exposed by application ports and services."""

from __future__ import annotations


class RepositoryError(RuntimeError):
    """Base class for persistence boundary failures."""


class EntityNotFoundError(RepositoryError):
    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id!r} was not found")
        self.entity = entity
        self.entity_id = entity_id


class RepositoryConflictError(RepositoryError):
    """Raised when a database constraint rejects an operation."""


class ApplicationServiceError(RuntimeError):
    """Base error carrying a stable machine-readable control-plane code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ApplicationConflictError(ApplicationServiceError):
    """The requested action conflicts with current durable state."""


class ServiceUnavailableError(ApplicationServiceError):
    """A required infrastructure service is currently unavailable."""


class AuthenticationError(ApplicationServiceError):
    """Runner or registration credentials were missing or invalid."""
