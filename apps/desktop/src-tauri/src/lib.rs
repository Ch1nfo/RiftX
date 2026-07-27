mod background;
mod bridge;
mod credentials;
mod daemon;
mod extensions;
mod notifications;
mod settings;
mod settings_coordination;

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
            bridge::change_engagement_mode,
            bridge::auto_status,
            bridge::pause_auto,
            bridge::resume_auto,
            bridge::kill_auto,
            bridge::start_turn,
            bridge::list_approvals,
            bridge::decide_approval,
            bridge::event_stream::engagement_stream_status,
            bridge::interrupt_engagement,
            bridge::engagement_report,
            bridge::engagement_report_markdown,
            bridge::conversation_history,
            credentials::list_assessment_credentials,
            credentials::create_assessment_credential,
            credentials::delete_assessment_credential,
            credentials::list_credential_grants,
            credentials::create_credential_grant,
            credentials::revoke_credential_grant,
            extensions::tool_inventory,
            extensions::tool_doctor,
            extensions::skill_catalog,
            extensions::skill_doctor,
            extensions::llm_profiles,
            extensions::test_llm_profile,
            settings::llm_settings,
            settings::get_tools_settings,
            settings::save_tools_settings,
            settings::upsert_llm_profile,
            settings::delete_llm_profile,
            settings::set_default_llm_profile,
            settings::save_llm_api_key,
            settings::delete_llm_api_key,
            settings_coordination::settings_reload_impact,
            settings_coordination::prepare_settings_reload,
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
