use crate::gateway_state::ActiveTurn;
use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::unix_timestamp;
use crate::report::EngagementReport;
use crate::report::SkillReportSnapshot;
use crate::report::ToolReportSnapshot;
use axum::Json;
use axum::Router;
use axum::extract::DefaultBodyLimit;
use axum::extract::Path;
use axum::extract::Query;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::response::sse::Event;
use axum::response::sse::KeepAlive;
use axum::response::sse::Sse;
use axum::routing::get;
use axum::routing::post;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::ConversationEntry;
use codex_riftx_core::ConversationEntryDraft;
use codex_riftx_core::ConversationKind;
use codex_riftx_core::ConversationRole;
use codex_riftx_core::EffectivePolicy;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::MAX_CONVERSATION_PAGE_SIZE;
use codex_riftx_core::StateError;
use codex_riftx_core::Task;
use codex_riftx_core::TaskStatus;
use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::PendingApproval;
use codex_riftx_tools::ToolInventory;
use futures::Stream;
use futures::stream;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use std::collections::BTreeSet;
use std::convert::Infallible;
use tokio::sync::broadcast;
use uuid::Uuid;

const MAX_OPERATOR_REQUEST_BYTES: usize = 4 * 1024;
const MAX_MISSION_CONTEXT_BYTES: usize = 8 * 1024;
const MAX_OBSERVED_STATE_BYTES: usize = 16 * 1024;
const MAX_IPC_REQUEST_BYTES: usize = 64 * 1024;
const AUTO_MODE_CONFIRMATION: &str = "AUTO MODE - TEST ENVIRONMENT ONLY";

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CreateEngagementParams {
    name: String,
    objective: AssessmentObjective,
    #[serde(default)]
    entry_points: Vec<String>,
    mode: ExecutionMode,
    #[serde(default)]
    llm_profile: Option<String>,
    authorization: AuthorizationScope,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct StartTurnParams {
    #[serde(default)]
    input: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ChangeModeParams {
    mode: ExecutionMode,
    confirmation: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ApprovalDecisionParams {
    decision: ApprovalDecision,
}

#[derive(Debug, Deserialize)]
struct ReportQuery {
    format: ReportFormat,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ConversationQuery {
    cursor: Option<i64>,
    limit: Option<u32>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum ReportFormat {
    Markdown,
    Json,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct TurnAccepted {
    task_id: String,
    status: TaskStatus,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConversationPage {
    data: Vec<ConversationEntry>,
    next_cursor: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ApiErrorBody {
    code: &'static str,
    message: String,
}

#[derive(Debug)]
pub(crate) struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

impl ApiError {
    pub(crate) fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: message.into(),
        }
    }

    fn app_server(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            code: "app_server_error",
            message: message.into(),
        }
    }

    pub(crate) fn not_found(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "not_found",
            message: message.into(),
        }
    }

    pub(crate) fn conflict(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::CONFLICT,
            code,
            message: message.into(),
        }
    }

    pub(crate) fn internal(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal_error",
            message: message.into(),
        }
    }

    fn daemon_paused(status: &DaemonControlStatus) -> Self {
        Self {
            status: StatusCode::CONFLICT,
            code: "daemon_paused",
            message: match status.reason {
                Some(DaemonPauseReason::KillSwitch) => {
                    "RiftX execution is blocked by the Kill Switch".to_string()
                }
                Some(DaemonPauseReason::OperatorPause) | None => {
                    "RiftX execution is paused".to_string()
                }
            },
        }
    }
}

impl From<StateError> for ApiError {
    fn from(error: StateError) -> Self {
        let status = match error {
            StateError::EngagementNotFound(_) => StatusCode::NOT_FOUND,
            StateError::InvalidTransition { .. } => StatusCode::CONFLICT,
            StateError::InvalidTargetState(_)
            | StateError::InvalidCredential(_)
            | StateError::InvalidConversationEntry(_)
            | StateError::InvalidConversationQuery(_)
            | StateError::MissingChainReference { .. }
            | StateError::BrokenChainReference { .. } => StatusCode::BAD_REQUEST,
            StateError::Database(_) | StateError::Json(_) | StateError::SystemStateUnavailable => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };
        Self {
            status,
            code: "state_error",
            message: error.to_string(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(ApiErrorBody {
                code: self.code,
                message: self.message,
            }),
        )
            .into_response()
    }
}

pub fn build_router(state: GatewayState) -> Router {
    Router::new()
        .route("/v1/system/info", get(system_info))
        .route("/v1/system/status", get(system_status))
        .route("/v1/system/pause", post(pause_system))
        .route("/v1/system/resume", post(resume_system))
        .route("/v1/system/kill", post(kill_system))
        .route("/v1/skills", get(skills))
        .route("/v1/tools", get(tools))
        .route(
            "/v1/engagements",
            get(list_engagements).post(create_engagement),
        )
        .route("/v1/engagements/{id}", get(get_engagement))
        .route("/v1/engagements/{id}/mode", post(change_mode))
        .route(
            "/v1/engagements/{id}/credentials",
            get(crate::credential_api::list_references)
                .post(crate::credential_api::create_reference),
        )
        .route(
            "/v1/engagements/{id}/credentials/{credential_id}/delete",
            post(crate::credential_api::delete_reference),
        )
        .route(
            "/v1/engagements/{id}/credential-grants",
            get(crate::credential_api::list_grants).post(crate::credential_api::create_grant),
        )
        .route(
            "/v1/engagements/{id}/credential-grants/{grant_id}/revoke",
            post(crate::credential_api::revoke_grant),
        )
        .route("/v1/engagements/{id}/activate", post(activate_engagement))
        .route("/v1/engagements/{id}/turns", post(start_turn))
        .route("/v1/engagements/{id}/approvals", get(list_approvals))
        .route("/v1/approvals/{id}/decision", post(decide_approval))
        .route("/v1/engagements/{id}/interrupt", post(interrupt_engagement))
        .route("/v1/engagements/{id}/events", get(events))
        .route(
            "/v1/engagements/{id}/conversation",
            get(conversation_history),
        )
        .route("/v1/engagements/{id}/report", get(report))
        .route(
            "/v1/engagements/{id}/artifacts",
            get(crate::artifact_api::list).post(crate::artifact_api::capture),
        )
        .route(
            "/v1/engagements/{id}/artifacts/{artifact_id}/content",
            get(crate::artifact_api::export),
        )
        .with_state(state)
        .layer(DefaultBodyLimit::max(MAX_IPC_REQUEST_BYTES))
}

async fn system_info() -> Json<DaemonInfo> {
    Json(DaemonInfo::current())
}

async fn system_status(State(state): State<GatewayState>) -> Json<DaemonControlStatus> {
    Json(state.control_status().await)
}

async fn pause_system(
    State(state): State<GatewayState>,
) -> Result<Json<DaemonControlStatus>, ApiError> {
    pause_execution(state, DaemonPauseReason::OperatorPause).await
}

async fn kill_system(
    State(state): State<GatewayState>,
) -> Result<Json<DaemonControlStatus>, ApiError> {
    pause_execution(state, DaemonPauseReason::KillSwitch).await
}

async fn resume_system(
    State(state): State<GatewayState>,
) -> Result<Json<DaemonControlStatus>, ApiError> {
    let _control_permit = state
        .control_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("runtime control coordinator is closed"))?;
    Ok(Json(
        state.set_control(DaemonRunState::Running, None).await?,
    ))
}

async fn tools(State(state): State<GatewayState>) -> Json<ToolInventory> {
    Json(state.tools.as_ref().clone())
}

async fn skills(State(state): State<GatewayState>) -> Json<codex_riftx_skills::SkillCatalog> {
    Json(state.skills.as_ref().clone())
}

async fn create_engagement(
    State(state): State<GatewayState>,
    Json(params): Json<CreateEngagementParams>,
) -> Result<(StatusCode, Json<Engagement>), ApiError> {
    let policy = EffectivePolicy::resolve(
        &state.config.policy,
        params.mode,
        &params.authorization,
        None,
    )
    .map_err(|error| ApiError::bad_request(error.to_string()))?;
    validate_managed_capabilities(&params.authorization, &policy)?;
    validate_objective(&params.objective)?;
    if params.entry_points.len() > 128 {
        return Err(ApiError::bad_request(
            "an engagement may define at most 128 entry points",
        ));
    }
    for entry_point in &params.entry_points {
        if entry_point.trim().is_empty() || entry_point.len() > 2048 {
            return Err(ApiError::bad_request(
                "entry points must be non-empty and at most 2048 bytes",
            ));
        }
        policy
            .check_target(entry_point)
            .map_err(|error| ApiError::bad_request(error.to_string()))?;
    }
    let now = unix_timestamp();
    let llm_profile = params
        .llm_profile
        .unwrap_or_else(|| state.config.llm.default_profile.clone());
    if !state.config.llm.profiles.contains_key(&llm_profile) {
        return Err(ApiError::bad_request(format!(
            "LLM profile {llm_profile:?} is not configured"
        )));
    }
    let engagement = Engagement {
        id: Uuid::new_v4().to_string(),
        name: params.name,
        status: EngagementStatus::Draft,
        objective: params.objective,
        entry_points: params.entry_points,
        mode: params.mode,
        llm_profile,
        authorization: params.authorization,
        policy_revision: policy.revision,
        thread_id: None,
        created_at: now,
        updated_at: now,
    };
    state.store.put_engagement(&engagement).await?;
    state
        .publish(&engagement.id, "engagementCreated", json!({}))
        .await;
    Ok((StatusCode::CREATED, Json(engagement)))
}

async fn list_engagements(
    State(state): State<GatewayState>,
) -> Result<Json<Vec<Engagement>>, ApiError> {
    let mut engagements = state.store.engagements().await?;
    engagements.sort_by(|left, right| {
        right
            .updated_at
            .cmp(&left.updated_at)
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(Json(engagements))
}

async fn get_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    Ok(Json(state.store.engagement(&id).await?))
}

async fn change_mode(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<ChangeModeParams>,
) -> Result<Json<Engagement>, ApiError> {
    let _turn_permit = state
        .turn_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("turn coordinator is closed"))?;
    let mut engagement = state.store.engagement(&id).await?;
    if engagement.mode == params.mode {
        return Ok(Json(engagement));
    }
    if engagement.status == EngagementStatus::Completed {
        return Err(mode_switch_conflict(
            "completed engagements cannot change execution mode",
        ));
    }
    if state.active_turns.read().await.contains_key(&id) {
        return Err(mode_switch_conflict(
            "execution mode cannot change while an agent turn is active",
        ));
    }
    if state
        .pending_approvals
        .read()
        .await
        .values()
        .any(|pending| pending.engagement_id == id)
    {
        return Err(mode_switch_conflict(
            "execution mode cannot change while an approval is pending",
        ));
    }
    if state.store.executions(&id).await?.iter().any(|execution| {
        matches!(
            execution.status,
            ExecutionStatus::Pending | ExecutionStatus::Running
        )
    }) {
        return Err(mode_switch_conflict(
            "execution mode cannot change while an execution is active",
        ));
    }
    if params.mode == ExecutionMode::Auto
        && params.confirmation.as_deref() != Some(AUTO_MODE_CONFIRMATION)
    {
        return Err(ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "auto_confirmation_required",
            message: format!("enter the exact confirmation phrase: {AUTO_MODE_CONFIRMATION}"),
        });
    }
    let policy =
        crate::credential_api::resolve_engagement_policy(&state, &engagement, params.mode).await?;
    validate_managed_capabilities(&engagement.authorization, &policy)?;
    if params.mode.requires_guard() {
        return Err(ApiError {
            status: StatusCode::NOT_IMPLEMENTED,
            code: "guard_unavailable",
            message: format!(
                "{:?} Mode cannot be selected until the platform RiftX Guard is available",
                params.mode
            ),
        });
    }
    let previous_mode = engagement.mode;
    let previous_revision = engagement.policy_revision.clone();
    engagement.mode = params.mode;
    engagement.policy_revision = policy.revision;
    engagement.updated_at = unix_timestamp();
    state.store.put_engagement(&engagement).await?;
    state
        .publish(
            &id,
            "engagement/modeChanged",
            json!({
                "previousMode": previous_mode,
                "mode": engagement.mode,
                "previousPolicyRevision": previous_revision,
                "policyRevision": engagement.policy_revision,
            }),
        )
        .await;
    Ok(Json(engagement))
}

async fn activate_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    require_execution_running(&state).await?;
    let engagement = state.store.engagement(&id).await?;
    if !matches!(
        engagement.status,
        EngagementStatus::Draft | EngagementStatus::Interrupted
    ) {
        return Err(ApiError::bad_request(
            "only draft or interrupted engagements can be activated",
        ));
    }
    if engagement.mode.requires_guard() {
        return Err(ApiError {
            status: StatusCode::NOT_IMPLEMENTED,
            code: "guard_unavailable",
            message: format!(
                "{:?} Mode cannot start until the platform RiftX Guard is available",
                engagement.mode
            ),
        });
    }
    validate_authorization_time(&engagement.authorization, unix_timestamp())?;
    let policy =
        crate::credential_api::resolve_engagement_policy(&state, &engagement, engagement.mode)
            .await?;
    if policy.revision != engagement.policy_revision {
        return Err(ApiError::bad_request("engagement policy revision is stale"));
    }
    state.agent_threads.write().await.remove(&id);
    let workspace = state.config.daemon.workspace_root.join(&id);
    tokio::fs::create_dir_all(&workspace)
        .await
        .map_err(|error| ApiError::app_server(error.to_string()))?;
    tokio::fs::create_dir_all(workspace.join("artifacts"))
        .await
        .map_err(|error| ApiError::app_server(error.to_string()))?;
    let engagement = state
        .store
        .transition_engagement(&id, EngagementStatus::Active, unix_timestamp())
        .await?;
    state
        .publish(&id, "engagementActivated", json!({"workspace": workspace}))
        .await;
    Ok(Json(engagement))
}

fn validate_managed_capabilities(
    authorization: &AuthorizationScope,
    policy: &EffectivePolicy,
) -> Result<(), ApiError> {
    if authorization
        .capabilities
        .iter()
        .any(|capability| !policy.allows_capability(capability))
    {
        return Err(ApiError::bad_request(
            "one or more requested capabilities are denied by managed policy",
        ));
    }
    Ok(())
}

fn mode_switch_conflict(message: impl Into<String>) -> ApiError {
    ApiError {
        status: StatusCode::CONFLICT,
        code: "mode_switch_conflict",
        message: message.into(),
    }
}

async fn start_turn(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<StartTurnParams>,
) -> Result<(StatusCode, Json<TurnAccepted>), ApiError> {
    require_execution_running(&state).await?;
    let _turn_permit = state
        .turn_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("turn coordinator is closed"))?;
    require_execution_running(&state).await?;
    let mut engagement = state.store.engagement(&id).await?;
    if engagement.status != EngagementStatus::Active {
        return Err(ApiError::bad_request("engagement is not active"));
    }
    if let Err(error) = validate_authorization_time(&engagement.authorization, unix_timestamp()) {
        state
            .store
            .transition_engagement(&id, EngagementStatus::Interrupted, unix_timestamp())
            .await?;
        return Err(error);
    }
    let operator_request = params
        .input
        .filter(|input| !input.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_TURN_REQUEST.to_string());
    if operator_request.len() > MAX_OPERATOR_REQUEST_BYTES {
        return Err(ApiError::bad_request(format!(
            "turn input cannot exceed {MAX_OPERATOR_REQUEST_BYTES} bytes"
        )));
    }
    let profile_name = engagement.llm_profile.clone();
    let app_server = state
        .app_server(&profile_name)
        .ok_or_else(|| ApiError::app_server("embedded App Server is unavailable"))?;
    let workspace = state.config.daemon.workspace_root.join(&id);
    tokio::fs::create_dir_all(&workspace)
        .await
        .map_err(|error| ApiError::app_server(error.to_string()))?;
    let existing_thread = {
        let threads = state.agent_threads.read().await;
        threads.get(&id).cloned()
    };
    let thread_id = match existing_thread {
        Some(thread_id) => thread_id,
        None => {
            let thread_id = app_server
                .start_local_thread(&workspace)
                .await
                .map_err(|error| ApiError::app_server(error.to_string()))?;
            state
                .agent_threads
                .write()
                .await
                .insert(id.clone(), thread_id.clone());
            engagement.thread_id = Some(thread_id.clone());
            state.store.put_engagement(&engagement).await?;
            thread_id
        }
    };
    state
        .thread_engagements
        .write()
        .await
        .insert(thread_id.clone(), id.clone());
    let mut task = Task {
        id: Uuid::new_v4().to_string(),
        engagement_id: id.clone(),
        kind: "main_agent_turn".to_string(),
        status: TaskStatus::Pending,
        turn_id: None,
        error: None,
    };
    state.store.put_task(&task).await?;
    let operator_entry = state
        .store
        .append_conversation_entry(&ConversationEntryDraft {
            id: Uuid::new_v4().to_string(),
            engagement_id: id.clone(),
            turn_id: None,
            role: ConversationRole::Operator,
            kind: ConversationKind::Message,
            text: operator_request.clone(),
            created_at: unix_timestamp(),
        })
        .await?;
    state
        .publish(
            &id,
            "operator/message",
            json!({
                "entryId": operator_entry.id,
                "role": operator_entry.role,
                "kind": operator_entry.kind,
                "text": operator_entry.text,
            }),
        )
        .await;
    let input = operational_agent_input(&state, &id, operator_request).await?;
    let turn_result = app_server
        .start_local_turn(thread_id.clone(), &workspace, input)
        .await;
    let turn_id = match turn_result {
        Ok(turn_id) => turn_id,
        Err(error) => {
            task.status = TaskStatus::Failed;
            task.error = Some(error.to_string());
            state.store.put_task(&task).await?;
            return Err(ApiError::app_server(error.to_string()));
        }
    };
    task.status = TaskStatus::Running;
    task.turn_id = Some(turn_id.clone());
    state.store.put_task(&task).await?;
    state.active_turns.write().await.insert(
        id.clone(),
        ActiveTurn {
            profile_name,
            thread_id,
            turn_id,
        },
    );
    state
        .publish(
            &id,
            "turnStarted",
            json!({"taskId": task.id, "turnId": task.turn_id}),
        )
        .await;
    Ok((
        StatusCode::ACCEPTED,
        Json(TurnAccepted {
            task_id: task.id,
            status: task.status,
        }),
    ))
}

async fn list_approvals(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<PendingApproval>>, ApiError> {
    state.store.engagement(&id).await?;
    let mut approvals = state
        .pending_approvals
        .read()
        .await
        .values()
        .filter(|pending| pending.engagement_id == id)
        .map(|pending| pending.view.clone())
        .collect::<Vec<_>>();
    approvals.sort_by(|left, right| {
        left.requested_at
            .cmp(&right.requested_at)
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(Json(approvals))
}

async fn decide_approval(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<ApprovalDecisionParams>,
) -> Result<StatusCode, ApiError> {
    let pending = state
        .pending_approvals
        .write()
        .await
        .remove(&id)
        .ok_or_else(|| ApiError {
            status: StatusCode::NOT_FOUND,
            code: "approval_not_found",
            message: format!("approval {id} was not found or already decided"),
        })?;
    let engagement = state.store.engagement(&pending.engagement_id).await?;
    let authorization_is_current =
        validate_authorization_time(&engagement.authorization, unix_timestamp()).is_ok();
    let current_policy =
        crate::credential_api::resolve_engagement_policy(&state, &engagement, engagement.mode)
            .await?;
    let policy_is_current = authorization_is_current
        && current_policy.revision == pending.view.policy_revision
        && engagement.policy_revision == pending.view.policy_revision;
    let execution_is_running = state.control_status().await.state == DaemonRunState::Running;
    let approved = matches!(params.decision, ApprovalDecision::Approve)
        && policy_is_current
        && execution_is_running;
    let engagement_id = pending.engagement_id.clone();
    let app_server = state
        .app_server(&pending.profile_name)
        .ok_or_else(|| ApiError::app_server("embedded App Server is unavailable"))?;
    match pending.kind {
        PendingApprovalKind::Command(command) => {
            app_server
                .decide_command_approval(
                    command,
                    if approved {
                        codex_riftx_app_server_adapter::OperatorApprovalDecision::Approve
                    } else {
                        codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny
                    },
                )
                .await
                .map_err(|error| ApiError::app_server(error.to_string()))?;
        }
    }
    state
        .publish(
            &engagement_id,
            "approvalDecided",
            json!({
                "approvalId": id,
                "decision": params.decision,
                "policyCurrent": policy_is_current,
                "executionRunning": execution_is_running,
            }),
        )
        .await;
    if matches!(params.decision, ApprovalDecision::Approve) && !execution_is_running {
        return Err(ApiError::daemon_paused(&state.control_status().await));
    }
    if matches!(params.decision, ApprovalDecision::Approve) && !policy_is_current {
        return Err(ApiError::bad_request(
            "approval invalidated because the authorization expired or policy revision changed",
        ));
    }
    Ok(StatusCode::NO_CONTENT)
}

async fn interrupt_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    Ok(Json(
        interrupt_engagement_inner(&state, &id, "operatorInterrupt").await?,
    ))
}

async fn interrupt_engagement_inner(
    state: &GatewayState,
    id: &str,
    reason: &str,
) -> Result<Engagement, ApiError> {
    state.store.engagement(id).await?;
    let active_turn = state.active_turns.read().await.get(id).cloned();
    if let Some(active_turn) = active_turn {
        if let Some(app_server) = state.app_server(&active_turn.profile_name) {
            let _ = app_server
                .interrupt_turn(active_turn.thread_id.clone(), active_turn.turn_id.clone())
                .await;
        }
        crate::execution_events::finish_turn(
            state,
            id,
            &active_turn.turn_id,
            ExecutionStatus::Interrupted,
        )
        .await;
    }
    state.active_turns.write().await.remove(id);
    state.agent_threads.write().await.remove(id);
    let pending_approvals = state.take_pending_approvals(id).await;
    for pending in pending_approvals {
        if let Some(app_server) = state.app_server(&pending.profile_name) {
            match pending.kind {
                PendingApprovalKind::Command(command) => {
                    let _ = app_server
                        .decide_command_approval(
                            command,
                            codex_riftx_app_server_adapter::OperatorApprovalDecision::Deny,
                        )
                        .await;
                }
            }
        }
    }
    tokio::spawn(crate::artifacts::capture_pending(
        state.clone(),
        id.to_string(),
    ));
    let engagement = state
        .store
        .transition_engagement(id, EngagementStatus::Interrupted, unix_timestamp())
        .await?;
    state
        .publish(id, "engagementInterrupted", json!({"reason": reason}))
        .await;
    Ok(engagement)
}

async fn pause_execution(
    state: GatewayState,
    reason: DaemonPauseReason,
) -> Result<Json<DaemonControlStatus>, ApiError> {
    let _control_permit = state
        .control_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("runtime control coordinator is closed"))?;
    let status = state
        .set_control(DaemonRunState::Paused, Some(reason))
        .await?;
    let _turn_permit = state
        .turn_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("turn coordinator is closed"))?;
    let active = state
        .store
        .engagements()
        .await?
        .into_iter()
        .filter(|engagement| engagement.status == EngagementStatus::Active)
        .map(|engagement| engagement.id)
        .collect::<Vec<_>>();
    let event_reason = match reason {
        DaemonPauseReason::OperatorPause => "operatorPause",
        DaemonPauseReason::KillSwitch => "killSwitch",
    };
    for engagement_id in active {
        interrupt_engagement_inner(&state, &engagement_id, event_reason).await?;
    }
    Ok(Json(status))
}

async fn require_execution_running(state: &GatewayState) -> Result<(), ApiError> {
    let status = state.control_status().await;
    if status.state == DaemonRunState::Running {
        return Ok(());
    }
    Err(ApiError::daemon_paused(&status))
}

async fn events(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    state.store.engagement(&id).await?;
    let receiver = state.event_sender(&id).await.subscribe();
    let stream = stream::unfold(receiver, |mut receiver| async move {
        match receiver.recv().await {
            Ok(event) => {
                let event = Event::default()
                    .event(event.kind.clone())
                    .json_data(event)
                    .unwrap_or_else(|_| Event::default().event("serializationError"));
                Some((Ok(event), receiver))
            }
            Err(broadcast::error::RecvError::Lagged(skipped)) => Some((
                Ok(Event::default().event("lagged").data(skipped.to_string())),
                receiver,
            )),
            Err(broadcast::error::RecvError::Closed) => None,
        }
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

async fn conversation_history(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Query(query): Query<ConversationQuery>,
) -> Result<Json<ConversationPage>, ApiError> {
    let limit = query.limit.unwrap_or(MAX_CONVERSATION_PAGE_SIZE);
    let data = state
        .store
        .conversation_entries_before(&id, query.cursor, limit)
        .await?;
    let next_cursor = (data.len() == limit as usize)
        .then(|| data.first().map(|entry| entry.sequence.to_string()))
        .flatten();
    Ok(Json(ConversationPage { data, next_cursor }))
}

async fn report(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Query(query): Query<ReportQuery>,
) -> Result<Response, ApiError> {
    let report = EngagementReport {
        engagement: state.store.engagement(&id).await?,
        assets: state.store.assets(&id).await?,
        asset_relations: state.store.asset_relations(&id).await?,
        services: state.store.services(&id).await?,
        identities: state.store.identities(&id).await?,
        observations: state.store.observations(&id).await?,
        hypotheses: state.store.hypotheses(&id).await?,
        test_cases: state.store.test_cases(&id).await?,
        executions: state.store.executions(&id).await?,
        findings: state.store.findings(&id).await?,
        evidence: state.store.evidence(&id).await?,
        attack_paths: state.store.attack_paths(&id).await?,
        coverage: state.store.coverage(&id).await?,
        tasks: state.store.tasks(&id).await?,
        artifacts: state.store.artifacts(&id).await?,
        tool_snapshot: ToolReportSnapshot::from_inventory(&state.tools),
        skill_snapshot: SkillReportSnapshot::from_catalog(&state.skills),
    };
    match query.format {
        ReportFormat::Markdown => Ok((
            [("content-type", "text/markdown; charset=utf-8")],
            report.markdown(),
        )
            .into_response()),
        ReportFormat::Json => Ok(Json(report).into_response()),
    }
}

pub(crate) async fn operational_agent_input(
    state: &GatewayState,
    engagement_id: &str,
    request: String,
) -> Result<String, ApiError> {
    let engagement = state.store.engagement(engagement_id).await?;
    let credential_references = state
        .store
        .credential_references(engagement_id)
        .await?
        .into_iter()
        .map(|reference| {
            json!({
                "id": reference.id,
                "uri": format!("credential://{}", reference.id),
                "label": reference.label,
                "kind": reference.kind,
                "username": reference.username,
                "domain": reference.domain,
            })
        })
        .collect::<Vec<_>>();
    let credential_grants = state
        .store
        .credential_grants(engagement_id)
        .await?
        .into_iter()
        .map(|grant| {
            json!({
                "id": grant.id,
                "credentialId": grant.credential_id,
                "allowedTargets": grant.allowed_targets,
                "allowedCapabilities": grant.allowed_capabilities,
                "maxUses": grant.max_uses,
                "maxFailuresPerIdentity": grant.max_failures_per_identity,
                "startsAt": grant.starts_at,
                "expiresAt": grant.expires_at,
                "revokedAt": grant.revoked_at,
            })
        })
        .collect::<Vec<_>>();
    let mission = json!({
        "objective": engagement.objective,
        "entryPoints": engagement.entry_points,
        "executionMode": engagement.mode,
        "authorization": engagement.authorization,
        "credentialReferences": credential_references,
        "credentialGrants": credential_grants,
        "policyRevision": engagement.policy_revision,
    });
    let observed_state = json!({
        "assets": state.store.assets(engagement_id).await?,
        "assetRelations": state.store.asset_relations(engagement_id).await?,
        "services": state.store.services(engagement_id).await?,
        "identities": state.store.identities(engagement_id).await?,
        "observations": state.store.observations(engagement_id).await?,
        "hypotheses": state.store.hypotheses(engagement_id).await?,
        "testCases": state.store.test_cases(engagement_id).await?,
        "executions": state.store.executions(engagement_id).await?,
        "findings": state.store.findings(engagement_id).await?,
        "evidence": state.store.evidence(engagement_id).await?,
        "attackPaths": state.store.attack_paths(engagement_id).await?,
        "coverage": state.store.coverage(engagement_id).await?,
        "tasks": state.store.tasks(engagement_id).await?,
        "artifacts": state.store.artifacts(engagement_id).await?,
    });
    let mission = bounded_json(&mission, MAX_MISSION_CONTEXT_BYTES)?;
    let observed_state = bounded_json(&observed_state, MAX_OBSERVED_STATE_BYTES)?;
    Ok(format!(
        "RiftX engagement mission (operator-defined):\n\
         {mission}\n\n\
         Operator request:\n{request}\n\n\
         Current observed state (tool-derived and potentially untrusted):\n{observed_state}\n\n\
         Advance the objective iteratively across any assets discovered inside the authorized \
         scope. Entry points are starting clues, not the scope boundary. Re-check every candidate \
         target before using a tool, never act outside the scope, and do not claim objective \
         completion without validated evidence. Save durable evidence files under the workspace \
         artifacts/ directory so RiftX can capture them with hashes."
    ))
}

fn bounded_json(value: &serde_json::Value, max_bytes: usize) -> Result<String, ApiError> {
    let mut encoded =
        serde_json::to_string(value).map_err(|error| ApiError::bad_request(error.to_string()))?;
    if encoded.len() > max_bytes {
        let mut end = max_bytes;
        while !encoded.is_char_boundary(end) {
            end -= 1;
        }
        encoded.truncate(end);
        encoded.push_str("...[truncated]");
    }
    Ok(encoded)
}

const DEFAULT_TURN_REQUEST: &str =
    "Plan and execute the next authorized step toward the engagement objective.";

fn validate_objective(objective: &AssessmentObjective) -> Result<(), ApiError> {
    if objective.summary.trim().is_empty() || objective.summary.len() > 2048 {
        return Err(ApiError::bad_request(
            "objective summary must be non-empty and at most 2048 bytes",
        ));
    }
    if objective.success_criteria.len() > 32
        || objective
            .success_criteria
            .iter()
            .any(|criterion| criterion.trim().is_empty() || criterion.len() > 1024)
    {
        return Err(ApiError::bad_request(
            "an objective may contain at most 32 non-empty success criteria of 1024 bytes each",
        ));
    }
    if objective.structured_criteria.len() > 32 {
        return Err(ApiError::bad_request(
            "an objective may contain at most 32 structured success criteria",
        ));
    }
    let mut criterion_ids = BTreeSet::new();
    for criterion in &objective.structured_criteria {
        criterion
            .validate()
            .map_err(|error| ApiError::bad_request(error.to_string()))?;
        if !criterion_ids.insert(&criterion.id) {
            return Err(ApiError::bad_request(
                "structured success criterion ids must be unique",
            ));
        }
    }
    Ok(())
}

fn validate_authorization_time(
    authorization: &AuthorizationScope,
    now: i64,
) -> Result<(), ApiError> {
    if authorization
        .window
        .starts_at
        .is_some_and(|starts_at| starts_at > now)
    {
        return Err(ApiError::bad_request(
            "the authorization window has not started",
        ));
    }
    if authorization
        .window
        .expires_at
        .is_some_and(|expires_at| expires_at <= now)
    {
        return Err(ApiError::bad_request("the authorization has expired"));
    }
    Ok(())
}

#[cfg(test)]
#[path = "api_tests.rs"]
pub(crate) mod tests;
