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
from .orchestrator import SubagentOrchestrator, SubagentTaskRunner
from .runtime import DurableSubagentTaskRunner, ModelDelegationExecutor

__all__ = [
    "DelegationPacket",
    "DurableSubagentTaskRunner",
    "FindingCandidate",
    "ModelDelegationExecutor",
    "PrimaryMergePacket",
    "PrimaryMergeResult",
    "PrimaryResultMerger",
    "SubagentResult",
    "SubagentStatus",
    "SubagentOrchestrator",
    "SubagentTaskRunner",
    "SubagentHandle",
    "SubagentLimitError",
    "SubagentManager",
]
