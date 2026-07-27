use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

#[test]
fn converts_text_deltas_usage_and_terminal_event() {
    let mut converter = ChatStreamConverter::new("resp_test");
    let mut events = converter
        .ingest_sse_frame(
            r#"data: {"id":"chatcmpl_1","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":"stop"}]}"#,
        )
        .expect("chunk");
    events.extend(
        converter
            .ingest_sse_frame(
                r#"data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5,"prompt_tokens_details":{"cached_tokens":1},"completion_tokens_details":{"reasoning_tokens":1}}}"#,
            )
            .expect("usage"),
    );
    events.extend(converter.ingest_sse_frame("data: [DONE]").expect("done"));

    assert_eq!(events[0].event, "response.created");
    assert_eq!(
        events.last().map(|event| event.event.as_str()),
        Some("response.completed")
    );
    assert_eq!(
        events.last().expect("terminal").data["response"]["usage"],
        json!({
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 5,
        })
    );
}

#[test]
fn converts_tool_call_stream_to_function_call_item() {
    let mut converter = ChatStreamConverter::new("resp_tools");
    let mut events = converter
        .ingest_chunk(&json!({
            "id": "chatcmpl_tools",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "demo", "arguments": "{\"x\":"}
                }]}
            }]
        }))
        .expect("first");
    events.extend(
        converter
            .ingest_chunk(&json!({
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"arguments": "1}"}
                    }]},
                    "finish_reason": "tool_calls"
                }]
            }))
            .expect("second"),
    );
    events.extend(converter.ingest_sse_frame("data: [DONE]").expect("done"));
    let done = events
        .iter()
        .find(|event| event.event == "response.output_item.done")
        .expect("function call done");
    assert_eq!(
        done.data["item"],
        json!({
            "type": "function_call",
            "id": "fc_call_9",
            "call_id": "call_9",
            "name": "demo",
            "arguments": "{\"x\":1}",
            "status": "completed",
        })
    );
}

#[test]
fn maps_length_and_content_filter_to_non_success_terminal_events() {
    for (reason, expected) in [
        ("length", "response.incomplete"),
        ("content_filter", "response.failed"),
    ] {
        let mut converter = ChatStreamConverter::new("resp_terminal");
        converter
            .ingest_chunk(&json!({
                "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": reason}]
            }))
            .expect("chunk");
        let events = converter.ingest_sse_frame("data: [DONE]").expect("done");
        assert_eq!(
            events.last().map(|event| event.event.as_str()),
            Some(expected)
        );
    }
}

#[test]
fn done_without_finish_reason_is_rejected() {
    let mut converter = ChatStreamConverter::new("resp_missing_finish");
    let error = converter
        .ingest_sse_frame("data: [DONE]")
        .expect_err("missing finish reason");
    assert!(error.to_string().contains("finish_reason"));
}
