"""Strict loader for RiftX-maintained Official Capability Pack bundles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from riftx.capabilities import (
    CAPABILITY_PACK_SCHEMA_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    CapabilityDependency,
    CapabilityDependencyKind,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPack,
    CapabilityPackManifest,
    CapabilityPackMember,
    CapabilityPermission,
    CapabilityProvenance,
    CapabilitySource,
    CapabilityTrustTier,
    CapabilityVersion,
    CapabilityVersionStatus,
    EvidenceContract,
    PackStatus,
    capability_manifest_digest,
    capability_pack_digest,
)
from riftx.skills import ProgressiveSkillRegistry, SkillDocument, SkillSummary

OFFICIAL_PACK_SOURCE_SCHEMA_VERSION = "riftx.official-pack-source/v1"
OFFICIAL_PACK_ROOT = Path(__file__).with_name("official")
_MAX_BUNDLE_FILE_BYTES = 256 * 1024
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_LogicalId = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"),
]


class _PackSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialCapabilitySource(_PackSourceModel):
    capability_id: _LogicalId
    kind: CapabilityKind
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    domains: tuple[str, ...] = Field(min_length=1)
    triggers: tuple[str, ...] = ()
    dependencies: tuple[CapabilityDependency, ...] = ()
    permission: CapabilityPermission
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_contract: EvidenceContract
    evaluation_case_ids: tuple[_LogicalId, ...] = ()


class OfficialPackSource(_PackSourceModel):
    schema_version: Literal["riftx.official-pack-source/v1"]
    pack_id: _LogicalId
    version: str = Field(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        )
    )
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    authored_by: str = Field(min_length=1)
    authored_at: AwareDatetime
    tool_requirements: tuple[str, ...] = Field(min_length=1)
    evidence_contract: EvidenceContract
    skill_ids: tuple[_LogicalId, ...] = Field(min_length=1)
    capabilities: tuple[OfficialCapabilitySource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source(self) -> OfficialPackSource:
        for label, values in (
            ("tool requirements", self.tool_requirements),
            ("skill IDs", self.skill_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Official Pack {label} must be unique")
        capability_ids = [item.capability_id for item in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Official Pack Capability IDs must be unique")
        skill_capability_ids = {
            item.capability_id
            for item in self.capabilities
            if item.kind is CapabilityKind.SKILL
        }
        if set(self.skill_ids) != skill_capability_ids:
            raise ValueError("Official Pack skill IDs must match Skill Capability members")
        selectable = {
            item.kind
            for item in self.capabilities
            if item.kind in {CapabilityKind.SKILL, CapabilityKind.TECHNIQUE}
        }
        if not selectable:
            raise ValueError("Official Pack requires a Skill or Technique member")
        return self


class OfficialNegativeCase(_PackSourceModel):
    case_id: _LogicalId
    description: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    forbidden_outcomes: tuple[str, ...] = Field(min_length=1)


class OfficialEvaluationCase(_PackSourceModel):
    case_id: _LogicalId
    capability_ids: tuple[_LogicalId, ...] = Field(min_length=1)
    description: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    assertions: tuple[str, ...] = Field(min_length=1)
    negative_case_ids: tuple[_LogicalId, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class OfficialPackBundle:
    root: Path
    source: OfficialPackSource
    pack: CapabilityPack
    capability_versions: tuple[CapabilityVersion, ...]
    skill_documents: tuple[SkillDocument, ...]
    negative_cases: tuple[OfficialNegativeCase, ...]
    evaluation_cases: tuple[OfficialEvaluationCase, ...]
    changelog: str
    bundle_digest: str


class OfficialPackCatalog:
    """Load bundled Official Packs without granting them runtime authority."""

    def __init__(self, root: Path = OFFICIAL_PACK_ROOT) -> None:
        self.root = root

    def load(self) -> tuple[OfficialPackBundle, ...]:
        if not self.root.is_dir():
            raise ValueError(f"Official Pack root is unavailable: {self.root}")
        bundles = tuple(
            _load_bundle(directory)
            for directory in sorted(self.root.iterdir(), key=lambda item: item.name)
            if directory.is_dir() and (directory / "pack.yaml").is_file()
        )
        pack_ids = [bundle.source.pack_id for bundle in bundles]
        if len(pack_ids) != len(set(pack_ids)):
            raise ValueError("Official Pack IDs must be unique")
        return bundles

    def get(self, pack_id: str) -> OfficialPackBundle:
        matches = [bundle for bundle in self.load() if bundle.source.pack_id == pack_id]
        if len(matches) != 1:
            raise KeyError(pack_id)
        return matches[0]

    def skill_roots(self) -> tuple[Path, ...]:
        return tuple(bundle.root / "skills" for bundle in self.load())


def _load_bundle(root: Path) -> OfficialPackBundle:
    source = OfficialPackSource.model_validate(_read_yaml(root / "pack.yaml"))
    negative_cases = tuple(
        TypeAdapter(list[OfficialNegativeCase]).validate_python(
            _read_yaml(root / "negative_cases.yaml")
        )
    )
    evaluation_cases = tuple(
        TypeAdapter(list[OfficialEvaluationCase]).validate_python(
            _read_yaml(root / "eval_cases.yaml")
        )
    )
    changelog = _read_text(root / "CHANGELOG.md")
    bundle_digest = _bundle_digest(root)
    skill_registry = ProgressiveSkillRegistry(root / "skills")
    skill_documents = tuple(skill_registry.validate())
    skill_summaries = {item.id: item for item in skill_registry.list_summaries()}
    _validate_bundle_source(
        source,
        skill_summaries=skill_summaries,
        negative_cases=negative_cases,
        evaluation_cases=evaluation_cases,
        changelog=changelog,
    )
    versions = tuple(
        _capability_version(
            source,
            capability,
            source_digest=(
                skill_summaries[capability.capability_id].digest
                if capability.kind is CapabilityKind.SKILL
                else bundle_digest
            ),
        )
        for capability in source.capabilities
    )
    pack_manifest = CapabilityPackManifest(
        schema_version=CAPABILITY_PACK_SCHEMA_VERSION,
        pack_id=source.pack_id,
        version=source.version,
        title=source.title,
        description=source.description,
        source=CapabilitySource.OFFICIAL,
        publisher="riftx",
        members=tuple(
            CapabilityPackMember(
                capability_id=version.manifest.capability_id,
                version=version.manifest.version,
                version_digest=version.manifest_digest,
            )
            for version in versions
        ),
        provenance=_provenance(
            source,
            source_reference=f"builtin://packs/{source.pack_id}/{source.version}",
            source_digest=bundle_digest,
        ),
    )
    pack = CapabilityPack(
        pack_version_id=_stable_id("pack-version", source.pack_id, source.version),
        manifest=pack_manifest,
        manifest_digest=capability_pack_digest(pack_manifest),
        status=PackStatus.ACTIVE,
        created_at=source.authored_at,
    )
    return OfficialPackBundle(
        root=root,
        source=source,
        pack=pack,
        capability_versions=versions,
        skill_documents=skill_documents,
        negative_cases=negative_cases,
        evaluation_cases=evaluation_cases,
        changelog=changelog,
        bundle_digest=bundle_digest,
    )


def _capability_version(
    source: OfficialPackSource,
    capability: OfficialCapabilitySource,
    *,
    source_digest: str,
) -> CapabilityVersion:
    manifest = CapabilityManifest(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        capability_id=capability.capability_id,
        version=source.version,
        kind=capability.kind,
        title=capability.title,
        description=capability.description,
        domains=capability.domains,
        triggers=capability.triggers,
        dependencies=capability.dependencies,
        permission=capability.permission,
        input_schema=capability.input_schema,
        output_schema=capability.output_schema,
        evidence_contract=capability.evidence_contract,
        provenance=_provenance(
            source,
            source_reference=(
                f"builtin://packs/{source.pack_id}/{source.version}/skills/"
                f"{capability.capability_id}"
                if capability.kind is CapabilityKind.SKILL
                else (
                    f"builtin://packs/{source.pack_id}/{source.version}/capabilities/"
                    f"{capability.capability_id}"
                )
            ),
            source_digest=source_digest,
        ),
        evaluation_case_ids=capability.evaluation_case_ids,
        trust_tier=CapabilityTrustTier.OFFICIAL,
    )
    return CapabilityVersion(
        version_id=_stable_id(
            "capability-version",
            capability.capability_id,
            source.version,
        ),
        manifest=manifest,
        manifest_digest=capability_manifest_digest(manifest),
        status=CapabilityVersionStatus.ACTIVE,
        created_at=source.authored_at,
        activated_at=source.authored_at,
    )


def _validate_bundle_source(
    source: OfficialPackSource,
    *,
    skill_summaries: dict[str, SkillSummary],
    negative_cases: tuple[OfficialNegativeCase, ...],
    evaluation_cases: tuple[OfficialEvaluationCase, ...],
    changelog: str,
) -> None:
    if set(source.skill_ids) != set(skill_summaries):
        raise ValueError("Official Pack Skill packages do not match declared skill IDs")
    if any(summary.source is not CapabilitySource.OFFICIAL for summary in skill_summaries.values()):
        raise ValueError("Official Pack Skill packages must declare source=official")
    if not negative_cases:
        raise ValueError("Official Pack requires negative cases")
    if not evaluation_cases:
        raise ValueError("Official Pack requires evaluation cases")
    negative_case_ids = [item.case_id for item in negative_cases]
    evaluation_case_ids = [item.case_id for item in evaluation_cases]
    for label, values in (
        ("negative case IDs", negative_case_ids),
        ("evaluation case IDs", evaluation_case_ids),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Official Pack {label} must be unique")
    capability_by_id = {item.capability_id: item for item in source.capabilities}
    declared_evaluation_ids = {
        item.capability_id
        for item in source.capabilities
        if item.kind is CapabilityKind.EVAL_CASE
    }
    if declared_evaluation_ids != set(evaluation_case_ids):
        raise ValueError("Official Pack Eval Case assets must match Eval Case members")
    non_eval_ids = {
        item.capability_id
        for item in source.capabilities
        if item.kind is not CapabilityKind.EVAL_CASE
    }
    for evaluation in evaluation_cases:
        if not set(evaluation.capability_ids) <= non_eval_ids:
            raise ValueError("Official Pack Eval Case targets an unknown Capability")
        if not set(evaluation.negative_case_ids) <= set(negative_case_ids):
            raise ValueError("Official Pack Eval Case references an unknown negative case")
    referenced_eval_ids = {
        case_id
        for capability in source.capabilities
        if capability.kind is not CapabilityKind.EVAL_CASE
        for case_id in capability.evaluation_case_ids
    }
    if referenced_eval_ids != declared_evaluation_ids:
        raise ValueError("Official Pack Capabilities must reference every Eval Case member")
    tool_dependencies = {
        dependency.reference
        for capability in source.capabilities
        if capability.kind is not CapabilityKind.EVAL_CASE
        for dependency in capability.dependencies
        if dependency.kind is CapabilityDependencyKind.TOOL and not dependency.optional
    }
    if tool_dependencies != set(source.tool_requirements):
        raise ValueError("Official Pack tool requirements must match required Tool dependencies")
    evidence_refs = set(source.evidence_contract.required_refs)
    if not any(
        evidence_refs <= set(capability.evidence_contract.required_refs)
        for capability in source.capabilities
        if capability.kind is not CapabilityKind.EVAL_CASE
    ):
        raise ValueError("Official Pack Evidence contract is not implemented by a member")
    if f"## {source.version}" not in changelog:
        raise ValueError("Official Pack CHANGELOG must describe the current version")
    if any(case_id not in capability_by_id for case_id in declared_evaluation_ids):
        raise ValueError("Official Pack Eval Case member is missing")


def _provenance(
    source: OfficialPackSource,
    *,
    source_reference: str,
    source_digest: str,
) -> CapabilityProvenance:
    return CapabilityProvenance(
        publisher="riftx",
        source=CapabilitySource.OFFICIAL,
        source_reference=source_reference,
        authored_by=source.authored_by,
        authored_at=source.authored_at,
        source_digest=source_digest,
    )


def _stable_id(kind: str, logical_id: str, version: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"riftx:{kind}:{logical_id}:{version}"))


def _read_yaml(path: Path) -> object:
    return yaml.safe_load(_read_text(path))


def _read_text(path: Path) -> str:
    _require_regular_file(path)
    if path.stat().st_size > _MAX_BUNDLE_FILE_BYTES:
        raise ValueError(f"Official Pack file exceeds size limit: {path}")
    return path.read_text(encoding="utf-8")


def _bundle_digest(root: Path) -> str:
    digest = hashlib.sha256(b"riftx.official-pack-bundle/v1\0")
    total = 0
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        _require_regular_file(path)
        content = path.read_bytes()
        total += len(content)
        if len(content) > _MAX_BUNDLE_FILE_BYTES or total > _MAX_BUNDLE_BYTES:
            raise ValueError(f"Official Pack bundle exceeds size limit: {root}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Official Pack asset must be a regular file: {path}")
