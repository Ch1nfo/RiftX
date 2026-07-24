use super::*;
use pretty_assertions::assert_eq;

#[test]
fn probe_only_starts_a_sidecar_when_ipc_is_unavailable() {
    assert_eq!(classify_probe(Ok(())), Ok(true));
    assert_eq!(
        classify_probe(Err(DesktopError::new(
            "daemon_unavailable",
            "connection refused",
        ))),
        Ok(false)
    );

    let protocol_error = DesktopError::new("protocol_mismatch", "incompatible daemon");
    assert_eq!(
        classify_probe(Err(protocol_error.clone())),
        Err(protocol_error)
    );
}

#[test]
fn api_key_frame_uses_a_big_endian_length_prefix() {
    let api_key =
        codex_riftx_credentials::LlmApiKey::new("secret".to_string()).expect("valid API key");

    assert_eq!(
        frame_api_key(api_key),
        Ok(vec![0, 0, 0, 6, b's', b'e', b'c', b'r', b'e', b't'])
    );
}
