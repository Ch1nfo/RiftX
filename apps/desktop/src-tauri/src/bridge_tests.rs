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
        Ok(
            "/v1/engagements/engagement-1/conversation?limit=200&cursor=42"
                .to_string()
        )
    );
    assert_eq!(
        conversation_path("engagement-1", Some(0)),
        Err(DesktopError::new(
            "invalid_cursor",
            "conversation cursor must be positive",
        ))
    );
}
