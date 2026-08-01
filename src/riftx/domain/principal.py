"""Server-owned Control Plane operator identities."""

from pydantic import ConfigDict, Field

from .base import DomainModel
from .enums import OperatorCapability, TrustProfile


class LocalPrincipal(DomainModel):
    """Stable identity for one local, single-operator RiftX installation."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=128)
    profile: TrustProfile = TrustProfile.LOCAL_SINGLE_OPERATOR
    namespace_id: str = Field(default="local", min_length=1, max_length=64)
    capabilities: frozenset[OperatorCapability]
