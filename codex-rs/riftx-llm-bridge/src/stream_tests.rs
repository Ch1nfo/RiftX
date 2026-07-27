use super::*;
use pretty_assertions::assert_eq;

#[test]
fn converts_text_deltas_to_responses_events() {
    let mut converter = ChatStreamConverter::new("resp_test");
    let mut buffer = String::from(
        "data: {\"id\":\"chatcmpl_1\",\"choices\":[{\"delta\":{\"content\":\"Hel\"}}]}\n\n\
         data: {\"choices\":[{\"delta\":{\"content\":\"lo\"},\"finish_reason\":\"stop\"}]}\n\n\
         data: [DONE]\n\n",
    );
    let events = converter
        .ingest_sse_buffer(&mut buffer)
        .expect("ingest text stream");
    assert_eq!(events[0].event, "response.created");
    assert!(
        events
            .iter()
            .any(|event| event.event == "response.output_text.delta")
    );
    assert!(
        events
            .iter()
            .any(|event| event.event == "response.output_item.done")
    );
    assert_eq!(
        events.last().map(|event| event.event.as_str()),
        Some("response.completed")
    );
}

#[test]
fn converts_tool_call_stream_to_function_call_item() {
    let mut converter = ChatStreamConverter::new("resp_tools");
    let chunk1 = serde_json::json!({
        "id": "chatcmpl_tools",
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "demo", "arguments": ""}
                }]
            }
        }]
    });
    let chunk2 = serde_json::json!({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "function": {"arguments": "{\"x\":1}"}
                }]
            },
            "finish_reason": "tool_calls"
        }]
    });
    let mut events = converter.ingest_chunk(&chunk1).expect("first");
    events.extend(converter.ingest_chunk(&chunk2).expect("second"));
    let done = events
        .iter()
        .find(|event| event.event == "response.output_item.done")
        .expect("function call done");
    assert_eq!(done.data["item"]["type"], "function_call");
    assert_eq!(done.data["item"]["call_id"], "call_9");
    assert_eq!(done.data["item"]["name"], "demo");
    assert_eq!(done.data["item"]["arguments"], "{\"x\":1}");
}

#[test]
fn ignores_keep_alive_comments() {
    let mut converter = ChatStreamConverter::new("resp_keepalive");
    let mut buffer = String::from(": keep-alive\n\ndata: [DONE]\n\n");
    let events = converter
        .ingest_sse_buffer(&mut buffer)
        .expect("keep-alive");
    assert_eq!(
        events.last().map(|event| event.event.as_str()),
        Some("response.completed")
    );
}
