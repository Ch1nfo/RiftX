"""Structured target scope enforcement."""

from .guard import (
    ScopeDecision,
    ScopeGuard,
    ScopeTargetKind,
    ScopeViolationError,
    infer_scope_target_kind,
)

__all__ = [
    "ScopeDecision",
    "ScopeGuard",
    "ScopeTargetKind",
    "ScopeViolationError",
    "infer_scope_target_kind",
]
