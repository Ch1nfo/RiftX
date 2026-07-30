"""Independent-context Subagent contracts and orchestration."""

from .manager import SubagentHandle, SubagentLimitError, SubagentManager
from .merge import PrimaryMergeResult, PrimaryResultMerger
from .models import (
    DelegationPacket,
    FindingCandidate,
    PrimaryMergePacket,
    SubagentResult,
    SubagentStatus,
)

__all__ = [
    "DelegationPacket",
    "FindingCandidate",
    "PrimaryMergePacket",
    "PrimaryMergeResult",
    "PrimaryResultMerger",
    "SubagentResult",
    "SubagentStatus",
    "SubagentHandle",
    "SubagentLimitError",
    "SubagentManager",
]
