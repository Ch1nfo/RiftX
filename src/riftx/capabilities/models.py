"""Authoritative domain contracts for versioned RiftX security capabilities."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from riftx.domain.enums import ApprovalLevel

CAPABILITY_SCHEMA_VERSION = "riftx.capability/v1"
CAPABILITY_PACK_SCHEMA_VERSION = "riftx.capability-pack/v1"
CAPABILITY_DIGEST_DOMAIN = b"riftx.capability-manifest/v1\0"
CAPABILITY_PACK_DIGEST_DOMAIN = b"riftx.capability-pack/v1\0"

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LogicalId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
OpaqueId = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,127}$"),
]
SemanticVersion = Annotated[
    str,
    Field(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        )
    ),
]
NonEmpty = Annotated[str, Field(min_length=1)]


class CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityKind(StrEnum):
    TOOL = "tool"
    TECHNIQUE = "technique"
    SKILL = "skill"
    PLAYBOOK = "playbook"
    KNOWLEDGE = "knowledge"
    EVAL_CASE = "eval_case"


class CapabilitySource(StrEnum):
    OFFICIAL = "official"
    OPERATOR = "operator"
    ORGANIZATION = "organization"
    ENGAGEMENT = "engagement"


class CapabilityTrustTier(StrEnum):
    OFFICIAL = "official"
    VERIFIED = "verified"
    LOCAL = "local"
    UNTRUSTED = "untrusted"


class CapabilityVersionStatus(StrEnum):
    APPROVED = "approved"
    ACTIVE = "active"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class CapabilityDependencyKind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    CAPABILITY = "capability"
    PLATFORM = "platform"


class CapabilityEffectClass(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_MUTATION = "local_mutation"
    TARGET_INTERACTION = "target_interaction"
    CODE_EXECUTION = "code_execution"
    CREDENTIAL_ACCESS = "credential_access"
    EXTERNAL_SERVICE = "external_service"


class ConfirmationPolicy(StrEnum):
    EXPLICIT_VERIFICATION = "explicit_verification"
    INDEPENDENT_SOURCES = "independent_sources"
    MANUAL_REVIEW = "manual_review"


class PackStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class PackInstallStatus(StrEnum):
    INSTALLED = "installed"
    DISABLED = "disabled"


class PackLockOwnerKind(StrEnum):
    PACK_INSTALL = "pack_install"
    RUN_SESSION = "run_session"


class CapabilityProvenance(CapabilityModel):
    publisher: NonEmpty
    source: CapabilitySource
    source_reference: NonEmpty
    authored_by: NonEmpty
    authored_at: AwareDatetime
    source_digest: Digest | None = None
    signature: str | None = Field(default=None, min_length=1)


class CapabilityDependency(CapabilityModel):
    kind: CapabilityDependencyKind
    reference: NonEmpty
    version_constraint: str | None = Field(default=None, min_length=1)
    optional: bool = False


class CapabilityPermission(CapabilityModel):
    effect_class: CapabilityEffectClass
    approval_level: ApprovalLevel
    requires_scope: bool
    credential_references: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def unique_credential_references(self) -> CapabilityPermission:
        if len(self.credential_references) != len(set(self.credential_references)):
            raise ValueError("credential references must be unique")
        if (
            self.effect_class is CapabilityEffectClass.TARGET_INTERACTION
            and not self.requires_scope
        ):
            raise ValueError("target interaction capabilities must require scope")
        return self


class EvidenceContract(CapabilityModel):
    required_refs: tuple[NonEmpty, ...]
    minimum_independent_sources: int = Field(default=1, ge=0)
    confirmation_policy: ConfirmationPolicy

    @model_validator(mode="after")
    def unique_required_refs(self) -> EvidenceContract:
        if len(self.required_refs) != len(set(self.required_refs)):
            raise ValueError("evidence contract references must be unique")
        if (
            self.confirmation_policy is ConfirmationPolicy.INDEPENDENT_SOURCES
            and self.minimum_independent_sources < 2
        ):
            raise ValueError("independent source confirmation requires at least two sources")
        return self


class CapabilityManifest(CapabilityModel):
    schema_version: str = Field(pattern=r"^riftx\.capability/v1$")
    capability_id: LogicalId
    version: SemanticVersion
    kind: CapabilityKind
    title: NonEmpty
    description: NonEmpty
    domains: tuple[NonEmpty, ...]
    triggers: tuple[NonEmpty, ...] = ()
    dependencies: tuple[CapabilityDependency, ...] = ()
    permission: CapabilityPermission
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_contract: EvidenceContract
    provenance: CapabilityProvenance
    evaluation_case_ids: tuple[LogicalId, ...] = ()
    trust_tier: CapabilityTrustTier

    @model_validator(mode="after")
    def validate_manifest_collections(self) -> CapabilityManifest:
        for name, values in (
            ("domains", self.domains),
            ("triggers", self.triggers),
            ("evaluation_case_ids", self.evaluation_case_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique values")
        if not self.domains:
            raise ValueError("capability manifest requires at least one domain")
        dependency_keys = [
            (dependency.kind, dependency.reference) for dependency in self.dependencies
        ]
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("capability dependency identities must be unique")
        return self


class Capability(CapabilityModel):
    capability_id: LogicalId
    kind: CapabilityKind
    created_at: AwareDatetime


class CapabilityVersion(CapabilityModel):
    version_id: OpaqueId
    manifest: CapabilityManifest
    manifest_digest: Digest
    status: CapabilityVersionStatus
    created_at: AwareDatetime
    activated_at: AwareDatetime | None = None
    retired_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_version(self) -> CapabilityVersion:
        expected_digest = capability_manifest_digest(self.manifest)
        if self.manifest_digest != expected_digest:
            raise ValueError("capability manifest digest does not match canonical content")
        if self.status is CapabilityVersionStatus.ACTIVE and self.activated_at is None:
            raise ValueError("active capability versions require activated_at")
        if self.retired_at is not None and self.activated_at is None:
            raise ValueError("retired capability versions require an activation time")
        if self.activated_at is not None and self.activated_at < self.created_at:
            raise ValueError("capability activation cannot predate creation")
        if self.retired_at is not None and self.retired_at < self.activated_at:
            raise ValueError("capability retirement cannot predate activation")
        return self


class CapabilityPackMember(CapabilityModel):
    capability_id: LogicalId
    version: SemanticVersion
    version_digest: Digest


class CapabilityPackManifest(CapabilityModel):
    schema_version: str = Field(pattern=r"^riftx\.capability-pack/v1$")
    pack_id: LogicalId
    version: SemanticVersion
    title: NonEmpty
    description: NonEmpty
    source: CapabilitySource
    publisher: NonEmpty
    members: tuple[CapabilityPackMember, ...]
    provenance: CapabilityProvenance

    @model_validator(mode="after")
    def validate_pack_members(self) -> CapabilityPackManifest:
        if not self.members:
            raise ValueError("capability packs require at least one member")
        member_ids = [member.capability_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("capability pack member IDs must be unique")
        return self


class CapabilityPack(CapabilityModel):
    pack_version_id: OpaqueId
    manifest: CapabilityPackManifest
    manifest_digest: Digest
    status: PackStatus
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_pack_digest(self) -> CapabilityPack:
        if self.manifest_digest != capability_pack_digest(self.manifest):
            raise ValueError("capability pack digest does not match canonical content")
        return self


class PackInstall(CapabilityModel):
    install_id: OpaqueId
    scope_type: CapabilitySource
    scope_id: NonEmpty
    pack_id: LogicalId
    pack_version_id: OpaqueId
    pack_version: SemanticVersion
    pack_digest: Digest
    status: PackInstallStatus
    state_version: int = Field(ge=1)
    previous_pack_version_id: str | None = Field(default=None, min_length=1)
    installed_at: AwareDatetime
    updated_at: AwareDatetime
    disabled_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_install(self) -> PackInstall:
        if self.updated_at < self.installed_at:
            raise ValueError("pack install update cannot predate installation")
        if self.status is PackInstallStatus.DISABLED:
            if self.disabled_at is None:
                raise ValueError("disabled pack installs require disabled_at")
        elif self.disabled_at is not None:
            raise ValueError("installed packs cannot carry disabled_at")
        return self


class PackLock(CapabilityModel):
    lock_id: OpaqueId
    owner_kind: PackLockOwnerKind
    owner_id: OpaqueId
    capability_id: LogicalId
    capability_version_id: OpaqueId
    capability_version: SemanticVersion
    capability_digest: Digest
    acquired_at: AwareDatetime
    released_at: AwareDatetime | None = None

    @property
    def active(self) -> bool:
        return self.released_at is None


def canonical_payload_json(payload: object) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_payload_digest(payload: object, *, domain: bytes) -> str:
    return hashlib.sha256(domain + canonical_payload_json(payload).encode("utf-8")).hexdigest()


def capability_manifest_digest(manifest: CapabilityManifest) -> str:
    return canonical_payload_digest(manifest, domain=CAPABILITY_DIGEST_DOMAIN)


def capability_pack_digest(manifest: CapabilityPackManifest) -> str:
    return canonical_payload_digest(manifest, domain=CAPABILITY_PACK_DIGEST_DOMAIN)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capability timestamps must be timezone-aware")
    return value
