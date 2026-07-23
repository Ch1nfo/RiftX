use crate::gateway_state::ActiveTurn;
use crate::gateway_state::GatewayState;
use crate::gateway_state::PendingApprovalKind;
use crate::gateway_state::unix_timestamp;
use crate::report::EngagementReport;
use axum::Json;
use axum::Router;
use axum::extract::Path;
use axum::extract::Query;
use axum::extract::Request;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::middleware;
use axum::middleware::Next;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::response::sse::Event;
use axum::response::sse::KeepAlive;
use axum::response::sse::Sse;
use axum::routing::get;
use axum::routing::post;
use codex_riftx_app_server_adapter::EnvironmentRegistration;
use codex_riftx_app_server_adapter::RemoteEnvironment;
use codex_riftx_core::EffectivePolicy;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::Scope;
use codex_riftx_core::StateError;
use codex_riftx_core::Task;
use codex_riftx_core::TaskStatus;
use codex_riftx_manager_client::CreateSandboxRequest;
use codex_riftx_manager_client::SandboxResources;
use codex_riftx_manager_client::SandboxScope;
use futures::Stream;
use futures::stream;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use std::convert::Infallible;
use std::sync::Arc;
use subtle::ConstantTimeEq;
use tokio::sync::broadcast;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct CreateEngagementParams {
    name: String,
    scope: Scope,
    tool_profile: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct StartTurnParams {
    input: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ApprovalDecisionParams {
    decision: ApprovalDecision,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
enum ApprovalDecision {
    Approve,
    Deny,
}

#[derive(Debug, Deserialize)]
struct ReportQuery {
    format: ReportFormat,
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
struct ApiErrorBody {
    code: &'static str,
    message: String,
}

struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

impl ApiError {
    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: message.into(),
        }
    }

    fn upstream(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            code: "manager_error",
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
}

impl From<StateError> for ApiError {
    fn from(error: StateError) -> Self {
        let status = match error {
            StateError::EngagementNotFound(_) => StatusCode::NOT_FOUND,
            StateError::InvalidTransition { .. } => StatusCode::CONFLICT,
            StateError::Database(_) | StateError::Json(_) => StatusCode::INTERNAL_SERVER_ERROR,
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

pub fn build_router(state: GatewayState, operator_token: String) -> Router {
    Router::new()
        .route("/v1/engagements", post(create_engagement))
        .route("/v1/engagements/{id}", get(get_engagement))
        .route("/v1/engagements/{id}/activate", post(activate_engagement))
        .route("/v1/engagements/{id}/turns", post(start_turn))
        .route("/v1/approvals/{id}/decision", post(decide_approval))
        .route("/v1/engagements/{id}/interrupt", post(interrupt_engagement))
        .route("/v1/engagements/{id}/events", get(events))
        .route("/v1/engagements/{id}/report", get(report))
        .with_state(state)
        .layer(middleware::from_fn_with_state(
            Arc::<str>::from(operator_token),
            authorize,
        ))
}

async fn authorize(
    State(operator_token): State<Arc<str>>,
    headers: HeaderMap,
    request: Request,
    next: Next,
) -> Response {
    let provided = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    let authorized = provided
        .is_some_and(|provided| provided.as_bytes().ct_eq(operator_token.as_bytes()).into());
    if authorized {
        next.run(request).await
    } else {
        StatusCode::UNAUTHORIZED.into_response()
    }
}

async fn create_engagement(
    State(state): State<GatewayState>,
    Json(params): Json<CreateEngagementParams>,
) -> Result<(StatusCode, Json<Engagement>), ApiError> {
    let profile = state
        .config
        .tool_profiles
        .get(&params.tool_profile)
        .ok_or_else(|| ApiError::bad_request("unknown tool profile"))?;
    let policy = EffectivePolicy::resolve(&state.config.policy, &params.scope, profile, None)
        .map_err(|error| ApiError::bad_request(error.to_string()))?;
    let now = unix_timestamp();
    let engagement = Engagement {
        id: Uuid::new_v4().to_string(),
        name: params.name,
        status: EngagementStatus::Draft,
        scope: params.scope,
        tool_profile: params.tool_profile,
        policy_revision: policy.revision,
        sandbox_id: None,
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

async fn get_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    Ok(Json(state.store.engagement(&id).await?))
}

async fn activate_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    let mut engagement = state.store.engagement(&id).await?;
    if !matches!(
        engagement.status,
        EngagementStatus::Draft | EngagementStatus::Interrupted
    ) {
        return Err(ApiError::bad_request(
            "only draft or interrupted engagements can be activated",
        ));
    }
    let app_server = state
        .app_server
        .as_ref()
        .ok_or_else(|| ApiError::app_server("embedded App Server is unavailable"))?
        .clone();
    let profile = state
        .config
        .tool_profiles
        .get(&engagement.tool_profile)
        .ok_or_else(|| ApiError::bad_request("unknown tool profile"))?;
    let policy = EffectivePolicy::resolve(&state.config.policy, &engagement.scope, profile, None)
        .map_err(|error| ApiError::bad_request(error.to_string()))?;
    if policy.revision != engagement.policy_revision {
        return Err(ApiError::bad_request("engagement policy revision is stale"));
    }
    if engagement.status == EngagementStatus::Interrupted
        && let Some(sandbox_id) = engagement.sandbox_id.take()
    {
        let _ = state.manager.kill(&sandbox_id).await;
        let _ = state.manager.delete(&sandbox_id).await;
        engagement.thread_id = None;
    }
    let mut sandbox = state
        .manager
        .create_sandbox(&CreateSandboxRequest {
            engagement_id: engagement.id.clone(),
            image: state.config.sandbox.image.clone(),
            profile: engagement.tool_profile.clone(),
            policy_revision: policy.revision,
            resources: SandboxResources {
                cpu_limit: state.config.sandbox.cpu_limit,
                memory_mib: state.config.sandbox.memory_mib,
                pids_limit: state.config.sandbox.pids_limit,
            },
            scope: SandboxScope {
                cidrs: policy
                    .allowed_cidrs
                    .iter()
                    .map(ToString::to_string)
                    .collect(),
                domains: policy.allowed_domains.into_iter().collect(),
                ports: policy.allowed_ports.into_iter().collect(),
                denied_cidrs: policy
                    .denied_cidrs
                    .iter()
                    .map(ToString::to_string)
                    .collect(),
                denied_domains: policy.denied_domains.into_iter().collect(),
            },
        })
        .await
        .map_err(|error| ApiError::upstream(error.to_string()))?;
    let Some(bootstrap_token) = sandbox.bootstrap_token.take() else {
        let _ = state.manager.kill(&sandbox.id).await;
        let _ = state.manager.delete(&sandbox.id).await;
        return Err(ApiError::upstream(
            "managerd did not return the one-time bootstrap credential",
        ));
    };
    let registration = EnvironmentRegistration::new(
        sandbox.environment_id.clone(),
        sandbox.exec_server_url.clone(),
        Some(state.config.manager.request_timeout_ms),
        bootstrap_token.into_inner(),
    );
    if let Err(error) = app_server.add_environment(registration).await {
        let _ = state.manager.kill(&sandbox.id).await;
        let _ = state.manager.delete(&sandbox.id).await;
        return Err(ApiError::app_server(error.to_string()));
    }
    if let Err(error) = app_server
        .environment_info(sandbox.environment_id.clone())
        .await
    {
        let _ = state.manager.kill(&sandbox.id).await;
        let _ = state.manager.delete(&sandbox.id).await;
        return Err(ApiError::app_server(error.to_string()));
    }
    engagement.sandbox_id = Some(sandbox.id);
    engagement.thread_id = None;
    state.store.put_engagement(&engagement).await?;
    let engagement = state
        .store
        .transition_engagement(&id, EngagementStatus::Active, unix_timestamp())
        .await?;
    state
        .publish(
            &id,
            "engagementActivated",
            json!({"sandboxId": engagement.sandbox_id}),
        )
        .await;
    Ok(Json(engagement))
}

async fn start_turn(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<StartTurnParams>,
) -> Result<(StatusCode, Json<TurnAccepted>), ApiError> {
    let _turn_permit = state
        .turn_slot
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::app_server("turn coordinator is closed"))?;
    let mut engagement = state.store.engagement(&id).await?;
    if engagement.status != EngagementStatus::Active {
        return Err(ApiError::bad_request("engagement is not active"));
    }
    if params.input.trim().is_empty() {
        return Err(ApiError::bad_request("turn input cannot be empty"));
    }
    let app_server = state
        .app_server
        .as_ref()
        .ok_or_else(|| ApiError::app_server("embedded App Server is unavailable"))?;
    let sandbox_id = engagement
        .sandbox_id
        .as_deref()
        .ok_or_else(|| ApiError::bad_request("engagement has no sandbox"))?;
    let sandbox = match state.manager.sandbox(sandbox_id).await {
        Ok(sandbox) => sandbox,
        Err(error) => {
            state
                .store
                .transition_engagement(&id, EngagementStatus::Interrupted, unix_timestamp())
                .await?;
            return Err(ApiError::upstream(error.to_string()));
        }
    };
    let environment = RemoteEnvironment {
        environment_id: sandbox.environment_id,
        cwd: "/workspace".to_string(),
    };
    let thread_id = match engagement.thread_id.clone() {
        Some(thread_id) => thread_id,
        None => {
            let thread_id = app_server
                .start_remote_thread(environment.clone())
                .await
                .map_err(|error| ApiError::app_server(error.to_string()))?;
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
        kind: "agent_turn".to_string(),
        status: TaskStatus::Pending,
        turn_id: None,
        error: None,
    };
    state.store.put_task(&task).await?;
    let turn_id = match app_server
        .start_remote_turn(thread_id.clone(), environment, params.input)
        .await
    {
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
    state
        .active_turns
        .write()
        .await
        .insert(id.clone(), ActiveTurn { thread_id, turn_id });
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
    let profile = state
        .config
        .tool_profiles
        .get(&engagement.tool_profile)
        .ok_or_else(|| ApiError::bad_request("engagement tool profile no longer exists"))?;
    let current_policy =
        EffectivePolicy::resolve(&state.config.policy, &engagement.scope, profile, None)
            .map_err(|error| ApiError::bad_request(error.to_string()))?;
    let policy_is_current = current_policy.revision == pending.policy_revision
        && engagement.policy_revision == pending.policy_revision;
    let approved = matches!(params.decision, ApprovalDecision::Approve) && policy_is_current;
    let engagement_id = pending.engagement_id.clone();
    let app_server = state
        .app_server
        .as_ref()
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
        PendingApprovalKind::Dynamic {
            pending,
            request,
            environment_id,
        } => {
            if approved {
                if !current_policy.allows_tool(request.name()) {
                    app_server
                        .complete_dynamic_tool_call(
                            pending,
                            Err("tool is no longer allowed by the effective profile"),
                        )
                        .await
                        .map_err(|error| ApiError::app_server(error.to_string()))?;
                    return Err(ApiError::bad_request(
                        "tool is no longer allowed by the effective profile",
                    ));
                }
                if let Some(error) = request
                    .targets()
                    .into_iter()
                    .find_map(|target| current_policy.check_target(target).err())
                {
                    let message = error.to_string();
                    app_server
                        .complete_dynamic_tool_call(pending, Err(&message))
                        .await
                        .map_err(|error| ApiError::app_server(error.to_string()))?;
                    return Err(ApiError::bad_request(message));
                }
                crate::app_events::spawn_dynamic_execution(
                    state.clone(),
                    engagement_id.clone(),
                    pending,
                    request,
                    environment_id,
                );
            } else {
                app_server
                    .complete_dynamic_tool_call(pending, Err("operator denied the tool call"))
                    .await
                    .map_err(|error| ApiError::app_server(error.to_string()))?;
            }
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
            }),
        )
        .await;
    if matches!(params.decision, ApprovalDecision::Approve) && !policy_is_current {
        return Err(ApiError::bad_request(
            "approval invalidated because the policy revision changed",
        ));
    }
    Ok(StatusCode::NO_CONTENT)
}

async fn interrupt_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    let engagement = state.store.engagement(&id).await?;
    let active_turn = state.active_turns.read().await.get(&id).cloned();
    if let Some(active_turn) = active_turn
        && let Some(app_server) = &state.app_server
    {
        let _ = app_server
            .interrupt_turn(active_turn.thread_id, active_turn.turn_id)
            .await;
    }
    let manager_result = match &engagement.sandbox_id {
        Some(sandbox_id) => state.manager.interrupt(sandbox_id).await.map(|_| ()),
        None => Ok(()),
    };
    state.active_turns.write().await.remove(&id);
    let engagement = state
        .store
        .transition_engagement(&id, EngagementStatus::Interrupted, unix_timestamp())
        .await?;
    state.publish(&id, "engagementInterrupted", json!({})).await;
    manager_result.map_err(|error| ApiError::upstream(error.to_string()))?;
    Ok(Json(engagement))
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

async fn report(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Query(query): Query<ReportQuery>,
) -> Result<Response, ApiError> {
    let report = EngagementReport {
        engagement: state.store.engagement(&id).await?,
        assets: state.store.assets(&id).await?,
        services: state.store.services(&id).await?,
        findings: state.store.findings(&id).await?,
        evidence: state.store.evidence(&id).await?,
        tasks: state.store.tasks(&id).await?,
        artifacts: state.store.artifacts(&id).await?,
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

#[cfg(test)]
#[path = "api_tests.rs"]
mod tests;
