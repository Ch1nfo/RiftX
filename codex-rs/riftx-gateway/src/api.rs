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
use codex_riftx_core::EffectivePolicy;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::Scope;
use codex_riftx_core::StateError;
use codex_riftx_core::StateStore;
use codex_riftx_core::Task;
use codex_riftx_core::TaskStatus;
use codex_riftx_manager_client::CreateSandboxRequest;
use codex_riftx_manager_client::ManagerClient;
use codex_riftx_manager_client::SandboxResources;
use codex_riftx_manager_client::SandboxScope;
use futures::Stream;
use futures::stream;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use subtle::ConstantTimeEq;
use tokio::sync::RwLock;
use tokio::sync::broadcast;
use uuid::Uuid;

#[derive(Clone)]
pub struct GatewayState {
    pub config: Arc<RiftxConfig>,
    pub store: StateStore,
    pub manager: ManagerClient,
    events: Arc<RwLock<HashMap<String, broadcast::Sender<GatewayEvent>>>>,
}

impl GatewayState {
    pub fn new(config: RiftxConfig, store: StateStore, manager: ManagerClient) -> Self {
        Self {
            config: Arc::new(config),
            store,
            manager,
            events: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    async fn publish(&self, engagement_id: &str, kind: &str, data: Value) {
        let sender = self.event_sender(engagement_id).await;
        let _ = sender.send(GatewayEvent {
            engagement_id: engagement_id.to_string(),
            kind: kind.to_string(),
            timestamp: unix_timestamp(),
            data,
        });
    }

    async fn event_sender(&self, engagement_id: &str) -> broadcast::Sender<GatewayEvent> {
        if let Some(sender) = self.events.read().await.get(engagement_id) {
            return sender.clone();
        }
        let mut events = self.events.write().await;
        events
            .entry(engagement_id.to_string())
            .or_insert_with(|| broadcast::channel(256).0)
            .clone()
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct GatewayEvent {
    engagement_id: String,
    kind: String,
    timestamp: i64,
    data: Value,
}

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

#[derive(Debug, Deserialize, Serialize)]
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
    if engagement.status != EngagementStatus::Draft {
        return Err(ApiError::bad_request(
            "only draft engagements can be activated",
        ));
    }
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
    let sandbox = state
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
    engagement.sandbox_id = Some(sandbox.id);
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
    let engagement = state.store.engagement(&id).await?;
    if engagement.status != EngagementStatus::Active {
        return Err(ApiError::bad_request("engagement is not active"));
    }
    if params.input.trim().is_empty() {
        return Err(ApiError::bad_request("turn input cannot be empty"));
    }
    let task = Task {
        id: Uuid::new_v4().to_string(),
        engagement_id: id.clone(),
        kind: "agent_turn".to_string(),
        status: TaskStatus::Pending,
        turn_id: None,
        error: None,
    };
    state.store.put_task(&task).await?;
    state
        .publish(
            &id,
            "turnQueued",
            json!({"taskId": task.id, "input": params.input}),
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
) -> StatusCode {
    state
        .publish(
            "approvals",
            "approvalDecided",
            json!({"approvalId": id, "decision": params.decision}),
        )
        .await;
    StatusCode::NO_CONTENT
}

async fn interrupt_engagement(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Engagement>, ApiError> {
    let engagement = state.store.engagement(&id).await?;
    if let Some(sandbox_id) = &engagement.sandbox_id {
        state
            .manager
            .interrupt(sandbox_id)
            .await
            .map_err(|error| ApiError::upstream(error.to_string()))?;
    }
    let engagement = state
        .store
        .transition_engagement(&id, EngagementStatus::Interrupted, unix_timestamp())
        .await?;
    state.publish(&id, "engagementInterrupted", json!({})).await;
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
        findings: state.store.findings(&id).await?,
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

fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}

#[cfg(test)]
#[path = "api_tests.rs"]
mod tests;
