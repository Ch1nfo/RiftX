use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxAppServerRequestHandle;
use codex_riftx_artifacts::ArtifactStore;
use codex_riftx_core::AuditRecord;
use codex_riftx_core::AuditWriter;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateError;
use codex_riftx_core::StateStore;
use codex_riftx_core::TaskStatus;
use codex_riftx_credentials::AssessmentCredentialStore;
use codex_riftx_credentials::AssessmentSecretProvider;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use codex_riftx_ipc::EngagementEvent;
use codex_riftx_ipc::PendingApproval;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_tools::ToolInventory;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use tokio::sync::RwLock;
use tokio::sync::Semaphore;
use tokio::sync::broadcast;
use tokio_util::sync::CancellationToken;

const DAEMON_CONTROL_STATE_KEY: &str = "daemonControl";

#[derive(Clone)]
pub struct GatewayState {
    pub config: Arc<RiftxConfig>,
    pub store: StateStore,
    pub skills: Arc<SkillCatalog>,
    pub tools: Arc<ToolInventory>,
    pub(crate) artifact_store: Arc<ArtifactStore>,
    pub(crate) audit: AuditWriter,
    pub(crate) app_servers: Arc<HashMap<String, RiftxAppServerRequestHandle>>,
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
    control_write_slot: Arc<Semaphore>,
    pub(crate) control: Arc<RwLock<DaemonControlStatus>>,
}

#[derive(Clone)]
pub(crate) struct ActiveTurn {
    pub(crate) profile_name: String,
    pub(crate) thread_id: String,
    pub(crate) turn_id: String,
}

#[derive(Clone)]
pub(crate) struct PendingApprovalRequest {
    pub(crate) profile_name: String,
    pub(crate) engagement_id: String,
    pub(crate) view: PendingApproval,
    pub(crate) kind: PendingApprovalKind,
}

#[derive(Clone)]
pub(crate) enum PendingApprovalKind {
    Command(PendingCommandApproval),
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
        let artifact_store = ArtifactStore::new(&config.artifacts);
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
            app_servers: Arc::new(HashMap::new()),
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
        mut self,
        profile_name: String,
        app_server: RiftxAppServerRequestHandle,
    ) -> Self {
        Arc::make_mut(&mut self.app_servers).insert(profile_name, app_server);
        self
    }

    pub(crate) fn app_server(&self, profile_name: &str) -> Option<&RiftxAppServerRequestHandle> {
        self.app_servers.get(profile_name)
    }

    pub fn spawn_app_server_event_pump(
        &self,
        profile_name: String,
        mut adapter: RiftxAppServerAdapter,
    ) {
        let state = self.clone();
        tokio::spawn(async move {
            loop {
                match adapter.next_event().await {
                    Ok(Some(event)) => {
                        crate::app_events::process(&state, &profile_name, event).await
                    }
                    Ok(None) => break,
                    Err(error) => {
                        state
                            .publish_to_profile_active(
                                &profile_name,
                                "appServer/error",
                                json!({"message": error.to_string()}),
                            )
                            .await;
                        break;
                    }
                }
            }
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
            },
            (DaemonRunState::Paused, None) => DaemonControlStatus {
                state: DaemonRunState::Paused,
                reason: Some(DaemonPauseReason::OperatorPause),
                updated_at: status.updated_at,
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
                })
                .await?;
            }
        }
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

    pub(crate) async fn publish(&self, engagement_id: &str, kind: &str, data: Value) {
        if let Ok(engagement) = self.store.engagement(engagement_id).await {
            let record = AuditRecord {
                timestamp: unix_timestamp(),
                event: kind.to_string(),
                engagement_id: engagement_id.to_string(),
                thread_id: first_string(&data, &["/threadId", "/payload/threadId"])
                    .or(engagement.thread_id),
                turn_id: first_string(
                    &data,
                    &[
                        "/turnId",
                        "/execution/turnId",
                        "/payload/turnId",
                        "/payload/turn/id",
                    ],
                ),
                tool_call_id: first_string(
                    &data,
                    &[
                        "/toolCallId",
                        "/callId",
                        "/payload/toolCallId",
                        "/payload/callId",
                        "/useId",
                        "/usage/id",
                        "/id",
                    ],
                ),
                mode: Some(engagement.mode),
                policy_revision: Some(engagement.policy_revision),
                outcome: event_outcome(kind, &data),
                details: (kind.starts_with("execution/")
                    || kind.starts_with("credential/use")
                    || kind.starts_with("tool/credential")
                    || kind.starts_with("artifact/")
                    || kind == "engagement/modeChanged")
                    .then(|| data.clone()),
            };
            let _ = self.audit.append(&record).await;
        }
        let sender = self.event_sender(engagement_id).await;
        let _ = sender.send(EngagementEvent {
            engagement_id: engagement_id.to_string(),
            kind: kind.to_string(),
            timestamp: unix_timestamp(),
            data,
        });
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
        let status = DaemonControlStatus {
            state,
            reason,
            updated_at: unix_timestamp(),
        };
        let _write_permit = self
            .control_write_slot
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| StateError::SystemStateUnavailable)?;
        self.store
            .put_system_state(DAEMON_CONTROL_STATE_KEY, &status)
            .await?;
        *self.control.write().await = status.clone();
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
        let _ = self
            .audit
            .append(&AuditRecord {
                timestamp: status.updated_at,
                event: event.to_string(),
                engagement_id: "system".to_string(),
                thread_id: None,
                turn_id: None,
                tool_call_id: None,
                mode: None,
                policy_revision: None,
                outcome: Some("success".to_string()),
                details: Some(json!({
                    "state": status.state,
                    "reason": status.reason,
                })),
            })
            .await;
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

fn first_string(data: &Value, pointers: &[&str]) -> Option<String> {
    pointers
        .iter()
        .find_map(|pointer| data.pointer(pointer).and_then(Value::as_str))
        .map(str::to_string)
}

fn event_outcome(kind: &str, data: &Value) -> Option<String> {
    first_string(data, &["/outcome", "/status", "/decision"]).or_else(|| {
        if kind.ends_with("/completed") || kind.ends_with("Completed") {
            Some("success".to_string())
        } else if kind.ends_with("/failed") || kind.ends_with("/rejected") {
            Some("failure".to_string())
        } else {
            None
        }
    })
}

pub(crate) fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}
use crate::execution_events::ActiveExecution;
use crate::execution_events::ExecutionKey;
