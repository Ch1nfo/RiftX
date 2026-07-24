use super::*;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;

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
fn api_key_bundle_frame_uses_a_big_endian_length_prefix() {
    let api_keys = BTreeMap::from([(
        "profile-a".to_string(),
        codex_riftx_credentials::LlmApiKey::new("secret".to_string()).expect("valid API key"),
    )]);

    let frame = frame_api_keys(api_keys).expect("framed API keys");
    let payload_length = u32::from_be_bytes(frame[..4].try_into().expect("length")) as usize;
    assert_eq!(payload_length, frame.len() - 4);
    assert_eq!(
        serde_json::from_slice::<BTreeMap<String, String>>(&frame[4..]).expect("API key bundle"),
        BTreeMap::from([("profile-a".to_string(), "secret".to_string())])
    );
}
