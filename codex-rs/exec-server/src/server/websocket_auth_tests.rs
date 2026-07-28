use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use axum::http::HeaderMap;
use axum::http::HeaderValue;
use axum::http::header::AUTHORIZATION;
use sha2::Digest;
use sha2::Sha256;

use super::ConnectionAuthorization;
use super::ExecServerWebSocketAuth;

#[test]
fn bootstrap_is_consumed_only_after_initialize() {
    let auth = test_auth("bootstrap-secret");
    assert!(auth.authorize(&headers("Bearer wrong")).is_none());

    let reservation = auth
        .authorize(&headers("Bearer bootstrap-secret"))
        .expect("valid bootstrap token");
    assert!(
        auth.authorize(&headers("Bearer bootstrap-secret"))
            .is_none()
    );
    drop(reservation);

    let reservation = auth
        .authorize(&headers("Bearer bootstrap-secret"))
        .expect("reservation should be released after disconnect");
    reservation
        .validate_initialize(None)
        .expect("bootstrap initializes a new session");
    reservation.consume_bootstrap();
    drop(reservation);

    assert!(
        auth.authorize(&headers("Bearer bootstrap-secret"))
            .is_none()
    );
}

#[test]
fn session_header_must_match_resume_session_id() {
    let auth = test_auth("bootstrap-secret");
    let bootstrap = auth
        .authorize(&headers("Bearer bootstrap-secret"))
        .expect("valid bootstrap token");
    bootstrap.consume_bootstrap();
    drop(bootstrap);

    let session_id = "123e4567-e89b-42d3-a456-426614174000";
    let authorization = auth
        .authorize(&headers(&format!("RiftX-Session {session_id}")))
        .expect("valid session header");
    assert!(matches!(authorization, ConnectionAuthorization::Session(_)));
    authorization
        .validate_initialize(Some(session_id))
        .expect("matching session should resume");
    assert!(
        authorization
            .validate_initialize(Some("123e4567-e89b-42d3-a456-426614174001"))
            .is_err()
    );
}

fn test_auth(token: &str) -> ExecServerWebSocketAuth {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("auth.json");
    let expires_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock")
        .checked_add(Duration::from_secs(60))
        .expect("expiry")
        .as_secs();
    let hash = Sha256::digest(token.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    std::fs::write(
        &path,
        format!(r#"{{"bootstrapSha256":"{hash}","expiresAt":{expires_at}}}"#),
    )
    .expect("write auth file");
    ExecServerWebSocketAuth::from_file(&path).expect("load auth")
}

fn headers(value: &str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(value).expect("valid authorization header"),
    );
    headers
}
