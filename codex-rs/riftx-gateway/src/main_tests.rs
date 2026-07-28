use super::*;
use clap::error::ErrorKind;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;
use std::io::Cursor;

#[test]
fn version_flag_reports_the_release_version() {
    let error = Args::try_parse_from(["riftxd", "--version"]).expect_err("version exits early");

    assert_eq!(error.kind(), ErrorKind::DisplayVersion);
    assert!(error.to_string().contains("1.0.0"));
}

#[test]
fn reads_a_length_prefixed_api_key_bundle() {
    let payload = br#"{"profile-a":"secret-a","profile-b":"secret-b"}"#;
    let mut frame = u32::try_from(payload.len())
        .expect("payload length")
        .to_be_bytes()
        .to_vec();
    frame.extend_from_slice(payload);

    assert_eq!(
        read_llm_api_keys(&mut Cursor::new(frame))
            .expect("valid framed API keys")
            .into_iter()
            .map(|(profile, api_key)| (profile, api_key.into_inner()))
            .collect::<BTreeMap<_, _>>(),
        BTreeMap::from([
            ("profile-a".to_string(), "secret-a".to_string()),
            ("profile-b".to_string(), "secret-b".to_string()),
        ])
    );
}

#[test]
fn reads_a_plain_json_api_key_bundle() {
    let payload = br#"{"profile-a":"secret-a","profile-b":"secret-b"}"#;

    assert_eq!(
        read_llm_api_keys_json(&mut Cursor::new(payload))
            .expect("valid JSON API keys")
            .into_iter()
            .map(|(profile, api_key)| (profile, api_key.into_inner()))
            .collect::<BTreeMap<_, _>>(),
        BTreeMap::from([
            ("profile-a".to_string(), "secret-a".to_string()),
            ("profile-b".to_string(), "secret-b".to_string()),
        ])
    );
}

#[test]
fn rejects_an_empty_plain_json_api_key_bundle() {
    assert_eq!(
        read_llm_api_keys_json(&mut Cursor::new(b"{}"))
            .expect_err("empty JSON API key bundle must fail")
            .to_string(),
        "LLM API key bundle cannot be empty"
    );
}

#[test]
fn rejects_an_oversized_api_key_bundle_before_allocating_it() {
    let length = u32::try_from(MAX_STDIN_API_KEY_BUNDLE_BYTES + 1).expect("test length fits");
    let mut frame = Cursor::new(length.to_be_bytes());

    assert_eq!(
        read_llm_api_keys(&mut frame)
            .expect_err("oversized API key bundle must fail")
            .to_string(),
        "invalid LLM API key bundle frame length"
    );
}

#[test]
fn rejects_an_oversized_plain_json_api_key_bundle_with_a_bounded_read() {
    let payload = vec![b'x'; MAX_STDIN_API_KEY_BUNDLE_BYTES + 1];

    assert_eq!(
        read_llm_api_keys_json(&mut Cursor::new(payload))
            .expect_err("oversized JSON API key bundle must fail")
            .to_string(),
        "LLM API key JSON exceeds the maximum size"
    );
}
