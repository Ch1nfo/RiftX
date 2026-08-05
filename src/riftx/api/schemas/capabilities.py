"""Control-plane schemas for the versioned Capability data plane."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from riftx.capabilities import (
    CAPABILITY_PACK_SCHEMA_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilityCandidate,
    CapabilityCandidateStatus,
    CapabilityEvaluationResult,
    CapabilityManifest,
    CapabilityPack,
    CapabilityPackManifest,
    CapabilitySource,
    CapabilityVersion,
    CapabilityVersionStatus,
    EvaluationResultStatus,
    PackInstall,
    PackInstallStatus,
    PackLock,
    PackLockOwnerKind,
    PackStatus,
    PromotionRun,
    PromotionStatus,
    capability_manifest_digest,
    capability_pack_digest,
    evaluation_report_digest,
)
from riftx.domain.base import new_id, utc_now


class CapabilityAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CreateCapabilityVersionRequest(CapabilityAPIModel):
    manifest: CapabilityManifest

    def to_domain(self) -> tuple[Capability, CapabilityVersion]:
        now = utc_now()
        capability = Capability(
            capability_id=self.manifest.capability_id,
            kind=self.manifest.kind,
            created_at=now,
        )
        version = CapabilityVersion(
            version_id=new_id(),
            manifest=self.manifest,
            manifest_digest=capability_manifest_digest(self.manifest),
            status=CapabilityVersionStatus.APPROVED,
            created_at=now,
        )
        return capability, version


class CreateCapabilityCandidateRequest(CapabilityAPIModel):
    proposed_manifest: CapabilityManifest
    proposed_by: str = Field(min_length=1)
    source_run_id: str | None = Field(default=None, min_length=1)

    def to_domain(self) -> CapabilityCandidate:
        now = utc_now()
        return CapabilityCandidate(
            candidate_id=new_id(),
            proposed_manifest=self.proposed_manifest,
            candidate_digest=capability_manifest_digest(self.proposed_manifest),
            status=CapabilityCandidateStatus.DRAFT,
            proposed_by=self.proposed_by,
            source_run_id=self.source_run_id,
            created_at=now,
            updated_at=now,
        )


class CreatePromotionRunRequest(CapabilityAPIModel):
    candidate_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)

    def to_domain(self) -> PromotionRun:
        now = utc_now()
        return PromotionRun(
            promotion_id=new_id(),
            candidate_id=self.candidate_id,
            status=PromotionStatus.PENDING,
            requested_by=self.requested_by,
            created_at=now,
            updated_at=now,
        )


class CreateCapabilityEvaluationResultRequest(CapabilityAPIModel):
    promotion_id: str = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    status: EvaluationResultStatus
    scenario_ids: tuple[str, ...] = Field(min_length=1)
    report: dict[str, JsonValue]

    def to_domain(self) -> CapabilityEvaluationResult:
        return CapabilityEvaluationResult(
            result_id=new_id(),
            promotion_id=self.promotion_id,
            evaluator=self.evaluator,
            status=self.status,
            scenario_ids=self.scenario_ids,
            report=self.report,
            report_digest=evaluation_report_digest(self.report),
            created_at=utc_now(),
        )


class RegisterCapabilityPackRequest(CapabilityAPIModel):
    manifest: CapabilityPackManifest

    def to_domain(self) -> CapabilityPack:
        return CapabilityPack(
            pack_version_id=new_id(),
            manifest=self.manifest,
            manifest_digest=capability_pack_digest(self.manifest),
            status=PackStatus.ACTIVE,
            created_at=utc_now(),
        )


class InstallCapabilityPackRequest(CapabilityAPIModel):
    install_id: str | None = Field(default=None, min_length=1)
    scope_type: CapabilitySource
    scope_id: str = Field(min_length=1)
    pack_version_id: str = Field(min_length=1)


class CapabilityVersionResponse(CapabilityAPIModel):
    version_id: str
    manifest: CapabilityManifest
    manifest_digest: str
    status: CapabilityVersionStatus
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None

    @classmethod
    def from_domain(cls, value: CapabilityVersion) -> CapabilityVersionResponse:
        return cls.model_validate(value)


class CapabilityCandidateResponse(CapabilityAPIModel):
    candidate_id: str
    proposed_manifest: CapabilityManifest
    candidate_digest: str
    status: CapabilityCandidateStatus
    proposed_by: str
    source_run_id: str | None
    created_at: datetime
    updated_at: datetime
    promoted_version_id: str | None

    @classmethod
    def from_domain(cls, value: CapabilityCandidate) -> CapabilityCandidateResponse:
        return cls.model_validate(value)


class PromotionRunResponse(CapabilityAPIModel):
    promotion_id: str
    candidate_id: str
    status: PromotionStatus
    requested_by: str
    approval_reference: str | None
    promoted_version_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: PromotionRun) -> PromotionRunResponse:
        return cls.model_validate(value)


class CapabilityEvaluationResultResponse(CapabilityAPIModel):
    result_id: str
    promotion_id: str
    evaluator: str
    status: EvaluationResultStatus
    scenario_ids: tuple[str, ...]
    report: dict[str, JsonValue]
    report_digest: str
    created_at: datetime

    @classmethod
    def from_domain(
        cls,
        value: CapabilityEvaluationResult,
    ) -> CapabilityEvaluationResultResponse:
        return cls.model_validate(value)


class CapabilityPackResponse(CapabilityAPIModel):
    pack_version_id: str
    manifest: CapabilityPackManifest
    manifest_digest: str
    status: PackStatus
    created_at: datetime

    @classmethod
    def from_domain(cls, value: CapabilityPack) -> CapabilityPackResponse:
        return cls.model_validate(value)


class PackInstallResponse(CapabilityAPIModel):
    install_id: str
    scope_type: CapabilitySource
    scope_id: str
    pack_id: str
    pack_version_id: str
    pack_version: str
    pack_digest: str
    status: PackInstallStatus
    state_version: int
    previous_pack_version_id: str | None
    installed_at: datetime
    updated_at: datetime
    disabled_at: datetime | None

    @classmethod
    def from_domain(cls, value: PackInstall) -> PackInstallResponse:
        return cls.model_validate(value)


class PackLockResponse(CapabilityAPIModel):
    lock_id: str
    owner_kind: PackLockOwnerKind
    owner_id: str
    capability_id: str
    capability_version_id: str
    capability_version: str
    capability_digest: str
    acquired_at: datetime
    released_at: datetime | None

    @classmethod
    def from_domain(cls, value: PackLock) -> PackLockResponse:
        return cls.model_validate(value)


assert CAPABILITY_SCHEMA_VERSION == "riftx.capability/v1"
assert CAPABILITY_PACK_SCHEMA_VERSION == "riftx.capability-pack/v1"
