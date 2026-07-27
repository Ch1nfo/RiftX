use crate::runtime_manager::ProfileRuntimeError;
use crate::runtime_manager::ProfileRuntimeManager;
use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxAppServerRequestHandle;
use codex_riftx_artifacts::ArtifactStore;
use codex_riftx_core::AuditWriter;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateError;
use codex_riftx_core::StateStore;
use codex_riftx_core::TaskStatus;
use codex_riftx_credentials::AssessmentCredentialStore;
use codex_riftx_credentials::AssessmentSecretProvider;
use codex_riftx_ipc::AuditHealthState;
use codex_riftx_ipc::AuditHealthStatus;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::EngagementEvent;
use codex_riftx_ipc::PendingApproval;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_tools::ToolInventory;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::RwLock as StdRwLock;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use tokio::sync::RwLock;
use tokio::sync::Semaphore;
use tokio::sync::broadcast;
use tokio_util::sync::CancellationToken;

pub(crate) const DAEMON_CONTROL_STATE_KEY: &str = "daemonControl";
const PROFILE_RUNTIME_FAILURE_PREFIX: &str = "llmProfileRuntimeFailure:";

#[derive(Debug, Serialize, Deserialize)]
struct ProfileRuntimeFailure {
    detail: Option<String>,
}

fn profile_runtime_failure_key(profile_name: &str) -> String {
    format!("{PROFILE_RUNTIME_FAILURE_PREFIX}{profile_name}")
}

#[derive(Clone)]
pub struct GatewayState {
    pub config: Arc<RiftxConfig>,
    pub store: StateStore,
    pub skills: Arc<SkillCatalog>,
    pub tools: Arc<ToolInventory>,
    pub(crate) artifact_store: Arc<ArtifactStore>,
    pub(crate) audit: AuditWriter,
    pub(crate) app_servers: Arc<StdRwLock<HashMap<String, RiftxAppServerRequestHandle>>>,
    runtime_manager: Option<Arc<ProfileRuntimeManager>>,
    runtime_start: Arc<Semaphore>,
    pub(crate) events: Arc<RwLock<HashMap<String, broadcast::Sender<EngagementEvent>>>>,
    pub(crate) thread_engagements: Arc<RwLock<HashMap<String, String>>>,
    pub(crate) active_turns: Arc<RwLock<HashMap<String, ActiveTurn>>>,
    pub(crate) agent_threads: Arc<RwLock<HashMap<String, String>>>,
    pub(crate) pending_approvals: Arc<RwLock<HashMap<String, PendingApprovalRequest>>>,
    pub(crate) active_executions: Arc<RwLock<HashMap<ExecutionKey, ActiveExecution>>>,
    pub(crate) credential_processes: Arc<RwLock<HashMap<String, ActiveCredentialProcess>>>,
    pub(crate) assessment_credentials: Arc<dyn AssessmentSecretProvider>,
    pub(crate) tool_search_path: Arc<Vec<PathBuf>>,
    pub(crate) turn_slot: Arc<Semaphore>,
    pub(crate) control_slot: Arc<Semaphore>,
    pub(crate) control_write_slot: Arc<Semaphore>,
    pub(crate) control: Arc<RwLock<DaemonControlStatus>>,
}

#[derive(Clone)]
pub(crate) struct ActiveTurn {
    pub(crate) profile_name: String,
    pub(crate) thread_id: String,
    pub(crate) turn_id: String,
}

pub(crate) struct PendingApprovalRequest {
    pub(crate) profile_name: String,
    pub(crate) engagement_id: String,
    pub(crate) view: PendingApproval,
    pub(crate) kind: PendingApprovalKind,
}

pub(crate) enum PendingApprovalKind {
    Command(Box<PendingCommandApproval>),
    Tool {
        decision_tx: tokio::sync::oneshot::Sender<bool>,
    },
}

#[derive(Clone)]
pub(crate) struct ActiveCredentialProcess {
    pub(crate) engagement_id: String,
    pub(crate) cancellation: CancellationToken,
}

impl GatewayState {
    pub fn new(
        config: RiftxConfig,
        store: StateStore,
        skills: SkillCatalog,
        tools: ToolInventory,
    ) -> Self {
        let audit = store.audit_writer(&config.audit);
        let artifact_store = ArtifactStore::new(&config.artifacts, store.record_cipher());
        let mut tool_search_path = tools.path_entries.clone();
        if let Some(system_path) = std::env::var_os("PATH") {
            tool_search_path.extend(
                std::env::split_paths(&system_path).filter(|path| !path.as_os_str().is_empty()),
            );
        }
        Self {
            config: Arc::new(config),
            store,
            skills: Arc::new(skills),
            tools: Arc::new(tools),
            artifact_store: Arc::new(artifact_store),
            audit,
            app_servers: Arc::new(StdRwLock::new(HashMap::new())),
            runtime_manager: None,
            runtime_start: Arc::new(Semaphore::new(1)),
            events: Arc::new(RwLock::new(HashMap::new())),
            thread_engagements: Arc::new(RwLock::new(HashMap::new())),
            active_turns: Arc::new(RwLock::new(HashMap::new())),
            agent_threads: Arc::new(RwLock::new(HashMap::new())),
            pending_approvals: Arc::new(RwLock::new(HashMap::new())),
            active_executions: Arc::new(RwLock::new(HashMap::new())),
            credential_processes: Arc::new(RwLock::new(HashMap::new())),
            assessment_credentials: Arc::new(AssessmentCredentialStore::default()),
            tool_search_path: Arc::new(tool_search_path),
            turn_slot: Arc::new(Semaphore::new(1)),
            control_slot: Arc::new(Semaphore::new(1)),
            control_write_slot: Arc::new(Semaphore::new(1)),
            control: Arc::new(RwLock::new(DaemonControlStatus {
                state: DaemonRunState::Running,
                reason: None,
                updated_at: unix_timestamp(),
                audit: AuditHealthStatus {
                    state: AuditHealthState::Healthy,
                    message: None,
                    updated_at: unix_timestamp(),
                },
            })),
        }
    }

    pub fn with_assessment_credentials(
        mut self,
        provider: Arc<dyn AssessmentSecretProvider>,
    ) -> Self {
        self.assessment_credentials = provider;
        self
    }

    pub fn with_app_server(
        self,
        profile_name: String,
        app_server: RiftxAppServerRequestHandle,
    ) -> Self {
        match self.app_servers.write() {
            Ok(mut app_servers) => {
                app_servers.insert(profile_name, app_server);
            }
            Err(poisoned) => {
                poisoned.into_inner().insert(profile_name, app_server);
            }
        }
        self
    }

    pub fn with_runtime_manager(mut self, manager: ProfileRuntimeManager) -> Self {
        self.runtime_manager = Some(Arc::new(manager));
        self
    }

    pub(crate) fn app_server(&self, profile_name: &str) -> Option<RiftxAppServerRequestHandle> {
        self.app_servers
            .read()
            .ok()
            .and_then(|servers| servers.get(profile_name).cloned())
    }

    pub(crate) fn runtime_ready(&self, profile_name: &str) -> bool {
        self.app_server(profile_name).is_some()
    }

    pub(crate) fn cancel_profile_model_requests(&self, profile_name: &str) {
        if let Some(manager) = &self.runtime_manager {
            manager.cancel_bridge_requests(profile_name);
        }
    }

    pub(crate) fn runtime_configured(&self, profile_name: &str) -> bool {
        self.runtime_manager
            .as_ref()
            .is_some_and(|manager| manager.configured(profile_name))
            || self.runtime_ready(profile_name)
    }

    pub(crate) fn has_runtime_manager(&self) -> bool {
        self.runtime_manager.is_some()
    }

    pub(crate) async fn runtime_failure(&self, profile_name: &str) -> Option<String> {
        self.store
            .system_state::<ProfileRuntimeFailure>(&profile_runtime_failure_key(profile_name))
            .await
            .ok()
            .flatten()
            .and_then(|failure| failure.detail)
    }

    pub(crate) async fn record_runtime_failure(&self, profile_name: &str, detail: String) {
        let _ = self
            .store
            .put_system_state(
                &profile_runtime_failure_key(profile_name),
                &ProfileRuntimeFailure {
                    detail: Some(detail),
                },
            )
            .await;
    }

    pub(crate) async fn clear_runtime_failure(&self, profile_name: &str) {
        let _ = self
            .store
            .put_system_state(
                &profile_runtime_failure_key(profile_name),
                &ProfileRuntimeFailure { detail: None },
            )
            .await;
    }

    pub(crate) async fn ensure_app_server(
        &self,
        profile_name: &str,
    ) -> Result<RiftxAppServerRequestHandle, ProfileRuntimeError> {
        if let Some(handle) = self.app_server(profile_name) {
            return Ok(handle);
        }
        let _start = self
            .runtime_start
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| ProfileRuntimeError::Start {
                profile_name: profile_name.to_string(),
                message: "Runtime startup coordinator is unavailable".into(),
            })?;
        if let Some(handle) = self.app_server(profile_name) {
            return Ok(handle);
        }
        let manager = self
            .runtime_manager
            .as_ref()
            .ok_or_else(|| ProfileRuntimeError::Unconfigured(profile_name.to_string()))?;
        let mut runtime = match manager.start_profile(profile_name).await {
            Ok(runtime) => runtime,
            Err(error) => {
                self.record_runtime_failure(profile_name, error.to_string())
                    .await;
                return Err(error);
            }
        };
        manager.retain_bridge(profile_name.to_string(), &mut runtime)?;
        let handle = runtime.handle.clone();
        self.app_servers
            .write()
            .map_err(|_| ProfileRuntimeError::Start {
                profile_name: profile_name.to_string(),
                message: "App Server state lock is unavailable".into(),
            })?
            .insert(profile_name.to_string(), handle.clone());
        self.clear_runtime_failure(profile_name).await;
        self.spawn_app_server_event_pump(profile_name.to_string(), runtime.adapter);
        Ok(handle)
    }

    pub fn spawn_app_server_event_pump(
        &self,
        profile_name: String,
        mut adapter: RiftxAppServerAdapter,
    ) {
        let state = self.clone();
        tokio::spawn(async move {
            let failure = loop {
                match adapter.next_event().await {
                    Ok(Some(event)) => {
                        crate::app_events::process(&state, &profile_name, event).await
                    }
                    Ok(None) => break "App Server Runtime closed unexpectedly".to_string(),
                    Err(error) => {
                        let message = error.to_string();
                        state
                            .publish_to_profile_active(
                                &profile_name,
                                "appServer/error",
                                json!({"message": &message}),
                            )
                            .await;
                        break message;
                    }
                }
            };
            if let Ok(mut app_servers) = state.app_servers.write() {
                app_servers.remove(&profile_name);
            }
            if let Some(manager) = &state.runtime_manager {
                manager.release_bridge(&profile_name);
            }
            state.record_runtime_failure(&profile_name, failure).await;
            state
                .pending_approvals
                .write()
                .await
                .retain(|_, pending| pending.profile_name != profile_name);
            state
                .publish_to_profile_active(&profile_name, "appServer/closed", json!({}))
                .await;
        });
    }

    pub async fn reconcile_after_restart(&self) -> Result<(), StateError> {
        let engagements = self.store.engagements().await?;
        let had_active_engagement = engagements
            .iter()
            .any(|engagement| engagement.status == EngagementStatus::Active);
        let persisted = self
            .store
            .system_state::<DaemonControlStatus>(DAEMON_CONTROL_STATE_KEY)
            .await?;
        let restored = persisted.map(|status| match (status.state, status.reason) {
            (DaemonRunState::Running, None)
            | (DaemonRunState::Paused, Some(DaemonPauseReason::OperatorPause))
            | (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch)) => status,
            (DaemonRunState::Running, Some(reason)) => DaemonControlStatus {
                state: DaemonRunState::Paused,
                reason: Some(reason),
                updated_at: status.updated_at,
                audit: status.audit,
            },
            (DaemonRunState::Paused, None) => DaemonControlStatus {
                state: DaemonRunState::Paused,
                reason: Some(DaemonPauseReason::OperatorPause),
                updated_at: status.updated_at,
                audit: status.audit,
            },
        });
        match restored {
            Some(status) if status.state == DaemonRunState::Paused => {
                self.restore_control(status).await?;
            }
            _ if had_active_engagement => {
                self.set_control(
                    DaemonRunState::Paused,
                    Some(DaemonPauseReason::OperatorPause),
                )
                .await?;
            }
            Some(status) => self.restore_control(status).await?,
            None => {
                self.restore_control(DaemonControlStatus {
                    state: DaemonRunState::Running,
                    reason: None,
                    updated_at: unix_timestamp(),
                    audit: AuditHealthStatus {
                        state: AuditHealthState::Healthy,
                        message: None,
                        updated_at: unix_timestamp(),
                    },
                })
                .await?;
            }
        }
        let _ = self
            .append_system_critical("audit/startupProbe", json!({"outcome": "success"}))
            .await;
        for engagement in engagements {
            if engagement.status != EngagementStatus::Active {
                continue;
            }
            let interrupted_at = unix_timestamp();
            for mut execution in self.store.executions(&engagement.id).await? {
                if matches!(
                    execution.status,
                    codex_riftx_core::ExecutionStatus::Pending
                        | codex_riftx_core::ExecutionStatus::Running
                ) {
                    execution.status = codex_riftx_core::ExecutionStatus::Interrupted;
                    execution.completed_at = Some(interrupted_at);
                    self.store.put_execution(&execution).await?;
                }
            }
            self.store
                .transition_engagement(
                    &engagement.id,
                    EngagementStatus::Interrupted,
                    interrupted_at,
                )
                .await?;
            crate::artifacts::capture_pending(self.clone(), engagement.id).await;
        }
        Ok(())
    }

    pub(crate) async fn event_sender(
        &self,
        engagement_id: &str,
    ) -> broadcast::Sender<EngagementEvent> {
        if let Some(sender) = self.events.read().await.get(engagement_id) {
            return sender.clone();
        }
        let mut events = self.events.write().await;
        events
            .entry(engagement_id.to_string())
            .or_insert_with(|| broadcast::channel(256).0)
            .clone()
    }

    pub(crate) async fn publish_to_profile_active(
        &self,
        profile_name: &str,
        kind: &str,
        data: Value,
    ) {
        let active = self
            .active_turns
            .read()
            .await
            .iter()
            .filter(|(_, turn)| turn.profile_name == profile_name)
            .map(|(engagement_id, _)| engagement_id.clone())
            .collect::<Vec<_>>();
        for engagement_id in active {
            self.publish(&engagement_id, kind, data.clone()).await;
        }
    }

    pub(crate) async fn control_status(&self) -> DaemonControlStatus {
        self.control.read().await.clone()
    }

    pub(crate) async fn set_control(
        &self,
        state: DaemonRunState,
        reason: Option<DaemonPauseReason>,
    ) -> Result<DaemonControlStatus, StateError> {
        let event = match (state, reason) {
            (DaemonRunState::Running, None) => "daemon/resumed",
            (DaemonRunState::Paused, Some(DaemonPauseReason::OperatorPause)) => "daemon/paused",
            (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch)) => {
                "daemon/killSwitchActivated"
            }
            (DaemonRunState::Running, Some(_)) | (DaemonRunState::Paused, None) => {
                "daemon/controlChanged"
            }
        };
        let audit_result = self
            .append_system_critical(
                event,
                json!({
                    "state": state,
                    "reason": reason,
                }),
            )
            .await;
        if state == DaemonRunState::Running && audit_result.is_err() {
            return Err(StateError::AuditUnavailable);
        }
        let _write_permit = self
            .control_write_slot
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| StateError::SystemStateUnavailable)?;
        let status = DaemonControlStatus {
            state,
            reason,
            updated_at: unix_timestamp(),
            audit: self.control.read().await.audit.clone(),
        };
        self.store
            .put_system_state(DAEMON_CONTROL_STATE_KEY, &status)
            .await?;
        *self.control.write().await = status.clone();
        Ok(status)
    }

    async fn restore_control(&self, status: DaemonControlStatus) -> Result<(), StateError> {
        let _write_permit = self
            .control_write_slot
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| StateError::SystemStateUnavailable)?;
        self.store
            .put_system_state(DAEMON_CONTROL_STATE_KEY, &status)
            .await?;
        *self.control.write().await = status;
        Ok(())
    }

    pub(crate) async fn take_pending_approvals(
        &self,
        engagement_id: &str,
    ) -> Vec<PendingApprovalRequest> {
        let mut approvals = self.pending_approvals.write().await;
        approvals
            .extract_if(|_, pending| pending.engagement_id == engagement_id)
            .map(|(_, pending)| pending)
            .collect()
    }

    pub(crate) async fn complete_task(&self, engagement_id: &str, turn_id: &str, data: &Value) {
        let Ok(Some(mut task)) = self.store.task_for_turn(engagement_id, turn_id).await else {
            return;
        };
        let status = data
            .pointer("/turn/status")
            .and_then(Value::as_str)
            .unwrap_or("failed");
        task.status = match status {
            "completed" => TaskStatus::Completed,
            "interrupted" => TaskStatus::Interrupted,
            "failed" | "inProgress" => TaskStatus::Failed,
            _ => TaskStatus::Failed,
        };
        task.error = data
            .pointer("/turn/error/message")
            .and_then(Value::as_str)
            .map(str::to_string);
        let _ = self.store.put_task(&task).await;
    }
}

pub(crate) fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}
use crate::execution_events::ActiveExecution;
use crate::execution_events::ExecutionKey;
