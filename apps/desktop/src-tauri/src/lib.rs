mod background;
mod bridge;
mod daemon;
mod notifications;
mod settings;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            background::install(app)?;
            Ok(())
        })
        .on_window_event(background::handle_window_event)
        .manage(bridge::DesktopState::load())
        .invoke_handler(tauri::generate_handler![
            bridge::daemon_info,
            bridge::pause_runtime,
            bridge::resume_runtime,
            bridge::kill_runtime,
            bridge::list_engagements,
            bridge::create_engagement,
            bridge::activate_engagement,
            bridge::start_turn,
            bridge::list_approvals,
            bridge::decide_approval,
            bridge::event_stream::subscribe_engagement,
            bridge::event_stream::unsubscribe_engagement,
            bridge::interrupt_engagement,
            bridge::engagement_report,
            bridge::conversation_history,
            settings::llm_settings,
            settings::save_llm_api_key,
            settings::delete_llm_api_key,
            notifications::notification_settings,
            notifications::request_notification_permission,
        ])
        .build(tauri::generate_context!())
        .expect("RiftX desktop runtime failed");
    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            use tauri::Manager;

            app_handle
                .state::<bridge::DesktopState>()
                .daemon
                .stop_owned();
        }
    });
}
