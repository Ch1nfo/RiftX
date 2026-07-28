use crate::bridge::event_stream::TaskActivitySummary;
use codex_riftx_ipc::DaemonControlStatus;
use codex_riftx_ipc::DaemonPauseReason;
use codex_riftx_ipc::DaemonRunState;
use tauri::Emitter;
use tauri::Manager;
use tauri::menu::Menu;
use tauri::menu::MenuItem;
use tauri::menu::PredefinedMenuItem;
use tauri::tray::MouseButton;
use tauri::tray::MouseButtonState;
use tauri::tray::TrayIconBuilder;
use tauri::tray::TrayIconEvent;

const MAIN_WINDOW: &str = "main";
const OPEN_MENU: &str = "open";
const STATUS_MENU: &str = "runtime-status";
const TASK_STATUS_MENU: &str = "task-status";
const PAUSE_MENU: &str = "pause";
const RESUME_MENU: &str = "resume";
const KILL_MENU: &str = "kill";
const QUIT_MENU: &str = "quit";
pub(crate) const RUNTIME_STATUS_EVENT: &str = "riftx://runtime-status";
pub(crate) const RUNTIME_ERROR_EVENT: &str = "riftx://runtime-error";

#[derive(Clone)]
struct RuntimeMenu {
    status: MenuItem<tauri::Wry>,
    task_status: MenuItem<tauri::Wry>,
    pause: MenuItem<tauri::Wry>,
    resume: MenuItem<tauri::Wry>,
    kill: MenuItem<tauri::Wry>,
}

impl RuntimeMenu {
    fn sync(&self, runtime: &DaemonControlStatus) {
        let (label, running, kill_switch) = match (runtime.state, runtime.reason) {
            (DaemonRunState::Running, _) => ("Runtime: Running", true, false),
            (DaemonRunState::Paused, Some(DaemonPauseReason::KillSwitch)) => {
                ("Runtime: Kill Switch", false, true)
            }
            (DaemonRunState::Paused, _) => ("Runtime: Paused", false, false),
        };
        let _ = self.status.set_text(label);
        let _ = self.pause.set_enabled(running);
        let _ = self.resume.set_enabled(!running);
        let _ = self.kill.set_enabled(!kill_switch);
    }

    fn busy(&self) {
        let _ = self.status.set_text("Runtime: Updating...");
        let _ = self.pause.set_enabled(false);
        let _ = self.resume.set_enabled(false);
        let _ = self.kill.set_enabled(false);
    }

    fn unavailable(&self) {
        let _ = self.status.set_text("Runtime: Unavailable");
        let _ = self.pause.set_enabled(false);
        let _ = self.resume.set_enabled(false);
        let _ = self.kill.set_enabled(false);
    }

    fn sync_tasks(&self, summary: TaskActivitySummary) {
        let _ = self.task_status.set_text(task_status_label(summary));
    }
}

fn task_status_label(summary: TaskActivitySummary) -> String {
    match task_attention(summary) {
        TaskAttention::Risk(count) => format!("Tasks: Risk ({count})"),
        TaskAttention::Waiting(count) => format!("Tasks: Waiting approval ({count})"),
        TaskAttention::Running(count) => format!("Tasks: Running ({count})"),
        TaskAttention::Ready(count) => format!("Tasks: Ready ({count})"),
        TaskAttention::None => "Tasks: None".to_string(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TaskAttention {
    Risk(usize),
    Waiting(usize),
    Running(usize),
    Ready(usize),
    None,
}

fn task_attention(summary: TaskActivitySummary) -> TaskAttention {
    if summary.risk > 0 {
        TaskAttention::Risk(summary.risk)
    } else if summary.waiting > 0 {
        TaskAttention::Waiting(summary.waiting)
    } else if summary.running > 0 {
        TaskAttention::Running(summary.running)
    } else if summary.ready > 0 {
        TaskAttention::Ready(summary.ready)
    } else {
        TaskAttention::None
    }
}

pub(crate) fn install(app: &mut tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, OPEN_MENU, "Open RiftX", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let status = MenuItem::with_id(app, STATUS_MENU, "Runtime: Offline", false, None::<&str>)?;
    let task_status = MenuItem::with_id(app, TASK_STATUS_MENU, "Tasks: None", false, None::<&str>)?;
    let pause = MenuItem::with_id(app, PAUSE_MENU, "Pause", false, None::<&str>)?;
    let resume = MenuItem::with_id(app, RESUME_MENU, "Resume", false, None::<&str>)?;
    let kill = MenuItem::with_id(app, KILL_MENU, "Kill Switch", false, None::<&str>)?;
    let control_separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, QUIT_MENU, "Quit RiftX", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &separator,
            &status,
            &task_status,
            &pause,
            &resume,
            &kill,
            &control_separator,
            &quit,
        ],
    )?;
    app.manage(RuntimeMenu {
        status,
        task_status,
        pause,
        resume,
        kill,
    });
    let mut tray = TrayIconBuilder::with_id("riftx")
        .tooltip("RiftX")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            OPEN_MENU => show_main_window(app),
            PAUSE_MENU => request_runtime_change(app, "/v1/system/pause"),
            RESUME_MENU => request_runtime_change(app, "/v1/system/resume"),
            KILL_MENU => request_runtime_change(app, "/v1/system/kill"),
            QUIT_MENU => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

pub(crate) fn sync_runtime_status(app: &tauri::AppHandle, status: &DaemonControlStatus) {
    if let Some(menu) = app.try_state::<RuntimeMenu>() {
        menu.sync(status);
    }
    let _ = app.emit(RUNTIME_STATUS_EVENT, status);
}

pub(crate) fn sync_task_activity(app: &tauri::AppHandle, summary: TaskActivitySummary) {
    if let Some(menu) = app.try_state::<RuntimeMenu>() {
        menu.sync_tasks(summary);
    }
}

pub(crate) fn handle_window_event(window: &tauri::Window, event: &tauri::WindowEvent) {
    if window.label() != MAIN_WINDOW {
        return;
    }
    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
        api.prevent_close();
        let _ = window.hide();
    }
}

fn request_runtime_change(app: &tauri::AppHandle, path: &'static str) {
    if let Some(menu) = app.try_state::<RuntimeMenu>() {
        menu.busy();
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = app
            .state::<crate::bridge::DesktopState>()
            .update_runtime(path)
            .await;
        match result {
            Ok(status) => sync_runtime_status(&app, &status),
            Err(error) => {
                if let Some(menu) = app.try_state::<RuntimeMenu>() {
                    menu.unavailable();
                }
                let _ = app.emit(RUNTIME_ERROR_EVENT, error);
            }
        }
    });
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(test)]
#[path = "background_tests.rs"]
mod tests;
