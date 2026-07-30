"""Control-plane route modules."""

from .approvals import router as approvals_router
from .artifacts import router as artifacts_router
from .browser import router as browser_router
from .connectors import router as connectors_router
from .context import router as context_router
from .events import router as events_router
from .executions import router as executions_router
from .findings import router as findings_router
from .memories import router as memories_router
from .nodes import router as nodes_router
from .reports import router as reports_router
from .runner_control import router as runner_control_router
from .runs import router as runs_router
from .terminals import router as terminals_router
from .tools import router as tools_router

__all__ = [
    "approvals_router",
    "artifacts_router",
    "browser_router",
    "connectors_router",
    "context_router",
    "events_router",
    "executions_router",
    "findings_router",
    "memories_router",
    "nodes_router",
    "reports_router",
    "runner_control_router",
    "runs_router",
    "terminals_router",
    "tools_router",
]
