use super::*;
use pretty_assertions::assert_eq;

#[test]
fn provider_errors_are_classified_without_exposing_response_bodies() {
    let cases = [
        (
            notification(Some(CodexErrorInfo::Unauthorized), "invalid key"),
            Some((ProviderErrorClass::Authentication, None)),
        ),
        (
            notification(
                Some(CodexErrorInfo::HttpConnectionFailed {
                    http_status_code: Some(403),
                }),
                "forbidden",
            ),
            Some((ProviderErrorClass::Authentication, Some(403))),
        ),
        (
            notification(
                Some(CodexErrorInfo::Other),
                "unexpected status 404 Not Found: redacted upstream body",
            ),
            Some((ProviderErrorClass::Protocol, Some(404))),
        ),
        (
            notification(
                Some(CodexErrorInfo::ResponseTooManyFailedAttempts {
                    http_status_code: Some(429),
                }),
                "retry budget exhausted",
            ),
            Some((ProviderErrorClass::RateLimited, Some(429))),
        ),
        (
            notification(Some(CodexErrorInfo::ServerOverloaded), "overloaded"),
            Some((ProviderErrorClass::RateLimited, None)),
        ),
        (
            notification(
                Some(CodexErrorInfo::Other),
                "Access blocked by Cloudflare (status 403 Forbidden)",
            ),
            Some((ProviderErrorClass::Authentication, Some(403))),
        ),
        (
            notification(Some(CodexErrorInfo::Other), "unclassified runtime error"),
            None,
        ),
    ];

    for (notification, expected) in cases {
        assert_eq!(classify_error(&notification.error), expected);
    }
}

fn notification(codex_error_info: Option<CodexErrorInfo>, message: &str) -> ErrorNotification {
    ErrorNotification {
        error: TurnError {
            message: message.to_string(),
            codex_error_info,
            additional_details: None,
        },
        will_retry: false,
        thread_id: "thread-provider-error".to_string(),
        turn_id: "turn-provider-error".to_string(),
    }
}
