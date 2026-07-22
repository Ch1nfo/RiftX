use codex_riftx_app_server_adapter::RiftxAppServerAdapter;
use codex_riftx_app_server_adapter::RiftxAppServerRequestHandle;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::RiftxConfig;
use codex_riftx_core::StateError;
use codex_riftx_core::StateStore;
use codex_riftx_core::TaskStatus;
use codex_riftx_manager_client::ManagerClient;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
use tokio::sync::RwLock;
use tokio::sync::Semaphore;
use tokio::sync::broadcast;

#[derive(Clone)]
pub struct GatewayState {
    pub config: Arc<RiftxConfig>,
    pub store: StateStore,
    pub manager: ManagerClient,
    pub(crate) app_server: Option<RiftxAppServerRequestHandle>,
    pub(crate) events: Arc<RwLock<HashMap<String, broadcast::Sender<GatewayEvent>>>>,
    pub(crate) thread_engagements: Arc<RwLock<HashMap<String, String>>>,
    pub(crate) active_turns: Arc<RwLock<HashMap<String, ActiveTurn>>>,
    pub(crate) turn_slot: Arc<Semaphore>,
}

#[derive(Clone)]
pub(crate) struct ActiveTurn {
    pub(crate) thread_id: String,
    pub(crate) turn_id: String,
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
    pub fn new(config: RiftxConfig, store: StateStore, manager: ManagerClient) -> Self {
        Self {
            config: Arc::new(config),
            store,
            manager,
            app_server: None,
            events: Arc::new(RwLock::new(HashMap::new())),
            thread_engagements: Arc::new(RwLock::new(HashMap::new())),
            active_turns: Arc::new(RwLock::new(HashMap::new())),
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
            while let Some(event) = next_app_server_event(&state, &mut adapter).await {
                let Some(thread_id) = event.thread_id.as_deref() else {
                    state.publish_to_active(&event.kind, event.data).await;
                    continue;
                };
                let engagement_id = state
                    .thread_engagements
                    .read()
                    .await
                    .get(thread_id)
                    .cloned();
                let Some(engagement_id) = engagement_id else {
                    continue;
                };
                if event.kind == "turn/completed"
                    && let Some(turn_id) = event.turn_id.as_deref()
                {
                    state
                        .complete_task(&engagement_id, turn_id, &event.data)
                        .await;
                    state.active_turns.write().await.remove(&engagement_id);
                }
                state
                    .publish(
                        &engagement_id,
                        &event.kind,
                        json!({
                            "requestId": event.request_id,
                            "turnId": event.turn_id,
                            "payload": event.data,
                        }),
                    )
                    .await;
            }
            state.publish_to_active("appServer/closed", json!({})).await;
        });
    }

    pub fn spawn_manager_event_pump(&self) {
        let state = self.clone();
        tokio::spawn(async move {
            let mut cursor = None;
            loop {
                match state.manager.events(cursor.as_deref()).await {
                    Ok(response) => {
                        cursor = response.next_cursor;
                        let engagements = state.store.engagements().await.unwrap_or_default();
                        for event in response.events {
                            let Some(engagement) = engagements.iter().find(|engagement| {
                                engagement.sandbox_id.as_deref() == Some(&event.sandbox_id)
                            }) else {
                                continue;
                            };
                            state
                                .publish(
                                    &engagement.id,
                                    &format!("manager/{}", event.kind),
                                    json!({
                                        "sandboxId": event.sandbox_id,
                                        "cursor": event.cursor,
                                        "detail": event.detail,
                                    }),
                                )
                                .await;
                        }
                    }
                    Err(error) => {
                        state
                            .publish_to_active(
                                "manager/error",
                                json!({"message": error.to_string()}),
                            )
                            .await;
                    }
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
    }

    pub async fn reconcile_after_restart(&self) -> Result<(), StateError> {
        for engagement in self.store.engagements().await? {
            if engagement.status != EngagementStatus::Active {
                continue;
            }
            if let Some(sandbox_id) = engagement.sandbox_id.as_deref() {
                let _ = self.manager.kill(sandbox_id).await;
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

    async fn publish_to_active(&self, kind: &str, data: Value) {
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

    async fn complete_task(&self, engagement_id: &str, turn_id: &str, data: &Value) {
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

async fn next_app_server_event(
    state: &GatewayState,
    adapter: &mut RiftxAppServerAdapter,
) -> Option<codex_riftx_app_server_adapter::RiftxEventEnvelope> {
    let event = match adapter.next_event().await {
        Ok(Some(event)) => event,
        Ok(None) => return None,
        Err(error) => {
            state
                .publish_to_active("appServer/error", json!({"message": error.to_string()}))
                .await;
            return None;
        }
    };
    match event.envelope() {
        Ok(event) => Some(event),
        Err(error) => {
            state
                .publish_to_active("appServer/error", json!({"message": error.to_string()}))
                .await;
            None
        }
    }
}

pub(crate) fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64)
}
