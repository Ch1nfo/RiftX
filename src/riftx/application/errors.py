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


class PentestBudgetExceededError(RepositoryConflictError):
    """A serialized Pentest effect claim would exceed its durable budget."""

    def __init__(
        self,
        budget_name: str,
        *,
        limit: int,
        used: int,
        reason: str = "exhausted",
    ) -> None:
        super().__init__(f"Pentest budget {budget_name!r} is exhausted")
        self.budget_name = budget_name
        self.limit = limit
        self.used = used
        self.reason = reason


class RepositoryUnavailableError(RepositoryError):
    """A persistence operation failed without exposing driver diagnostics."""


class AuditIdempotencyConflictError(RepositoryConflictError):
    """A Code Audit client request key is bound to a different payload."""


class RepositoryIntegrityError(RepositoryError):
    """A persisted row cannot be reconstructed into its authoritative domain fact.

    The message intentionally contains only an entity kind, opaque identifier,
    and stable reason code. Persisted Code Audit contracts can contain sensitive
    source paths, so mapper exceptions and raw row values must never be reflected
    across this boundary.
    """

    def __init__(
        self,
        entity: str,
        entity_id: str,
        *,
        reason_code: str = "invalid_persisted_state",
    ) -> None:
        allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-")
        opaque_id = (
            entity_id
            if isinstance(entity_id, str)
            and 0 < len(entity_id) <= 128
            and all(character in allowed for character in entity_id)
            else "invalid-id"
        )
        super().__init__(f"{entity} {opaque_id!r} failed integrity validation ({reason_code})")
        self.entity = entity
        self.entity_id = opaque_id
        self.reason_code = reason_code


class RepositoryDecisionConflictError(RepositoryConflictError):
    """Raised when a durable approval tuple differs from the requested tuple."""

    def __init__(self, message: str, *, details: dict[str, object]) -> None:
        super().__init__(message)
        self.details = details


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


class AuthorizationError(ApplicationServiceError):
    """An authenticated principal lacks a required server-owned capability."""


class ResourceNotAccessibleError(ApplicationServiceError):
    """A resource is absent or not accessible without revealing which case applies."""


def resource_not_accessible() -> ResourceNotAccessibleError:
    """Return the uniform absent-or-denied error used at object read boundaries."""

    return ResourceNotAccessibleError(
        "resource_not_accessible",
        "The requested resource was not found",
    )
