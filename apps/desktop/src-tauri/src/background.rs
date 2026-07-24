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
const QUIT_MENU: &str = "quit";

pub(crate) fn install(app: &mut tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, OPEN_MENU, "Open RiftX", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, QUIT_MENU, "Quit RiftX", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &separator, &quit])?;
    let mut tray = TrayIconBuilder::with_id("riftx")
        .tooltip("RiftX")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            OPEN_MENU => show_main_window(app),
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

pub(crate) fn handle_window_event(window: &tauri::Window, event: &tauri::WindowEvent) {
    if window.label() != MAIN_WINDOW {
        return;
    }
    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
        api.prevent_close();
        let _ = window.hide();
    }
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}
