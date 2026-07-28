use super::DesktopError;
use super::DesktopState;
use codex_riftx_ipc::EngagementEvent;
use codex_riftx_ipc::LocalIpcClient;
use serde::Serialize;
use std::collections::HashMap;
use std::collections::HashSet;
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
    activity: Mutex<ActivityBook>,
    streams: Mutex<StreamStatusBook>,
}

impl SubscriptionRegistry {
    pub(super) fn sync_active(
        &self,
        app: &tauri::AppHandle,
        client: LocalIpcClient,
        engagement_ids: impl IntoIterator<Item = String>,
    ) -> Result<(), DesktopError> {
        let engagement_ids = engagement_ids.into_iter().collect::<HashSet<_>>();
        let mut tasks = self.tasks.lock().map_err(|_| {
            DesktopError::new("subscription_state", "subscription state is poisoned")
        })?;
        tasks.retain(|engagement_id, task| {
            if engagement_ids.contains(engagement_id) {
                true
            } else {
                task.abort();
                false
            }
        });
        for engagement_id in &engagement_ids {
            Self::spawn_missing(&mut tasks, app, &client, engagement_id);
        }
        drop(tasks);

        let summary = self.update_activity(|activity| {
            activity.sync(&engagement_ids);
        })?;
        self.update_streams(|streams| streams.sync(&engagement_ids))?;
        crate::background::sync_task_activity(app, summary);
        Ok(())
    }

    pub(super) fn ensure_active(
        &self,
        app: &tauri::AppHandle,
        client: LocalIpcClient,
        engagement_id: String,
    ) -> Result<(), DesktopError> {
        let mut tasks = self.tasks.lock().map_err(|_| {
            DesktopError::new("subscription_state", "subscription state is poisoned")
        })?;
        Self::spawn_missing(&mut tasks, app, &client, &engagement_id);
        drop(tasks);

        let summary = self.update_activity(|activity| activity.ensure(engagement_id.clone()))?;
        self.update_streams(|streams| streams.ensure(engagement_id))?;
        crate::background::sync_task_activity(app, summary);
        Ok(())
    }

    fn spawn_missing(
        tasks: &mut HashMap<String, JoinHandle<()>>,
        app: &tauri::AppHandle,
        client: &LocalIpcClient,
        engagement_id: &str,
    ) {
        if tasks.contains_key(engagement_id) {
            return;
        }
        let subscription_id = engagement_id.to_string();
        let stream_app = app.clone();
        let stream_client = client.clone();
        let task = tauri::async_runtime::spawn(async move {
            stream_engagement_events(stream_app, stream_client, subscription_id).await;
        });
        tasks.insert(engagement_id.to_string(), task);
    }

    fn record_event(
        &self,
        app: &tauri::AppHandle,
        event: &EngagementEvent,
    ) -> Result<(), DesktopError> {
        let summary = self.update_activity(|activity| activity.record(event))?;
        crate::background::sync_task_activity(app, summary);
        Ok(())
    }

    fn record_disconnected(
        &self,
        app: &tauri::AppHandle,
        engagement_id: &str,
    ) -> Result<(), DesktopError> {
        let summary = self.update_activity(|activity| activity.disconnected(engagement_id))?;
        crate::background::sync_task_activity(app, summary);
        Ok(())
    }

    fn update_activity(
        &self,
        update: impl FnOnce(&mut ActivityBook),
    ) -> Result<TaskActivitySummary, DesktopError> {
        let mut activity = self.activity.lock().map_err(|_| {
            DesktopError::new("subscription_state", "task activity state is poisoned")
        })?;
        update(&mut activity);
        Ok(activity.summary())
    }

    fn stream_status(&self, engagement_id: &str) -> Result<DesktopStreamStatus, DesktopError> {
        let streams = self.streams.lock().map_err(|_| {
            DesktopError::new("subscription_state", "stream status state is poisoned")
        })?;
        Ok(streams.status(engagement_id))
    }

    fn record_stream_status(
        &self,
        app: &tauri::AppHandle,
        status: DesktopStreamStatus,
    ) -> Result<(), DesktopError> {
        let connected_engagement =
            (status.state == "connected").then(|| status.engagement_id.clone());
        self.update_streams(|streams| streams.record(status))?;
        if let Some(engagement_id) = connected_engagement {
            let summary = self.update_activity(|activity| activity.connected(&engagement_id))?;
            crate::background::sync_task_activity(app, summary);
        }
        Ok(())
    }

    fn update_streams(
        &self,
        update: impl FnOnce(&mut StreamStatusBook),
    ) -> Result<(), DesktopError> {
        let mut streams = self.streams.lock().map_err(|_| {
            DesktopError::new("subscription_state", "stream status state is poisoned")
        })?;
        update(&mut streams);
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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct TaskActivitySummary {
    pub(crate) ready: usize,
    pub(crate) running: usize,
    pub(crate) waiting: usize,
    pub(crate) risk: usize,
}

#[derive(Debug, Default)]
struct ActivityBook {
    tasks: HashMap<String, TaskActivity>,
}

impl ActivityBook {
    fn ensure(&mut self, engagement_id: String) {
        self.tasks.entry(engagement_id).or_default();
    }

    fn sync(&mut self, engagement_ids: &HashSet<String>) {
        self.tasks
            .retain(|engagement_id, _| engagement_ids.contains(engagement_id));
        for engagement_id in engagement_ids {
            self.tasks.entry(engagement_id.clone()).or_default();
        }
    }

    fn record(&mut self, event: &EngagementEvent) {
        let Some(activity) = self.tasks.get_mut(&event.engagement_id) else {
            return;
        };
        match event.kind.as_str() {
            "engagementActivated" => activity.risk = false,
            "turnStarted" => {
                activity.turn_running = true;
                activity.risk = false;
            }
            "approval/command" => {
                if let Some(approval_id) = event.data.get("approvalId").and_then(|id| id.as_str()) {
                    activity.approvals.insert(approval_id.to_string());
                }
            }
            "approvalDecided" => {
                if let Some(approval_id) = event.data.get("approvalId").and_then(|id| id.as_str()) {
                    activity.approvals.remove(approval_id);
                }
            }
            "turn/completed" => {
                activity.turn_running = false;
                activity.approvals.clear();
            }
            "engagementInterrupted" | "appServer/closed" | "execution/failed" => {
                activity.turn_running = false;
                activity.approvals.clear();
                activity.risk = true;
            }
            _ => {}
        }
    }

    fn disconnected(&mut self, engagement_id: &str) {
        if let Some(activity) = self.tasks.get_mut(engagement_id) {
            activity.stream_disconnected = true;
        }
    }

    fn connected(&mut self, engagement_id: &str) {
        if let Some(activity) = self.tasks.get_mut(engagement_id) {
            activity.stream_disconnected = false;
        }
    }

    fn summary(&self) -> TaskActivitySummary {
        let mut summary = TaskActivitySummary::default();
        for activity in self.tasks.values() {
            if activity.risk || activity.stream_disconnected {
                summary.risk += 1;
            } else if !activity.approvals.is_empty() {
                summary.waiting += 1;
            } else if activity.turn_running {
                summary.running += 1;
            } else {
                summary.ready += 1;
            }
        }
        summary
    }
}

#[derive(Debug, Default)]
struct TaskActivity {
    turn_running: bool,
    approvals: HashSet<String>,
    risk: bool,
    stream_disconnected: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopStreamStatus {
    engagement_id: String,
    state: &'static str,
    message: Option<String>,
}

#[derive(Debug, Default)]
struct StreamStatusBook {
    statuses: HashMap<String, DesktopStreamStatus>,
}

impl StreamStatusBook {
    fn sync(&mut self, engagement_ids: &HashSet<String>) {
        self.statuses
            .retain(|engagement_id, _| engagement_ids.contains(engagement_id));
        for engagement_id in engagement_ids {
            self.ensure(engagement_id.clone());
        }
    }

    fn ensure(&mut self, engagement_id: String) {
        self.statuses
            .entry(engagement_id.clone())
            .or_insert_with(|| DesktopStreamStatus {
                engagement_id,
                state: "connecting",
                message: None,
            });
    }

    fn record(&mut self, status: DesktopStreamStatus) {
        self.statuses.insert(status.engagement_id.clone(), status);
    }

    fn status(&self, engagement_id: &str) -> DesktopStreamStatus {
        self.statuses
            .get(engagement_id)
            .cloned()
            .unwrap_or_else(|| DesktopStreamStatus {
                engagement_id: engagement_id.to_string(),
                state: "disconnected",
                message: None,
            })
    }
}

#[tauri::command]
pub(crate) fn engagement_stream_status(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<DesktopStreamStatus, DesktopError> {
    super::validate_engagement_id(&engagement_id)?;
    state.subscriptions.stream_status(&engagement_id)
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
                                    let _ = app
                                        .state::<DesktopState>()
                                        .subscriptions
                                        .record_event(&app, &event);
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
        let _ = app
            .state::<DesktopState>()
            .subscriptions
            .record_disconnected(&app, &engagement_id);
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
    let status = DesktopStreamStatus {
        engagement_id: engagement_id.to_string(),
        state,
        message: message.map(str::to_string),
    };
    let _ = app
        .state::<DesktopState>()
        .subscriptions
        .record_stream_status(app, status.clone());
    let _ = app.emit(ENGAGEMENT_STREAM_NAME, status);
}

#[cfg(test)]
#[path = "event_stream_tests.rs"]
mod tests;
