use super::*;

#[test]
fn bounded_text_preserves_utf8_and_marks_truncation() {
    let text = "测".repeat(MAX_CONVERSATION_ENTRY_BYTES);
    let bounded = bounded_text(&text);

    assert!(bounded.is_char_boundary(bounded.len()));
    assert!(bounded.ends_with(TRUNCATION_SUFFIX));
    assert!(bounded.len() <= MAX_CONVERSATION_ENTRY_BYTES);
}
