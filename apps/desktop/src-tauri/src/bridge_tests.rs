use super::*;
use pretty_assertions::assert_eq;

#[test]
fn opaque_identifiers_reject_path_injection() {
    assert_eq!(
        validate_engagement_id("9d7edfc1-7b24-41a0-9438-06baee034630"),
        Ok(())
    );
    assert_eq!(
        validate_engagement_id("../v1/system/info"),
        Err(DesktopError::new(
            "invalid_identifier",
            "engagement identifier is invalid",
        ))
    );
}

#[test]
fn desktop_rejects_an_incompatible_daemon_protocol() {
    assert_eq!(validate_protocol_version(IPC_PROTOCOL_VERSION), Ok(()));
    assert_eq!(
        validate_protocol_version(IPC_PROTOCOL_VERSION + 1),
        Err(DesktopError::new(
            "protocol_mismatch",
            format!(
                "RiftX Desktop requires IPC protocol {IPC_PROTOCOL_VERSION}, but riftxd provides {}",
                IPC_PROTOCOL_VERSION + 1
            ),
        ))
    );
}

#[test]
fn conversation_path_validates_the_cursor() {
    assert_eq!(
        conversation_path("engagement-1", None),
        Ok("/v1/engagements/engagement-1/conversation?limit=200".to_string())
    );
    assert_eq!(
        conversation_path("engagement-1", Some(42)),
        Ok("/v1/engagements/engagement-1/conversation?limit=200&cursor=42".to_string())
    );
    assert_eq!(
        conversation_path("engagement-1", Some(0)),
        Err(DesktopError::new(
            "invalid_cursor",
            "conversation cursor must be positive",
        ))
    );
}

#[test]
fn mode_change_request_is_typed_and_rejects_path_injection() {
    let (path, body) = mode_change_request(ChangeModeInput {
        engagement_id: "engagement-1".to_string(),
        mode: ExecutionMode::Auto,
        confirmation: Some("AUTO MODE - TEST ENVIRONMENT ONLY".to_string()),
    })
    .expect("mode request");
    assert_eq!(path, "/v1/engagements/engagement-1/mode");
    assert_eq!(
        body,
        ChangeModeParams {
            mode: ExecutionMode::Auto,
            confirmation: Some("AUTO MODE - TEST ENVIRONMENT ONLY".to_string()),
        }
    );
    assert_eq!(
        mode_change_request(ChangeModeInput {
            engagement_id: "../system/kill".to_string(),
            mode: ExecutionMode::Native,
            confirmation: None,
        }),
        Err(DesktopError::new(
            "invalid_identifier",
            "engagement identifier is invalid",
        ))
    );
}

#[test]
fn report_path_rejects_path_injection() {
    assert_eq!(
        report_path("engagement-1", ReportFormat::Markdown),
        Ok("/v1/engagements/engagement-1/report?format=markdown".to_string())
    );
    assert_eq!(
        report_path("../system/kill", ReportFormat::Json),
        Err(DesktopError::new(
            "invalid_identifier",
            "engagement identifier is invalid",
        ))
    );
}
