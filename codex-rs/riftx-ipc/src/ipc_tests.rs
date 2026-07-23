use super::*;
use axum::Router;
use axum::routing::get;
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
            Router::new().route("/events", get(|| async { "one\ntwo\n" })),
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
    server.abort();
}
