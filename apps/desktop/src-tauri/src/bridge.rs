use codex_riftx_ipc::ApprovalDecision;
use codex_riftx_ipc::ApprovalDecisionParams;
use codex_riftx_ipc::AssessmentObjective;
use codex_riftx_ipc::AuthorizationScope;
use codex_riftx_ipc::AuthorizationWindow;
use codex_riftx_ipc::ChangeModeParams;
use codex_riftx_ipc::ConversationPage;
use codex_riftx_ipc::CreateEngagementParams;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::Engagement;
use codex_riftx_ipc::EngagementReport;
use codex_riftx_ipc::EngagementStatus;
use codex_riftx_ipc::EnvironmentClass;
use codex_riftx_ipc::ExecutionMode;
use codex_riftx_ipc::IPC_PROTOCOL_VERSION;
use codex_riftx_ipc::IdentitySelector;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcError;
use codex_riftx_ipc::LocalIpcResponse;
use codex_riftx_ipc::PendingApproval;
use codex_riftx_ipc::ReportFormat;
use codex_riftx_ipc::Scope;
use codex_riftx_ipc::StartTurnParams;
use codex_riftx_ipc::TurnAccepted;
use serde::Deserialize;
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::env;
use std::path::Path;
use std::path::PathBuf;

const CONFIG_ENV: &str = "RIFTX_CONFIG";
const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

pub(crate) mod event_stream;

pub(crate) struct DesktopState {
    client: Option<LocalIpcClient>,
    config_path: Option<PathBuf>,
    startup_error: Option<DesktopError>,
    subscriptions: event_stream::SubscriptionRegistry,
    pub(crate) notifications: crate::notifications::NotificationManager,
    pub(crate) daemon: crate::daemon::DaemonSupervisor,
}

impl DesktopState {
    pub(crate) fn load() -> Self {
        match load_endpoint() {
            Ok((config_path, endpoint)) => Self {
                client: Some(LocalIpcClient::new(endpoint)),
                config_path: Some(config_path),
                startup_error: None,
                subscriptions: event_stream::SubscriptionRegistry::default(),
                notifications: crate::notifications::NotificationManager::default(),
                daemon: crate::daemon::DaemonSupervisor::default(),
            },
            Err(error) => Self {
                client: None,
                config_path: None,
                startup_error: Some(error),
                subscriptions: event_stream::SubscriptionRegistry::default(),
                notifications: crate::notifications::NotificationManager::default(),
                daemon: crate::daemon::DaemonSupervisor::default(),
            },
        }
    }

    pub(crate) fn client(&self) -> Result<LocalIpcClient, DesktopError> {
        self.client
            .clone()
            .ok_or_else(|| self.startup_error.clone().unwrap_or_else(unavailable))
    }

    pub(crate) fn config_path(&self) -> Result<&Path, DesktopError> {
        self.config_path.as_deref().ok_or_else(unavailable)
    }

    pub(crate) async fn query_daemon_info(&self) -> Result<DesktopDaemonInfo, DesktopError> {
        let client = self.client()?;
        let info: DaemonInfo = json_response(client.get("/v1/system/info").await).await?;
        validate_protocol_version(info.protocol_version)?;
        let runtime = self.query_runtime_status().await?;
        Ok(DesktopDaemonInfo {
            protocol_version: info.protocol_version,
            daemon_version: info.daemon_version,
            config_path: self.config_path.clone().ok_or_else(unavailable)?,
            runtime,
        })
    }

    pub(crate) async fn query_runtime_status(&self) -> Result<DaemonControlStatus, DesktopError> {
        let client = self.client()?;
        json_response(client.get("/v1/system/status").await).await
    }

    pub(crate) async fn update_runtime(
        &self,
        path: &str,
    ) -> Result<DaemonControlStatus, DesktopError> {
        let client = self.client()?;
        json_response(client.post(path).await).await
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopError {
    code: String,
    message: String,
}

impl DesktopError {
    pub(crate) fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    pub(crate) fn is_code(&self, code: &str) -> bool {
        self.code == code
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopDaemonInfo {
    protocol_version: u32,
    daemon_version: String,
    config_path: PathBuf,
    runtime: DaemonControlStatus,
}

pub(crate) type EngagementView = Engagement;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateEngagementInput {
    name: String,
    objective: String,
    #[serde(default)]
    success_criteria: Vec<String>,
    #[serde(default)]
    entry_points: Vec<String>,
    cidrs: Vec<String>,
    #[serde(default)]
    domains: Vec<String>,
    #[serde(default)]
    ports: Vec<u16>,
    mode: ExecutionMode,
    llm_profile: Option<String>,
    environment: EnvironmentClass,
    #[serde(default)]
    capabilities: Vec<String>,
    #[serde(default)]
    identities: Vec<IdentitySelector>,
    starts_at: Option<i64>,
    expires_at: Option<i64>,
    #[serde(default)]
    confirmation: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ChangeModeInput {
    engagement_id: String,
    mode: ExecutionMode,
    confirmation: Option<String>,
}

pub(crate) type ConversationPageView = ConversationPage;

#[derive(Debug, Deserialize)]
struct EndpointConfig {
    daemon: EndpointDaemonConfig,
}

#[derive(Debug, Deserialize)]
struct EndpointDaemonConfig {
    ipc_dir: PathBuf,
}

#[derive(Debug, Deserialize)]
struct ApiErrorBody {
    code: String,
    message: String,
}

#[tauri::command]
pub(crate) async fn daemon_info(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<DesktopDaemonInfo, DesktopError> {
    state.daemon.ensure_running(&app, &state).await?;
    let info = state.query_daemon_info().await?;
    crate::background::sync_runtime_status(&app, &info.runtime);
    Ok(info)
}

#[tauri::command]
pub(crate) async fn pause_runtime(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<DaemonControlStatus, DesktopError> {
    update_runtime(&app, &state, "/v1/system/pause").await
}

#[tauri::command]
pub(crate) async fn resume_runtime(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<DaemonControlStatus, DesktopError> {
    update_runtime(&app, &state, "/v1/system/resume").await
}

#[tauri::command]
pub(crate) async fn kill_runtime(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<DaemonControlStatus, DesktopError> {
    update_runtime(&app, &state, "/v1/system/kill").await
}

#[tauri::command]
pub(crate) async fn list_engagements(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<Vec<EngagementView>, DesktopError> {
    let client = state.client()?;
    let engagements: Vec<EngagementView> =
        json_response(client.get("/v1/engagements").await).await?;
    state.subscriptions.sync_active(
        &app,
        client,
        engagements
            .iter()
            .filter(|engagement| engagement.status == EngagementStatus::Active)
            .map(|engagement| engagement.id.clone()),
    )?;
    Ok(engagements)
}

#[tauri::command]
pub(crate) async fn create_engagement(
    state: tauri::State<'_, DesktopState>,
    input: CreateEngagementInput,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    let cidrs = input
        .cidrs
        .into_iter()
        .map(|cidr| {
            cidr.parse()
                .map_err(|error| DesktopError::new("invalid_cidr", format!("{cidr}: {error}")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let params = CreateEngagementParams {
        name: input.name,
        objective: AssessmentObjective {
            summary: input.objective,
            success_criteria: input.success_criteria,
            structured_criteria: Vec::new(),
        },
        entry_points: input.entry_points,
        mode: input.mode,
        llm_profile: input.llm_profile,
        confirmation: input.confirmation,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs,
                domains: input.domains,
                ports: input.ports,
            },
            identities: input.identities,
            capabilities: input.capabilities,
            environment: input.environment,
            window: AuthorizationWindow {
                starts_at: input.starts_at,
                expires_at: input.expires_at,
            },
        },
    };
    json_response(client.post_typed("/v1/engagements", &params).await).await
}

#[tauri::command]
pub(crate) async fn activate_engagement(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    let engagement: EngagementView = json_response(
        client
            .post(&format!("/v1/engagements/{engagement_id}/activate"))
            .await,
    )
    .await?;
    state
        .subscriptions
        .ensure_active(&app, client, engagement.id.clone())?;
    Ok(engagement)
}

#[tauri::command]
pub(crate) async fn change_engagement_mode(
    state: tauri::State<'_, DesktopState>,
    input: ChangeModeInput,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    let (path, params) = mode_change_request(input)?;
    json_response(client.post_typed(&path, &params).await).await
}

#[tauri::command]
pub(crate) async fn start_turn(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
    input: String,
) -> Result<TurnAccepted, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .post_typed(
                &format!("/v1/engagements/{engagement_id}/turns"),
                &StartTurnParams { input: Some(input) },
            )
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn list_approvals(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<Vec<PendingApproval>, DesktopError> {
    validate_engagement_id(&engagement_id)?;
    let client = state.client()?;
    json_response(
        client
            .get(&format!("/v1/engagements/{engagement_id}/approvals"))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn decide_approval(
    state: tauri::State<'_, DesktopState>,
    approval_id: String,
    decision: ApprovalDecision,
) -> Result<(), DesktopError> {
    validate_opaque_id("approval", &approval_id)?;
    let client = state.client()?;
    empty_response(
        client
            .post_typed(
                &format!("/v1/approvals/{approval_id}/decision"),
                &ApprovalDecisionParams { decision },
            )
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn interrupt_engagement(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .post(&format!("/v1/engagements/{engagement_id}/interrupt"))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn engagement_report(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<EngagementReport, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .get(&report_path(&engagement_id, ReportFormat::Json)?)
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn engagement_report_markdown(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<String, DesktopError> {
    let client = state.client()?;
    text_response(
        client
            .get(&report_path(&engagement_id, ReportFormat::Markdown)?)
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn conversation_history(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
    cursor: Option<i64>,
) -> Result<ConversationPageView, DesktopError> {
    let path = conversation_path(&engagement_id, cursor)?;
    let client = state.client()?;
    json_response(client.get(&path).await).await
}

pub(crate) async fn json_response<T>(
    response: Result<LocalIpcResponse, LocalIpcError>,
) -> Result<T, DesktopError>
where
    T: DeserializeOwned,
{
    let response =
        response.map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    let status = response.status();
    let bytes = response
        .bytes()
        .await
        .map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    if bytes.len() > MAX_RESPONSE_BYTES {
        return Err(DesktopError::new(
            "response_too_large",
            "riftxd returned a response larger than the desktop limit",
        ));
    }
    if !status.is_success() {
        let error = serde_json::from_slice::<ApiErrorBody>(&bytes).unwrap_or(ApiErrorBody {
            code: "daemon_error".to_string(),
            message: format!("riftxd returned HTTP {status}"),
        });
        return Err(DesktopError::new(error.code, error.message));
    }
    serde_json::from_slice(&bytes)
        .map_err(|error| DesktopError::new("invalid_daemon_response", error.to_string()))
}

async fn empty_response(
    response: Result<LocalIpcResponse, LocalIpcError>,
) -> Result<(), DesktopError> {
    let response =
        response.map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    let status = response.status();
    let bytes = response
        .bytes()
        .await
        .map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    if status.is_success() {
        return Ok(());
    }
    let error = serde_json::from_slice::<ApiErrorBody>(&bytes).unwrap_or(ApiErrorBody {
        code: "daemon_error".to_string(),
        message: format!("riftxd returned HTTP {status}"),
    });
    Err(DesktopError::new(error.code, error.message))
}

async fn text_response(
    response: Result<LocalIpcResponse, LocalIpcError>,
) -> Result<String, DesktopError> {
    let response =
        response.map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    let status = response.status();
    let bytes = response
        .bytes()
        .await
        .map_err(|error| DesktopError::new("daemon_unavailable", error.to_string()))?;
    if bytes.len() > MAX_RESPONSE_BYTES {
        return Err(DesktopError::new(
            "response_too_large",
            "riftxd returned a response larger than the desktop limit",
        ));
    }
    if !status.is_success() {
        let error = serde_json::from_slice::<ApiErrorBody>(&bytes).unwrap_or(ApiErrorBody {
            code: "daemon_error".to_string(),
            message: format!("riftxd returned HTTP {status}"),
        });
        return Err(DesktopError::new(error.code, error.message));
    }
    String::from_utf8(bytes.to_vec())
        .map_err(|error| DesktopError::new("invalid_daemon_response", error.to_string()))
}

async fn update_runtime(
    app: &tauri::AppHandle,
    state: &DesktopState,
    path: &str,
) -> Result<DaemonControlStatus, DesktopError> {
    let status = state.update_runtime(path).await?;
    crate::background::sync_runtime_status(app, &status);
    Ok(status)
}

fn load_endpoint() -> Result<(PathBuf, LocalIpcEndpoint), DesktopError> {
    let config_path = find_config_path()?.canonicalize().map_err(|error| {
        DesktopError::new(
            "config_unavailable",
            format!("resolve RiftX config path: {error}"),
        )
    })?;
    let content = std::fs::read_to_string(&config_path).map_err(|error| {
        DesktopError::new(
            "config_unavailable",
            format!("read {}: {error}", config_path.display()),
        )
    })?;
    let config: EndpointConfig = toml::from_str(&content)
        .map_err(|error| DesktopError::new("invalid_config", error.to_string()))?;
    let base = config_path.parent().unwrap_or_else(|| Path::new("."));
    let ipc_dir = if config.daemon.ipc_dir.is_absolute() {
        config.daemon.ipc_dir
    } else {
        base.join(config.daemon.ipc_dir)
    };
    Ok((config_path, LocalIpcEndpoint::new(ipc_dir)))
}

fn find_config_path() -> Result<PathBuf, DesktopError> {
    if let Some(config_path) = env::var_os(CONFIG_ENV) {
        return Ok(PathBuf::from(config_path));
    }
    let current_dir = env::current_dir()
        .map_err(|error| DesktopError::new("config_unavailable", error.to_string()))?;
    current_dir
        .ancestors()
        .map(|directory| directory.join("riftx.toml"))
        .find(|candidate| candidate.is_file())
        .ok_or_else(|| {
            DesktopError::new(
                "config_unavailable",
                format!("riftx.toml was not found; set {CONFIG_ENV}"),
            )
        })
}

fn unavailable() -> DesktopError {
    DesktopError::new(
        "daemon_unavailable",
        "RiftX daemon connection is unavailable",
    )
}

fn validate_engagement_id(engagement_id: &str) -> Result<(), DesktopError> {
    validate_opaque_id("engagement", engagement_id)
}

fn conversation_path(engagement_id: &str, cursor: Option<i64>) -> Result<String, DesktopError> {
    validate_engagement_id(engagement_id)?;
    match cursor {
        Some(cursor) if cursor <= 0 => Err(DesktopError::new(
            "invalid_cursor",
            "conversation cursor must be positive",
        )),
        Some(cursor) => Ok(format!(
            "/v1/engagements/{engagement_id}/conversation?limit=200&cursor={cursor}"
        )),
        None => Ok(format!(
            "/v1/engagements/{engagement_id}/conversation?limit=200"
        )),
    }
}

fn mode_change_request(input: ChangeModeInput) -> Result<(String, ChangeModeParams), DesktopError> {
    validate_engagement_id(&input.engagement_id)?;
    let path = format!("/v1/engagements/{}/mode", input.engagement_id);
    Ok((
        path,
        ChangeModeParams {
            mode: input.mode,
            confirmation: input.confirmation,
        },
    ))
}

fn report_path(engagement_id: &str, format: ReportFormat) -> Result<String, DesktopError> {
    validate_engagement_id(engagement_id)?;
    Ok(format!(
        "/v1/engagements/{engagement_id}/report?format={}",
        format.as_str()
    ))
}

pub(crate) fn validate_opaque_id(kind: &str, id: &str) -> Result<(), DesktopError> {
    if !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Ok(());
    }
    Err(DesktopError::new(
        "invalid_identifier",
        format!("{kind} identifier is invalid"),
    ))
}

fn validate_protocol_version(protocol_version: u32) -> Result<(), DesktopError> {
    if protocol_version == IPC_PROTOCOL_VERSION {
        return Ok(());
    }
    Err(DesktopError::new(
        "protocol_mismatch",
        format!(
            "RiftX Desktop requires IPC protocol {IPC_PROTOCOL_VERSION}, but riftxd provides {protocol_version}"
        ),
    ))
}

#[cfg(test)]
#[path = "bridge_tests.rs"]
mod tests;
