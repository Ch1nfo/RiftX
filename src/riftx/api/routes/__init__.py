"""Control-plane route modules."""

from .approvals import router as approvals_router
from .artifacts import router as artifacts_router
from .events import router as events_router
from .findings import router as findings_router
from .reports import router as reports_router
from .runs import router as runs_router
from .terminals import router as terminals_router
from .tools import router as tools_router

__all__ = [
    "approvals_router",
    "artifacts_router",
    "events_router",
    "findings_router",
    "reports_router",
    "runs_router",
    "terminals_router",
    "tools_router",
]
