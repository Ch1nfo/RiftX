"""Code-finding state primitives whose wire values are frozen for RiftX 3.0."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .errors import InvalidStateTransitionError


class CandidateStatus(StrEnum):
    NEW = "new"
    NORMALIZED = "normalized"
    VALIDATING = "validating"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    MERGED = "merged"


_CANDIDATE_TRANSITIONS: Mapping[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.NEW: frozenset({CandidateStatus.NORMALIZED}),
    CandidateStatus.NORMALIZED: frozenset({CandidateStatus.VALIDATING}),
    CandidateStatus.VALIDATING: frozenset(
        {
            CandidateStatus.CONFIRMED,
            CandidateStatus.REJECTED,
            CandidateStatus.DEFERRED,
            CandidateStatus.MERGED,
        }
    ),
    CandidateStatus.CONFIRMED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
    CandidateStatus.DEFERRED: frozenset(),
    CandidateStatus.MERGED: frozenset(),
}


def candidate_can_transition_to(current: CandidateStatus, target: CandidateStatus) -> bool:
    """Return whether the versioned Candidate state machine allows this edge."""

    return (
        isinstance(current, CandidateStatus)
        and isinstance(target, CandidateStatus)
        and target in _CANDIDATE_TRANSITIONS[current]
    )


def validate_candidate_transition(
    current: CandidateStatus,
    target: CandidateStatus,
) -> CandidateStatus:
    """Return the accepted target or fail without mutating any Candidate fact."""

    if not candidate_can_transition_to(current, target):
        raise InvalidStateTransitionError("CodeFindingCandidate", current, target)
    return target
