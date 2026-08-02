"""Observable local deployment profile and capability schemas."""

from typing import Literal

from pydantic import BaseModel

from riftx.domain import OperatorCapability, TrustProfile


class SecurityProfileResponse(BaseModel):
    profile: TrustProfile
    principal_id: str
    capabilities: list[OperatorCapability]
    features: dict[str, bool]
    tenant_safe: Literal[False] = False
