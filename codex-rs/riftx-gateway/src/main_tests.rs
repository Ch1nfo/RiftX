use super::*;
use pretty_assertions::assert_eq;
use std::io::Cursor;

#[test]
fn reads_a_length_prefixed_api_key() {
    let mut frame = Cursor::new([0, 0, 0, 6, b's', b'e', b'c', b'r', b'e', b't']);

    assert_eq!(
        read_llm_api_key(&mut frame)
            .expect("valid framed API key")
            .into_inner(),
        "secret"
    );
}

#[test]
fn rejects_an_oversized_api_key_before_allocating_it() {
    let length = u32::try_from(MAX_STDIN_API_KEY_BYTES + 1).expect("test length fits");
    let mut frame = Cursor::new(length.to_be_bytes());

    assert_eq!(
        read_llm_api_key(&mut frame)
            .expect_err("oversized key must fail")
            .to_string(),
        "invalid LLM API key frame length"
    );
}
