use crate::BridgeUpstream;
use crate::start_loopback_bridge;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::time::Duration;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;
use wiremock::matchers::header;
use wiremock::matchers::method;
use wiremock::matchers::path;

#[tokio::test]
async fn loopback_bridge_translates_chat_completions_stream() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/chat/completions"))
        .and(header("authorization", "Bearer upstream-secret"))
        .respond_with(ResponseTemplate::new(200).set_body_string(
            "data: {\"id\":\"chatcmpl_bridge\",\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n\
             data: [DONE]\n\n",
        ))
        .mount(&upstream)
        .await;

    let bridge = start_loopback_bridge(BridgeUpstream {
        base_url: upstream.uri(),
        api_key: "upstream-secret".to_string(),
        timeout: Duration::from_secs(5),
    })
    .await
    .expect("start bridge");

    let client = reqwest::Client::new();
    let response = client
        .post(format!("{}/responses", bridge.responses_base_url()))
        .bearer_auth(bridge.bearer_token())
        .json(&json!({
            "model": "demo",
            "instructions": "test",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}]
            }],
            "stream": true
        }))
        .send()
        .await
        .expect("bridge response");
    assert_eq!(response.status(), reqwest::StatusCode::OK);
    let body = response.text().await.expect("sse body");
    assert!(body.contains("response.created"));
    assert!(body.contains("response.output_text.delta"));
    assert!(body.contains("response.completed"));
}
