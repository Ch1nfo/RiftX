mod bridge;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(bridge::DesktopState::load())
        .invoke_handler(tauri::generate_handler![
            bridge::daemon_info,
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
        ])
        .run(tauri::generate_context!())
        .expect("RiftX desktop runtime failed");
}
