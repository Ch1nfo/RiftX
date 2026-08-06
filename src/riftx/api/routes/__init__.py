"""Control-plane route modules."""

from .actions import router as actions_router
from .approvals import router as approvals_router
from .artifacts import router as artifacts_router
from .audit_preflight import router as audit_preflight_router
from .audit_preflight_runner import router as audit_preflight_runner_router
from .audits import router as audits_router
from .browser import router as browser_router
from .connectors import router as connectors_router
from .context import router as context_router
from .events import router as events_router
from .executions import router as executions_router
from .findings import router as findings_router
from .graphs import router as graphs_router
from .memories import router as memories_router
from .models import router as models_router
from .nodes import router as nodes_router
from .observability import router as observability_router
from .observer import router as observer_router
from .reports import router as reports_router
from .runner_control import router as runner_control_router
from .runs import router as runs_router
from .security import router as security_router
from .system import router as system_router
from .terminals import router as terminals_router
from .tools import router as tools_router
from .traffic import router as traffic_router

__all__ = [
    "actions_router",
    "approvals_router",
    "artifacts_router",
    "audit_preflight_router",
    "audit_preflight_runner_router",
    "audits_router",
    "browser_router",
    "connectors_router",
    "context_router",
    "events_router",
    "executions_router",
    "findings_router",
    "graphs_router",
    "memories_router",
    "models_router",
    "nodes_router",
    "observability_router",
    "observer_router",
    "reports_router",
    "runner_control_router",
    "runs_router",
    "security_router",
    "system_router",
    "terminals_router",
    "tools_router",
    "traffic_router",
]
