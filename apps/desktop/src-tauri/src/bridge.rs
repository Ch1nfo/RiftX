use codex_riftx_ipc::DaemonInfo;
use codex_riftx_ipc::LocalIpcClient;
use codex_riftx_ipc::LocalIpcEndpoint;
use codex_riftx_ipc::LocalIpcError;
use codex_riftx_ipc::LocalIpcResponse;
use serde::Deserialize;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use serde_json::json;
use std::env;
use std::path::Path;
use std::path::PathBuf;

const CONFIG_ENV: &str = "RIFTX_CONFIG";
const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone)]
pub(crate) struct DesktopState {
    client: Option<LocalIpcClient>,
    config_path: Option<PathBuf>,
    startup_error: Option<DesktopError>,
}

impl DesktopState {
    pub(crate) fn load() -> Self {
        match load_endpoint() {
            Ok((config_path, endpoint)) => Self {
                client: Some(LocalIpcClient::new(endpoint)),
                config_path: Some(config_path),
                startup_error: None,
            },
            Err(error) => Self {
                client: None,
                config_path: None,
                startup_error: Some(error),
            },
        }
    }

    fn client(&self) -> Result<LocalIpcClient, DesktopError> {
        self.client
            .clone()
            .ok_or_else(|| self.startup_error.clone().unwrap_or_else(unavailable))
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopError {
    code: String,
    message: String,
}

impl DesktopError {
    fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopDaemonInfo {
    protocol_version: u32,
    daemon_version: String,
    config_path: PathBuf,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct EngagementView {
    id: String,
    name: String,
    status: String,
    objective: ObjectiveView,
    #[serde(default)]
    entry_points: Vec<String>,
    mode: String,
    authorization: AuthorizationView,
    policy_revision: String,
    thread_id: Option<String>,
    created_at: i64,
    updated_at: i64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct ObjectiveView {
    summary: String,
    success_criteria: Vec<String>,
    structured_criteria: Vec<Value>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AuthorizationView {
    network: NetworkScopeView,
    identities: Vec<Value>,
    capabilities: Vec<String>,
    environment: String,
    window: AuthorizationWindowView,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct NetworkScopeView {
    cidrs: Vec<String>,
    domains: Vec<String>,
    ports: Vec<u16>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AuthorizationWindowView {
    starts_at: Option<i64>,
    expires_at: Option<i64>,
}

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
    mode: String,
    environment: String,
    #[serde(default)]
    capabilities: Vec<String>,
    #[serde(default)]
    identities: Vec<Value>,
    starts_at: Option<i64>,
    expires_at: Option<i64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct TurnAccepted {
    task_id: String,
    status: String,
}

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
    state: tauri::State<'_, DesktopState>,
) -> Result<DesktopDaemonInfo, DesktopError> {
    let client = state.client()?;
    let info: DaemonInfo = json_response(client.get("/v1/system/info").await).await?;
    Ok(DesktopDaemonInfo {
        protocol_version: info.protocol_version,
        daemon_version: info.daemon_version,
        config_path: state.config_path.clone().ok_or_else(unavailable)?,
    })
}

#[tauri::command]
pub(crate) async fn list_engagements(
    state: tauri::State<'_, DesktopState>,
) -> Result<Vec<EngagementView>, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/engagements").await).await
}

#[tauri::command]
pub(crate) async fn create_engagement(
    state: tauri::State<'_, DesktopState>,
    input: CreateEngagementInput,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    let body = json!({
        "name": input.name,
        "objective": {
            "summary": input.objective,
            "successCriteria": input.success_criteria,
            "structuredCriteria": [],
        },
        "entryPoints": input.entry_points,
        "mode": input.mode,
        "authorization": {
            "network": {
                "cidrs": input.cidrs,
                "domains": input.domains,
                "ports": input.ports,
            },
            "identities": input.identities,
            "capabilities": input.capabilities,
            "environment": input.environment,
            "window": {
                "startsAt": input.starts_at,
                "expiresAt": input.expires_at,
            },
        },
    });
    let body = serde_json::to_vec(&body)
        .map_err(|error| DesktopError::new("encode_request", error.to_string()))?;
    json_response(client.post_json("/v1/engagements", body).await).await
}

#[tauri::command]
pub(crate) async fn activate_engagement(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<EngagementView, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .post(&format!("/v1/engagements/{engagement_id}/activate"))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn start_turn(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
    input: String,
) -> Result<TurnAccepted, DesktopError> {
    let client = state.client()?;
    let body = serde_json::to_vec(&json!({"input": input}))
        .map_err(|error| DesktopError::new("encode_request", error.to_string()))?;
    json_response(
        client
            .post_json(&format!("/v1/engagements/{engagement_id}/turns"), body)
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
) -> Result<Value, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .get(&format!(
                "/v1/engagements/{engagement_id}/report?format=json"
            ))
            .await,
    )
    .await
}

async fn json_response<T>(
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

fn load_endpoint() -> Result<(PathBuf, LocalIpcEndpoint), DesktopError> {
    let config_path = find_config_path()?;
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
