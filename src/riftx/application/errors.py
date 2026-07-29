"""Errors exposed by application ports."""


class RepositoryError(RuntimeError):
    """Base class for persistence boundary failures."""


class EntityNotFoundError(RepositoryError):
    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id!r} was not found")
        self.entity = entity
        self.entity_id = entity_id


class RepositoryConflictError(RepositoryError):
    """Raised when a database constraint rejects an operation."""
