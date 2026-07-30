"""Independent-context Subagent contracts and orchestration."""

from .manager import SubagentHandle, SubagentLimitError, SubagentManager
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
    "SubagentResult",
    "SubagentStatus",
    "SubagentHandle",
    "SubagentLimitError",
    "SubagentManager",
]
