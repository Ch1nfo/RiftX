use super::*;

#[test]
fn parses_jsonl_and_severity() {
    assert_eq!(
        json_lines("{\"url\":\"https://example.test\"}\ninvalid\n").count(),
        1
    );
    assert_eq!(severity(Some("high")), FindingSeverity::High);
}
