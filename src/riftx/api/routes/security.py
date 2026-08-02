"""Read-only deployment trust profile observation."""

from fastapi import APIRouter, Request

from ..dependencies import LocalPrincipalDependency
from ..schemas.security import SecurityProfileResponse

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/profile", response_model=SecurityProfileResponse)
async def get_security_profile(
    request: Request,
    principal: LocalPrincipalDependency,
) -> SecurityProfileResponse:
    security = request.app.state.local_operator_security
    return SecurityProfileResponse(
        profile=principal.profile,
        principal_id=principal.id,
        capabilities=sorted(principal.capabilities, key=str),
        features=dict(security.features),
    )
