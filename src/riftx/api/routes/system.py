"""Local operator system diagnostics."""

from fastapi import APIRouter

from riftx.diagnostics import SystemDiagnosticsSnapshot

from ..dependencies import SystemDiagnosticsServiceDependency

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/diagnostics", response_model=SystemDiagnosticsSnapshot)
async def get_system_diagnostics(
    service: SystemDiagnosticsServiceDependency,
) -> SystemDiagnosticsSnapshot:
    return await service.snapshot()
