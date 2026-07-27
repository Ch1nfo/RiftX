use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

#[test]
fn converts_instructions_user_text_and_function_tool() {
    let request = json!({
        "model": "deepseek-chat",
        "instructions": "You are RiftX.",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "scan now"}]
        }],
        "tools": [{
            "type": "function",
            "name": "demo",
            "description": "Demo tool",
            "parameters": {"type": "object", "properties": {}}
        }],
        "tool_choice": "auto",
        "parallel_tool_calls": false,
        "stream": true
    });

    let chat = responses_request_to_chat(&request).expect("convert");
    assert_eq!(
        chat,
        json!({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "You are RiftX."},
                {"role": "user", "content": "scan now"}
            ],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "demo",
                    "description": "Demo tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            }],
            "tool_choice": "auto",
            "parallel_tool_calls": false,
            "stream": true
        })
    );
}

#[test]
fn converts_function_call_and_output_history() {
    let request = json!({
        "model": "deepseek-chat",
        "instructions": "",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "demo",
                "arguments": "{\"target\":\"10.0.0.1\"}"
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "ok"
            }
        ],
        "stream": true
    });
    let chat = responses_request_to_chat(&request).expect("convert");
    assert_eq!(
        chat["messages"],
        json!([
            {
                "role": "assistant",
                "content": null,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "demo",
                        "arguments": "{\"target\":\"10.0.0.1\"}"
                    }
                }]
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "ok"
            }
        ])
    );
}

#[test]
fn rejects_image_input() {
    let request = json!({
        "model": "deepseek-chat",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": "https://example.test/a.png"}]
        }],
        "stream": true
    });
    let error = responses_request_to_chat(&request).expect_err("images unsupported");
    assert!(error.to_string().contains("input_image"));
}

#[test]
fn rejects_unknown_tool_types() {
    let request = json!({
        "model": "deepseek-chat",
        "input": [],
        "tools": [{"type": "web_search"}],
        "stream": true
    });
    let error = responses_request_to_chat(&request).expect_err("web_search unsupported");
    assert!(error.to_string().contains("web_search"));
}

#[test]
fn chat_completions_url_appends_path() {
    assert_eq!(
        chat_completions_url("https://api.deepseek.com"),
        "https://api.deepseek.com/chat/completions"
    );
    assert_eq!(
        chat_completions_url("https://api.openai.com/v1/"),
        "https://api.openai.com/v1/chat/completions"
    );
    assert_eq!(
        chat_completions_url("https://api.deepseek.com/chat/completions"),
        "https://api.deepseek.com/chat/completions"
    );
}
