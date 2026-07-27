use super::sanitize_diagnostic;
use pretty_assertions::assert_eq;

#[test]
fn redacts_common_secret_shapes() {
    assert_eq!(
        sanitize_diagnostic(
            "Authorization: Bearer secret-token api_key=another-secret sk-live-secret",
            200,
        ),
        "[REDACTED]"
    );
}

#[test]
fn truncates_at_unicode_character_boundaries() {
    assert_eq!(sanitize_diagnostic("错误错误错误", 4), "错误错误…");
}
