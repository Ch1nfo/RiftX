"""Strict contracts for repeatable security capability evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class EvaluationModel(BaseModel):
    """Frozen base model used by evaluation manifests and results."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioKind(StrEnum):
    CODE_AUDIT = "code_audit"
    PENETRATION_TEST = "penetration_test"


class ScenarioVisibility(StrEnum):
    PUBLIC_DEVELOPMENT = "public_development"
    SEALED_REGRESSION = "sealed_regression"


class TargetKind(StrEnum):
    REPOSITORY_FIXTURE = "repository_fixture"
    WEB_FIXTURE = "web_fixture"


class ResetStrategy(StrEnum):
    IMMUTABLE_FIXTURE = "immutable_fixture"


class MemoryShareMode(StrEnum):
    ISOLATED = "isolated"
    DECLARED = "declared"


class FindingDisposition(StrEnum):
    SUSPECTED = "suspected"
    VERIFIED = "verified"
    FALSE_POSITIVE = "false_positive"
    NOT_FOUND = "not_found"


class EvidenceKind(StrEnum):
    SOURCE_LOCATION = "source_location"
    TOOL_OUTPUT = "tool_output"
    HTTP_REQUEST = "http_request"
    HTTP_RESPONSE = "http_response"
    REPLAY_PROOF = "replay_proof"


class EvaluationSubjectKind(StrEnum):
    RIFTX_VERSION = "riftx_version"
    RIFTX_CONFIGURATION = "riftx_configuration"
    EXTERNAL_REFERENCE = "external_reference"


class TrajectoryStepKind(StrEnum):
    PLAN = "plan"
    TOOL = "tool"
    OBSERVATION = "observation"
    EVIDENCE = "evidence"
    DECISION = "decision"


class TargetSpec(EvaluationModel):
    kind: TargetKind
    fixture_path: NonEmpty
    snapshot_digest: Digest
    authorization_scope: NonEmpty


class ResetRecipe(EvaluationModel):
    strategy: ResetStrategy = ResetStrategy.IMMUTABLE_FIXTURE
    expected_digest: Digest


class ScenarioBudget(EvaluationModel):
    max_duration_seconds: int = Field(ge=1)
    max_total_tokens: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    max_target_interactions: int = Field(ge=0)
    max_concurrency: int = Field(default=1, ge=1)


class EvaluationMemoryPolicy(EvaluationModel):
    share_mode: MemoryShareMode = MemoryShareMode.ISOLATED
    allowed_source_ids: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def validate_declared_sources(self) -> EvaluationMemoryPolicy:
        if len(self.allowed_source_ids) != len(set(self.allowed_source_ids)):
            raise ValueError("memory source IDs must be unique")
        if self.share_mode is MemoryShareMode.ISOLATED and self.allowed_source_ids:
            raise ValueError("isolated evaluation scenarios cannot allow shared memory")
        if self.share_mode is MemoryShareMode.DECLARED and not self.allowed_source_ids:
            raise ValueError("declared memory sharing requires at least one source ID")
        return self


class ExpectedFinding(EvaluationModel):
    finding_key: NonEmpty
    title: NonEmpty
    required_evidence: tuple[EvidenceKind, ...] = ()

    @model_validator(mode="after")
    def unique_evidence_kinds(self) -> ExpectedFinding:
        if len(self.required_evidence) != len(set(self.required_evidence)):
            raise ValueError("required evidence kinds must be unique")
        return self


class SecurityScenario(EvaluationModel):
    schema_version: str = Field(pattern=r"^riftx\.security-evaluation-scenario/v1$")
    scenario_id: NonEmpty
    version: NonEmpty
    kind: ScenarioKind
    visibility: ScenarioVisibility
    objective: NonEmpty
    target: TargetSpec
    reset: ResetRecipe
    budget: ScenarioBudget
    memory_policy: EvaluationMemoryPolicy = Field(default_factory=EvaluationMemoryPolicy)
    expected_findings: tuple[ExpectedFinding, ...]

    @model_validator(mode="after")
    def validate_scenario_contract(self) -> SecurityScenario:
        finding_keys = [finding.finding_key for finding in self.expected_findings]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("expected finding keys must be unique")
        expected_target = {
            ScenarioKind.CODE_AUDIT: TargetKind.REPOSITORY_FIXTURE,
            ScenarioKind.PENETRATION_TEST: TargetKind.WEB_FIXTURE,
        }[self.kind]
        if self.target.kind is not expected_target:
            raise ValueError(f"{self.kind.value} requires target kind {expected_target.value}")
        if self.target.snapshot_digest != self.reset.expected_digest:
            raise ValueError("target and reset digests must identify the same fixture")
        return self


class EvidenceReference(EvaluationModel):
    evidence_id: NonEmpty
    kind: EvidenceKind
    locator: NonEmpty
    digest: Digest


class EvidenceReplayCheck(EvaluationModel):
    name: NonEmpty
    passed: bool
    detail: NonEmpty


class EvidenceReplay(EvaluationModel):
    replay_id: NonEmpty
    evidence_ids: tuple[NonEmpty, ...]
    checks: tuple[EvidenceReplayCheck, ...]
    passed: bool
    result_digest: Digest

    @model_validator(mode="after")
    def validate_replay_result(self) -> EvidenceReplay:
        if not self.evidence_ids:
            raise ValueError("evidence replay requires at least one evidence ID")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence replay IDs must be unique")
        if not self.checks:
            raise ValueError("evidence replay requires at least one check")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("replay status must match all replay checks")
        return self


class FindingObservation(EvaluationModel):
    finding_key: NonEmpty
    title: NonEmpty
    disposition: FindingDisposition
    rationale: NonEmpty
    evidence: tuple[EvidenceReference, ...] = ()
    replay: EvidenceReplay | None = None

    @model_validator(mode="after")
    def verified_findings_require_replayable_evidence(self) -> FindingObservation:
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("finding evidence IDs must be unique")
        if self.disposition is FindingDisposition.VERIFIED:
            if not self.evidence or self.replay is None or not self.replay.passed:
                raise ValueError("verified findings require replayable evidence")
            if set(self.replay.evidence_ids) != set(evidence_ids):
                raise ValueError("replay must cover every finding evidence reference")
        elif self.replay is not None:
            raise ValueError("only verified findings can carry a replay result")
        return self


class EvaluationSubmission(EvaluationModel):
    observations: tuple[FindingObservation, ...] = ()

    @model_validator(mode="after")
    def unique_observation_keys(self) -> EvaluationSubmission:
        keys = [observation.finding_key for observation in self.observations]
        if len(keys) != len(set(keys)):
            raise ValueError("submission finding keys must be unique")
        return self


class JudgedFinding(EvaluationModel):
    finding_key: NonEmpty
    expected: bool
    declared_disposition: FindingDisposition | None
    final_disposition: FindingDisposition
    evidence_contract_met: bool
    detail: NonEmpty


class EvaluationJudgement(EvaluationModel):
    judge_version: NonEmpty
    findings: tuple[JudgedFinding, ...]
    disposition_counts: dict[FindingDisposition, int]

    @model_validator(mode="after")
    def validate_disposition_counts(self) -> EvaluationJudgement:
        if set(self.disposition_counts) != set(FindingDisposition):
            raise ValueError("judgement must count every finding disposition")
        observed = {disposition: 0 for disposition in FindingDisposition}
        for finding in self.findings:
            observed[finding.final_disposition] += 1
        if self.disposition_counts != observed:
            raise ValueError("disposition counts must match judged findings")
        return self


class EvaluationSubject(EvaluationModel):
    kind: EvaluationSubjectKind
    name: NonEmpty
    version: NonEmpty


class BuildDescriptor(EvaluationModel):
    commit: NonEmpty
    dirty: bool
    schema_version: NonEmpty
    platform: NonEmpty
    python_version: NonEmpty


class ModelDescriptor(EvaluationModel):
    provider: NonEmpty
    model: NonEmpty
    request_mode: NonEmpty
    configuration_digest: Digest


class RuntimeDescriptor(EvaluationModel):
    control_plane: NonEmpty
    worker: NonEmpty
    runner: NonEmpty
    browser: NonEmpty
    optional_services: tuple[NonEmpty, ...] = ()


class CapabilitySnapshot(EvaluationModel):
    tool_versions: dict[str, str] = Field(default_factory=dict)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    pack_versions: dict[str, str] = Field(default_factory=dict)
    selection_digest: Digest


class ResourceUsage(EvaluationModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    target_interactions: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TrajectoryStep(EvaluationModel):
    sequence: int = Field(ge=0)
    kind: TrajectoryStepKind
    summary: NonEmpty
    tool_id: str | None = None
    capability_ids: tuple[NonEmpty, ...] = ()
    evidence_ids: tuple[NonEmpty, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class EvaluationTrajectory(EvaluationModel):
    steps: tuple[TrajectoryStep, ...] = ()

    @model_validator(mode="after")
    def require_contiguous_sequence(self) -> EvaluationTrajectory:
        sequences = [step.sequence for step in self.steps]
        if sequences != list(range(len(self.steps))):
            raise ValueError("trajectory sequence must be contiguous and start at zero")
        return self


class EvaluationRun(EvaluationModel):
    schema_version: str = Field(pattern=r"^riftx\.security-evaluation-run/v1$")
    run_id: NonEmpty
    scenario_id: NonEmpty
    scenario_version: NonEmpty
    scenario_digest: Digest
    subject: EvaluationSubject
    build: BuildDescriptor
    model: ModelDescriptor
    runtime: RuntimeDescriptor
    capabilities: CapabilitySnapshot
    memory_namespace: NonEmpty
    memory_source_ids: tuple[NonEmpty, ...]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    usage: ResourceUsage
    trajectory: EvaluationTrajectory
    submission: EvaluationSubmission
    judgement: EvaluationJudgement

    @model_validator(mode="after")
    def validate_run_times(self) -> EvaluationRun:
        if self.completed_at < self.started_at:
            raise ValueError("evaluation run cannot complete before it starts")
        return self


class EvaluationRunContext(EvaluationModel):
    run_id: NonEmpty
    subject: EvaluationSubject
    build: BuildDescriptor
    model: ModelDescriptor
    runtime: RuntimeDescriptor
    capabilities: CapabilitySnapshot
    requested_memory_source_ids: tuple[NonEmpty, ...] = ()
    started_at: AwareDatetime
    completed_at: AwareDatetime
    usage: ResourceUsage
    trajectory: EvaluationTrajectory

    @model_validator(mode="after")
    def validate_context(self) -> EvaluationRunContext:
        if self.completed_at < self.started_at:
            raise ValueError("evaluation context cannot complete before it starts")
        if len(self.requested_memory_source_ids) != len(
            set(self.requested_memory_source_ids)
        ):
            raise ValueError("requested memory source IDs must be unique")
        return self


class ResetReceipt(EvaluationModel):
    scenario_id: NonEmpty
    strategy: ResetStrategy
    fixture_digest: Digest
    reset_at: AwareDatetime


class EvaluationComparison(EvaluationModel):
    baseline_run_id: NonEmpty
    candidate_run_id: NonEmpty
    disposition_deltas: dict[FindingDisposition, int]
    token_delta: int
    tool_call_delta: int
    target_interaction_delta: int
    duration_ms_delta: int
    notes: tuple[NonEmpty, ...]


def aware_datetime(value: datetime) -> datetime:
    """Validate timestamps supplied by deterministic harness callers."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamps must be timezone-aware")
    return value
