use crate::AssessmentObjective;
use crate::AuthorizationScope;
use serde::Deserialize;
use serde::Serialize;

/// Immutable safety budgets applied to one Auto run.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoRunLimits {
    pub max_turns: u32,
    pub max_tool_calls: u32,
    pub max_wall_clock_seconds: u64,
    pub max_single_command_seconds: u64,
    pub max_consecutive_failures: u32,
    pub no_progress_window: u32,
    pub max_model_tokens_or_cost: Option<u64>,
}

impl Default for AutoRunLimits {
    fn default() -> Self {
        Self {
            max_turns: 20,
            max_tool_calls: 100,
            max_wall_clock_seconds: 3_600,
            max_single_command_seconds: 300,
            max_consecutive_failures: 3,
            no_progress_window: 3,
            max_model_tokens_or_cost: None,
        }
    }
}

/// Secret-free LLM Profile snapshot bound to an Auto run.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoLlmProfileSnapshot {
    pub name: String,
    pub model: String,
    pub base_url: String,
    pub protocol: String,
    pub timeout_seconds: u64,
    pub reasoning_level: String,
    pub context_budget: u32,
    pub config_sha256: String,
}

/// Immutable inputs and safety limits captured before Auto execution starts.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoRunConfig {
    pub objective: AssessmentObjective,
    pub authorization: AuthorizationScope,
    pub llm_profile: AutoLlmProfileSnapshot,
    pub tools_snapshot_sha256: String,
    pub policy_revision: String,
    pub expires_at: i64,
    pub limits: AutoRunLimits,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum AutoRunState {
    Ready,
    Running,
    Evaluating,
    Paused,
    NeedsInput,
    Succeeded,
    Expired,
    BudgetExhausted,
    Failed,
    Killed,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum AutoStopReason {
    OperatorPause,
    AuthorizationExpired,
    TurnBudgetExhausted,
    ToolBudgetExhausted,
    WallClockBudgetExhausted,
    ConsecutiveFailures,
    NoProgress,
    AuditUnavailable,
    ProviderAuthentication,
    ProviderProtocolError,
    DaemonRestart,
    KillSwitch,
    UnrecoverableError,
    ScopeNeedsInput,
    SuccessCriteriaMet,
}

/// Deterministic result for one structured Auto success criterion.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoCriterionAssessment {
    pub criterion_id: String,
    pub satisfied: bool,
    pub evidence_ids: Vec<String>,
}

/// Persisted output from the RiftX goal evaluator.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoGoalAssessment {
    pub evaluator_version: String,
    pub evaluated_at: i64,
    pub succeeded: bool,
    pub criteria: Vec<AutoCriterionAssessment>,
    pub evidence_ids: Vec<String>,
}

/// Fixed-size fingerprint for one category in the structured engagement state.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoProgressCategory {
    pub item_count: u32,
    pub sha256: String,
}

/// Structured state captured immediately before an Auto turn starts.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoProgressSnapshot {
    pub assets: AutoProgressCategory,
    pub services: AutoProgressCategory,
    pub identities: AutoProgressCategory,
    pub findings: AutoProgressCategory,
    pub evidence: AutoProgressCategory,
    pub artifacts: AutoProgressCategory,
    pub attack_paths: AutoProgressCategory,
    pub coverage: AutoProgressCategory,
    pub observations: AutoProgressCategory,
    pub hypotheses: AutoProgressCategory,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum AutoProgressSignal {
    Asset,
    Service,
    Identity,
    Finding,
    Evidence,
    Artifact,
    AttackPath,
    Coverage,
    Observation,
    Hypothesis,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum AutoProgressAction {
    Continue,
    Replan,
    SwitchStrategy,
    NeedsInput,
}

/// Persisted deterministic progress decision for the latest completed turn.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoProgressAssessment {
    pub evaluator_version: String,
    pub evaluated_at: i64,
    pub progressed: bool,
    pub signals: Vec<AutoProgressSignal>,
    pub no_progress_turns: u32,
    pub action: AutoProgressAction,
}

/// Persisted checkpoint for one engagement's bounded Auto run.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AutoRun {
    pub engagement_id: String,
    pub config: AutoRunConfig,
    pub state: AutoRunState,
    pub stop_reason: Option<AutoStopReason>,
    pub current_subgoal: Option<String>,
    pub turns_started: u32,
    pub turns_completed: u32,
    pub tool_calls: u32,
    pub consecutive_failures: u32,
    pub no_progress_turns: u32,
    #[serde(default)]
    pub last_goal_assessment: Option<AutoGoalAssessment>,
    #[serde(default)]
    pub progress_baseline: Option<AutoProgressSnapshot>,
    #[serde(default)]
    pub last_progress_assessment: Option<AutoProgressAssessment>,
    pub started_at: Option<i64>,
    pub updated_at: i64,
}
