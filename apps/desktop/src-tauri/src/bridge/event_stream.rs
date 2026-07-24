use super::DesktopError;
use super::DesktopState;
use super::validate_engagement_id;
use codex_riftx_ipc::EngagementEvent;
use codex_riftx_ipc::LocalIpcClient;
use serde::Serialize;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;
use tauri::Emitter;
use tauri::Manager;
use tauri::async_runtime::JoinHandle;

const ENGAGEMENT_EVENT_NAME: &str = "riftx://engagement-event";
const ENGAGEMENT_STREAM_NAME: &str = "riftx://engagement-stream";

#[derive(Default)]
pub(super) struct SubscriptionRegistry {
    tasks: Mutex<HashMap<String, JoinHandle<()>>>,
}

impl SubscriptionRegistry {
    fn insert(&self, engagement_id: String, task: JoinHandle<()>) -> Result<bool, DesktopError> {
        let mut tasks = self.tasks.lock().map_err(|_| {
            DesktopError::new("subscription_state", "subscription state is poisoned")
        })?;
        if tasks.contains_key(&engagement_id) {
            task.abort();
            return Ok(false);
        }
        tasks.insert(engagement_id, task);
        Ok(true)
    }

    fn remove(&self, engagement_id: &str) -> Result<(), DesktopError> {
        let mut tasks = self.tasks.lock().map_err(|_| {
            DesktopError::new("subscription_state", "subscription state is poisoned")
        })?;
        if let Some(task) = tasks.remove(engagement_id) {
            task.abort();
        }
        Ok(())
    }
}

impl Drop for SubscriptionRegistry {
    fn drop(&mut self) {
        if let Ok(tasks) = self.tasks.get_mut() {
            for (_, task) in tasks.drain() {
                task.abort();
            }
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopStreamStatus {
    engagement_id: String,
    state: &'static str,
    message: Option<String>,
}

#[tauri::command]
pub(crate) async fn subscribe_engagement(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<(), DesktopError> {
    validate_engagement_id(&engagement_id)?;
    let client = state.client()?;
    let subscription_id = engagement_id.clone();
    let task = tauri::async_runtime::spawn(async move {
        stream_engagement_events(app, client, subscription_id).await;
    });
    state.subscriptions.insert(engagement_id, task)?;
    Ok(())
}

#[tauri::command]
pub(crate) fn unsubscribe_engagement(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<(), DesktopError> {
    validate_engagement_id(&engagement_id)?;
    state.subscriptions.remove(&engagement_id)
}

async fn stream_engagement_events(
    app: tauri::AppHandle,
    client: LocalIpcClient,
    engagement_id: String,
) {
    let mut retry_delay = Duration::from_millis(500);
    loop {
        emit_stream_status(&app, &engagement_id, "connecting", None);
        let path = format!("/v1/engagements/{engagement_id}/events");
        let result = client.get(&path).await;
        match result {
            Ok(response) if response.status().is_success() => {
                emit_stream_status(&app, &engagement_id, "connected", None);
                retry_delay = Duration::from_millis(500);
                let mut stream = response.into_sse_stream();
                loop {
                    match stream.next_event().await {
                        Ok(Some(frame)) => {
                            if frame.data.is_empty() {
                                continue;
                            }
                            match serde_json::from_str::<EngagementEvent>(&frame.data) {
                                Ok(event) if event.engagement_id == engagement_id => {
                                    app.state::<DesktopState>()
                                        .notifications
                                        .notify(&app, &event);
                                    let _ = app.emit(ENGAGEMENT_EVENT_NAME, event);
                                }
                                Ok(_) => {
                                    emit_stream_status(
                                        &app,
                                        &engagement_id,
                                        "disconnected",
                                        Some("daemon sent an event for another engagement"),
                                    );
                                    break;
                                }
                                Err(error) => {
                                    emit_stream_status(
                                        &app,
                                        &engagement_id,
                                        "disconnected",
                                        Some(&format!("invalid daemon event: {error}")),
                                    );
                                    break;
                                }
                            }
                        }
                        Ok(None) => {
                            emit_stream_status(
                                &app,
                                &engagement_id,
                                "disconnected",
                                Some("daemon event stream closed"),
                            );
                            break;
                        }
                        Err(error) => {
                            emit_stream_status(
                                &app,
                                &engagement_id,
                                "disconnected",
                                Some(&error.to_string()),
                            );
                            break;
                        }
                    }
                }
            }
            Ok(response) => {
                emit_stream_status(
                    &app,
                    &engagement_id,
                    "disconnected",
                    Some(&format!(
                        "daemon rejected event stream with HTTP {}",
                        response.status()
                    )),
                );
            }
            Err(error) => {
                emit_stream_status(
                    &app,
                    &engagement_id,
                    "disconnected",
                    Some(&error.to_string()),
                );
            }
        }
        tokio::time::sleep(retry_delay).await;
        retry_delay = (retry_delay * 2).min(Duration::from_secs(5));
    }
}

fn emit_stream_status(
    app: &tauri::AppHandle,
    engagement_id: &str,
    state: &'static str,
    message: Option<&str>,
) {
    let _ = app.emit(
        ENGAGEMENT_STREAM_NAME,
        DesktopStreamStatus {
            engagement_id: engagement_id.to_string(),
            state,
            message: message.map(str::to_string),
        },
    );
}
