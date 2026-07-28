use super::SseDecoder;
use pretty_assertions::assert_eq;

#[test]
fn preserves_utf8_split_across_network_chunks() {
    let mut decoder = SseDecoder::default();
    let bytes = "data: 模型\n\n".as_bytes();
    let split = bytes.iter().position(|byte| *byte >= 0x80).expect("UTF-8");
    assert_eq!(
        decoder.push(&bytes[..split + 1]).expect("first"),
        Vec::<String>::new()
    );
    assert_eq!(
        decoder.push(&bytes[split + 1..]).expect("second"),
        vec!["data: 模型"]
    );
}

#[test]
fn accepts_crlf_frames_split_at_the_delimiter() {
    let mut decoder = SseDecoder::default();
    assert!(decoder.push(b"data: one\r\n\r").expect("first").is_empty());
    assert_eq!(
        decoder.push(b"\ndata: two\r\n\r\n").expect("second"),
        vec!["data: one", "data: two"]
    );
}

#[test]
fn rejects_non_whitespace_residual_data_at_eof() {
    let mut decoder = SseDecoder::default();
    decoder.push(b"data: partial").expect("push");
    assert!(
        decoder
            .finish()
            .expect_err("incomplete")
            .to_string()
            .contains("incomplete SSE frame")
    );
}
