"""Domain-specific exceptions."""


class DomainError(ValueError):
    """Base class for rejected domain operations."""


class InvalidStateTransitionError(DomainError):
    """Raised when an entity cannot move between the requested states."""

    def __init__(self, entity: str, current: object, target: object) -> None:
        super().__init__(f"{entity} cannot transition from {current!s} to {target!s}")
        self.entity = entity
        self.current = current
        self.target = target
