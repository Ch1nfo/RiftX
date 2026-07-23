use codex_riftx_app_server_adapter::PendingCommandApproval;
use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxAppServerRequestHandle;
use codex_riftx_core::AuditRecord;
use codex_riftx_core::AuditWriter;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateError;
use codex_riftx_core::StateStore;
use codex_riftx_core::TaskStatus;
use codex_riftx_skills::SkillCatalog;
use codex_riftx_tools::ToolInventory;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use tokio::sync::RwLock;
use tokio::sync::Semaphore;
use tokio::sync::broadcast;

#[derive(Clone)]
pub struct GatewayState {
    pub config: Arc<RiftxConfig>,
    pub store: StateStore,
    pub skills: Arc<SkillCatalog>,
    pub tools: Arc<ToolInventory>,
    pub(crate) audit: AuditWriter,
    pub(crate) app_server: Option<RiftxAppServerRequestHandle>,
    pub(crate) events: Arc<RwLock<HashMap<String, broadcast::Sender<GatewayEvent>>>>,
    pub(crate) thread_engagements: Arc<RwLock<HashMap<String, String>>>,
    pub(crate) active_turns: Arc<RwLock<HashMap<String, ActiveTurn>>>,
    pub(crate) agent_threads: Arc<RwLock<HashMap<String, String>>>,
    pub(crate) pending_approvals: Arc<RwLock<HashMap<String, PendingApproval>>>,
    pub(crate) turn_slot: Arc<Semaphore>,
}

#[derive(Clone)]
pub(crate) struct ActiveTurn {
    pub(crate) thread_id: String,
    pub(crate) turn_id: String,
}

#[derive(Clone)]
pub(crate) struct PendingApproval {
    pub(crate) engagement_id: String,
    pub(crate) policy_revision: String,
    pub(crate) kind: PendingApprovalKind,
}

#[derive(Clone)]
pub(crate) enum PendingApprovalKind {
    Command(PendingCommandApproval),
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GatewayEvent {
    pub(crate) engagement_id: String,
    pub(crate) kind: String,
    pub(crate) timestamp: i64,
    pub(crate) data: Value,
}

impl GatewayState {
    pub fn new(
        config: RiftxConfig,
        store: StateStore,
        skills: SkillCatalog,
        tools: ToolInventory,
    ) -> Self {
        let audit = AuditWriter::new(&config.audit);
        Self {
            config: Arc::new(config),
            store,
            skills: Arc::new(skills),
            tools: Arc::new(tools),
            audit,
            app_server: None,
            events: Arc::new(RwLock::new(HashMap::new())),
            thread_engagements: Arc::new(RwLock::new(HashMap::new())),
            active_turns: Arc::new(RwLock::new(HashMap::new())),
            agent_threads: Arc::new(RwLock::new(HashMap::new())),
            pending_approvals: Arc::new(RwLock::new(HashMap::new())),
            turn_slot: Arc::new(Semaphore::new(1)),
        }
    }

    pub fn with_app_server(mut self, app_server: RiftxAppServerRequestHandle) -> Self {
        self.app_server = Some(app_server);
        self
    }

    pub fn spawn_app_server_event_pump(&self, mut adapter: RiftxAppServerAdapter) {
        let state = self.clone();
        tokio::spawn(async move {
            loop {
                match adapter.next_event().await {
                    Ok(Some(event)) => crate::app_events::process(&state, event).await,
                    Ok(None) => break,
                    Err(error) => {
                        state
                            .publish_to_active(
                                "appServer/error",
                                json!({"message": error.to_string()}),
                            )
                            .await;
                        break;
                    }
                }
            }
            state.publish_to_active("appServer/closed", json!({})).await;
        });
    }

    pub async fn reconcile_after_restart(&self) -> Result<(), StateError> {
        for engagement in self.store.engagements().await? {
            if engagement.status != EngagementStatus::Active {
                continue;
            }
            self.store
                .transition_engagement(
                    &engagement.id,
                    EngagementStatus::Interrupted,
                    unix_timestamp(),
                )
                .await?;
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
                turn_id: first_string(&data, &["/turnId", "/payload/turnId", "/payload/turn/id"]),
                tool_call_id: first_string(
                    &data,
                    &["/toolCallId", "/payload/toolCallId", "/payload/callId"],
                ),
                mode: Some(engagement.mode),
                policy_revision: Some(engagement.policy_revision),
                outcome: event_outcome(kind, &data),
            };
            let _ = self.audit.append(&record).await;
        }
        let sender = self.event_sender(engagement_id).await;
        let _ = sender.send(GatewayEvent {
            engagement_id: engagement_id.to_string(),
            kind: kind.to_string(),
            timestamp: unix_timestamp(),
            data,
        });
    }

    pub(crate) async fn event_sender(
        &self,
        engagement_id: &str,
    ) -> broadcast::Sender<GatewayEvent> {
        if let Some(sender) = self.events.read().await.get(engagement_id) {
            return sender.clone();
        }
        let mut events = self.events.write().await;
        events
            .entry(engagement_id.to_string())
            .or_insert_with(|| broadcast::channel(256).0)
            .clone()
    }

    pub(crate) async fn publish_to_active(&self, kind: &str, data: Value) {
        let active = self
            .active_turns
            .read()
            .await
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        for engagement_id in active {
            self.publish(&engagement_id, kind, data.clone()).await;
        }
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
