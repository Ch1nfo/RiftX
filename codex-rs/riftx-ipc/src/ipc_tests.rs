use super::*;
use axum::Router;
use axum::routing::get;
use axum::routing::post;
use futures::StreamExt;
use pretty_assertions::assert_eq;

#[test]
fn endpoint_is_derived_from_runtime_directory() {
    let endpoint = LocalIpcEndpoint::new("runtime/ipc");
    #[cfg(unix)]
    assert_eq!(
        endpoint.socket_path(),
        std::path::PathBuf::from("runtime/ipc/riftxd.sock")
    );
    #[cfg(windows)]
    assert!(endpoint.pipe_name().starts_with(r"\\.\pipe\riftx-"));
}

#[cfg(unix)]
#[tokio::test]
async fn local_http_round_trip_and_streaming() {
    let temp = tempfile::tempdir().expect("tempdir");
    let endpoint = LocalIpcEndpoint::new(temp.path().join("ipc"));
    let listener = LocalIpcListener::bind(endpoint.clone())
        .await
        .expect("bind");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new()
                .route("/events", get(|| async { "one\ntwo\n" }))
                .route(
                    "/secret",
                    post(
                        |headers: axum::http::HeaderMap, body: axum::body::Bytes| async move {
                            assert_eq!(
                                headers.get(axum::http::header::CONTENT_TYPE),
                                Some(&axum::http::HeaderValue::from_static(
                                    "application/octet-stream"
                                ))
                            );
                            body
                        },
                    ),
                ),
        )
        .await
        .expect("serve");
    });

    let response = LocalIpcClient::new(endpoint)
        .get("/events")
        .await
        .expect("request");
    assert_eq!(response.status(), http::StatusCode::OK);
    let chunks = response
        .into_data_stream()
        .collect::<Vec<_>>()
        .await
        .into_iter()
        .collect::<Result<Vec<_>, _>>()
        .expect("stream");
    assert_eq!(chunks.concat(), b"one\ntwo\n");
    let response = LocalIpcClient::new(LocalIpcEndpoint::new(temp.path().join("ipc")))
        .post_bytes("/secret", b"sensitive bytes".to_vec())
        .await
        .expect("binary request");
    assert_eq!(
        response.bytes().await.expect("binary response"),
        "sensitive bytes"
    );
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn typed_json_request_and_response_round_trip() {
    let temp = tempfile::tempdir().expect("tempdir");
    let endpoint = LocalIpcEndpoint::new(temp.path().join("ipc"));
    let listener = LocalIpcListener::bind(endpoint.clone())
        .await
        .expect("bind");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new().route(
                "/turn",
                post(|body: axum::body::Bytes| async move {
                    let params: StartTurnParams =
                        serde_json::from_slice(&body).expect("typed request JSON");
                    serde_json::to_vec(&TurnAccepted {
                        task_id: params.input.unwrap_or_default(),
                        status: codex_riftx_domain::TaskStatus::Pending,
                    })
                    .expect("typed response JSON")
                }),
            ),
        )
        .await
        .expect("serve");
    });

    let response = LocalIpcClient::new(endpoint)
        .post_typed(
            "/turn",
            &StartTurnParams {
                input: Some("task-1".to_string()),
            },
        )
        .await
        .expect("typed request");
    assert_eq!(response.status(), http::StatusCode::OK);
    assert_eq!(
        response
            .json::<TurnAccepted>()
            .await
            .expect("typed response"),
        TurnAccepted {
            task_id: "task-1".to_string(),
            status: codex_riftx_domain::TaskStatus::Pending,
        }
    );
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn local_sse_stream_decodes_events_and_ignores_keep_alive_frames() {
    let temp = tempfile::tempdir().expect("tempdir");
    let endpoint = LocalIpcEndpoint::new(temp.path().join("ipc"));
    let listener = LocalIpcListener::bind(endpoint.clone())
        .await
        .expect("bind");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new().route(
                "/events",
                get(|| async {
                    concat!(
                        ":keep-alive\r\n\r\n",
                        "id: event-1\r\n",
                        "event: turnStarted\r\n",
                        "data: {\"engagementId\":\"eng-1\",\r\n",
                        "data: \"kind\":\"turnStarted\"}\r\n\r\n",
                        "event: turnCompleted\n",
                        "data: {}\n\n"
                    )
                }),
            ),
        )
        .await
        .expect("serve");
    });

    let response = LocalIpcClient::new(endpoint)
        .get("/events")
        .await
        .expect("request");
    let mut events = response.into_sse_stream();
    assert_eq!(
        events.next_event().await.expect("first event"),
        Some(LocalSseEvent {
            event: Some("turnStarted".to_string()),
            data: "{\"engagementId\":\"eng-1\",\n\"kind\":\"turnStarted\"}".to_string(),
            id: Some("event-1".to_string()),
        })
    );
    assert_eq!(
        events.next_event().await.expect("second event"),
        Some(LocalSseEvent {
            event: Some("turnCompleted".to_string()),
            data: "{}".to_string(),
            id: None,
        })
    );
    assert_eq!(events.next_event().await.expect("stream end"), None);
    server.abort();
}
