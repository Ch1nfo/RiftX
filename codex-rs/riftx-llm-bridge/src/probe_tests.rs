use super::ProbeProtocol;
use super::ProbeTarget;
use super::probe_connection;
use super::responses_url;
use pretty_assertions::assert_eq;
use std::time::Duration;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path;

#[test]
fn responses_url_appends_path() {
    assert_eq!(
        responses_url("http://127.0.0.1:9/v1"),
        "http://127.0.0.1:9/v1/responses"
    );
    assert_eq!(
        responses_url("http://127.0.0.1:9/v1/responses/"),
        "http://127.0.0.1:9/v1/responses"
    );
}

#[tokio::test]
async fn responses_probe_passes_text_and_tool_layers() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "event: response.created\ndata: {\"type\":\"response.created\"}\n\n\
             event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"ping\"}\n\n\
             event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
        ))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "event: response.created\ndata: {\"type\":\"response.created\"}\n\n\
             event: response.output_item.done\n\
             data: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"function_call\",\"name\":\"riftx_connection_test\",\"call_id\":\"c1\",\"arguments\":\"{\\\"ping\\\":\\\"ok\\\"}\"}}\n\n\
             event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
        ))
        .mount(&server)
        .await;

    let outcome = probe_connection(ProbeTarget {
        protocol: ProbeProtocol::Responses,
        base_url: format!("{}/v1", server.uri()),
        api_key: "test-key".into(),
        model: "demo".into(),
        timeout: Duration::from_secs(5),
    })
    .await;

    assert!(outcome.stream_text.ok, "{:?}", outcome.stream_text);
    assert!(outcome.function_tools.ok, "{:?}", outcome.function_tools);
}

#[tokio::test]
async fn responses_probe_rejects_plain_text_tool_name_false_positive() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"ping\"}\n\n\
             event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
        ))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "event: response.output_text.delta\n\
             data: {\"type\":\"response.output_text.delta\",\"delta\":\"I would call riftx_connection_test\"}\n\n\
             event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n",
        ))
        .mount(&server)
        .await;

    let outcome = probe_connection(ProbeTarget {
        protocol: ProbeProtocol::Responses,
        base_url: format!("{}/v1", server.uri()),
        api_key: "test-key".into(),
        model: "demo".into(),
        timeout: Duration::from_secs(5),
    })
    .await;

    assert!(outcome.stream_text.ok);
    assert!(!outcome.function_tools.ok);
}

#[tokio::test]
async fn chat_completions_probe_uses_bridge() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "data: {\"id\":\"chatcmpl_1\",\"choices\":[{\"delta\":{\"content\":\"ping\"},\"finish_reason\":\"stop\"}]}\n\n\
             data: [DONE]\n\n",
        ))
        .up_to_n_times(1)
        .mount(&upstream)
        .await;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "data: {\"id\":\"chatcmpl_2\",\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"function\":{\"name\":\"riftx_connection_test\",\"arguments\":\"{\\\"ping\\\":\\\"ok\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\n\
             data: [DONE]\n\n",
        ))
        .mount(&upstream)
        .await;

    let outcome = probe_connection(ProbeTarget {
        protocol: ProbeProtocol::ChatCompletions,
        base_url: upstream.uri(),
        api_key: "upstream-secret".into(),
        model: "demo".into(),
        timeout: Duration::from_secs(5),
    })
    .await;

    assert!(outcome.stream_text.ok, "{:?}", outcome.stream_text);
    assert!(outcome.function_tools.ok, "{:?}", outcome.function_tools);
}
