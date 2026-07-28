use crate::bridge::DesktopError;
use codex_riftx_ipc::EngagementEvent;
use serde::Serialize;
use std::collections::VecDeque;
use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_notification::PermissionState;

const MAIN_WINDOW: &str = "main";
const RECENT_NOTIFICATION_LIMIT: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
struct NotificationKey {
    engagement_id: String,
    kind: String,
    timestamp: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct NotificationCopy {
    title: &'static str,
    body: &'static str,
}

#[derive(Default)]
pub(crate) struct NotificationManager {
    recent: Mutex<VecDeque<NotificationKey>>,
}

impl NotificationManager {
    pub(crate) fn notify(&self, app: &tauri::AppHandle, event: &EngagementEvent) {
        let Some(copy) = notification_copy(&event.kind) else {
            return;
        };
        if !operator_is_away(app)
            || !matches!(
                app.notification().permission_state(),
                Ok(PermissionState::Granted)
            )
            || !self.claim(event)
        {
            return;
        }
        let _ = app
            .notification()
            .builder()
            .title(copy.title)
            .body(copy.body)
            .show();
    }

    fn claim(&self, event: &EngagementEvent) -> bool {
        let Ok(mut recent) = self.recent.lock() else {
            return false;
        };
        let key = NotificationKey {
            engagement_id: event.engagement_id.clone(),
            kind: event.kind.clone(),
            timestamp: event.timestamp,
        };
        if recent.contains(&key) {
            return false;
        }
        recent.push_back(key);
        if recent.len() > RECENT_NOTIFICATION_LIMIT {
            recent.pop_front();
        }
        true
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) enum DesktopNotificationPermission {
    Granted,
    Denied,
    Prompt,
    PromptWithRationale,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(crate) struct NotificationSettings {
    permission: DesktopNotificationPermission,
}

#[tauri::command]
pub(crate) fn notification_settings(
    app: tauri::AppHandle,
) -> Result<NotificationSettings, DesktopError> {
    let permission = app
        .notification()
        .permission_state()
        .map_err(notification_error)?;
    Ok(NotificationSettings {
        permission: permission.into(),
    })
}

#[tauri::command]
pub(crate) fn request_notification_permission(
    app: tauri::AppHandle,
) -> Result<NotificationSettings, DesktopError> {
    let permission = app
        .notification()
        .request_permission()
        .map_err(notification_error)?;
    Ok(NotificationSettings {
        permission: permission.into(),
    })
}

impl From<PermissionState> for DesktopNotificationPermission {
    fn from(value: PermissionState) -> Self {
        match value {
            PermissionState::Granted => Self::Granted,
            PermissionState::Denied => Self::Denied,
            PermissionState::Prompt => Self::Prompt,
            PermissionState::PromptWithRationale => Self::PromptWithRationale,
        }
    }
}

fn notification_copy(kind: &str) -> Option<NotificationCopy> {
    match kind {
        "approval/command" => Some(NotificationCopy {
            title: "RiftX approval required",
            body: "An active task is waiting for a command decision.",
        }),
        "turn/completed" => Some(NotificationCopy {
            title: "RiftX task update",
            body: "The active task finished its current turn.",
        }),
        "engagementInterrupted" => Some(NotificationCopy {
            title: "RiftX execution paused",
            body: "Execution was interrupted. Open RiftX to review the task.",
        }),
        "appServer/closed" => Some(NotificationCopy {
            title: "RiftX runtime disconnected",
            body: "The local Agent Runtime disconnected. Open RiftX to review its state.",
        }),
        _ => None,
    }
}

fn operator_is_away(app: &tauri::AppHandle) -> bool {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return true;
    };
    let visible = window.is_visible().unwrap_or(false);
    let focused = window.is_focused().unwrap_or(false);
    !visible || !focused
}

fn notification_error(error: impl std::fmt::Display) -> DesktopError {
    DesktopError::new("notification_unavailable", error.to_string())
}

#[cfg(test)]
#[path = "notifications_tests.rs"]
mod tests;
