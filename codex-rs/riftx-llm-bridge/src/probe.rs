//! Connection probes against Responses or Chat Completions endpoints.
//!
//! Probes never return Authorization headers, API keys, or full response bodies.

use crate::BridgeUpstream;
use crate::diagnostics::sanitize_diagnostic;
use crate::start_loopback_bridge;
use futures::StreamExt;
use serde_json::Value;
use serde_json::json;
use std::time::Duration;

const TEST_TOOL_NAME: &str = "riftx_connection_test";
const MAX_ERROR_CHARS: usize = 240;
const MAX_SSE_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeProtocol {
    Responses,
    ChatCompletions,
}

#[derive(Debug, Clone)]
pub struct ProbeTarget {
    pub protocol: ProbeProtocol,
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub timeout: Duration,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeLayerResult {
    pub ok: bool,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeOutcome {
    pub stream_text: ProbeLayerResult,
    pub function_tools: ProbeLayerResult,
}

/// Run stream-text and function-tool probes against a Profile endpoint.
pub async fn probe_connection(target: ProbeTarget) -> ProbeOutcome {
    match target.protocol {
        ProbeProtocol::Responses => probe_responses_endpoint(&target).await,
        ProbeProtocol::ChatCompletions => probe_via_bridge(target).await,
    }
}

async fn probe_via_bridge(target: ProbeTarget) -> ProbeOutcome {
    let bridge = match start_loopback_bridge(BridgeUpstream {
        base_url: target.base_url.clone(),
        api_key: target.api_key.clone(),
        timeout: target.timeout,
    })
    .await
    {
        Ok(bridge) => bridge,
        Err(error) => {
            let detail = sanitize_error(&error.to_string());
            return ProbeOutcome {
                stream_text: ProbeLayerResult { ok: false, detail },
                function_tools: ProbeLayerResult {
                    ok: false,
                    detail: "skipped because bridge startup failed".into(),
                },
            };
        }
    };

    let bridged = ProbeTarget {
        protocol: ProbeProtocol::Responses,
        base_url: bridge.responses_base_url().to_string(),
        api_key: bridge.bearer_token().to_string(),
        model: target.model,
        timeout: target.timeout,
    };
    probe_responses_endpoint(&bridged).await
}

async fn probe_responses_endpoint(target: &ProbeTarget) -> ProbeOutcome {
    let client = match reqwest::Client::builder().timeout(target.timeout).build() {
        Ok(client) => client,
        Err(error) => {
            let detail = sanitize_error(&error.to_string());
            return ProbeOutcome {
                stream_text: ProbeLayerResult { ok: false, detail },
                function_tools: ProbeLayerResult {
                    ok: false,
                    detail: "skipped because HTTP client setup failed".into(),
                },
            };
        }
    };

    let stream_text = match run_stream_text_probe(&client, target).await {
        Ok(()) => ProbeLayerResult {
            ok: true,
            detail: "received streamed text delta".into(),
        },
        Err(error) => ProbeLayerResult {
            ok: false,
            detail: sanitize_error(&error),
        },
    };

    let function_tools = if stream_text.ok {
        match run_function_tool_probe(&client, target).await {
            Ok(()) => ProbeLayerResult {
                ok: true,
                detail: format!("model produced {TEST_TOOL_NAME} function call"),
            },
            Err(error) => ProbeLayerResult {
                ok: false,
                detail: sanitize_error(&error),
            },
        }
    } else {
        ProbeLayerResult {
            ok: false,
            detail: "skipped because stream text probe failed".into(),
        }
    };

    ProbeOutcome {
        stream_text,
        function_tools,
    }
}

async fn run_stream_text_probe(
    client: &reqwest::Client,
    target: &ProbeTarget,
) -> Result<(), String> {
    let body = json!({
        "model": target.model,
        "instructions": "Reply with the single word ping and nothing else.",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}]
        }],
        "stream": true,
        "store": false,
    });
    let sse = post_responses_sse(client, target, &body).await?;
    if sse.contains("response.output_text.delta") {
        return Ok(());
    }
    Err("stream completed without a text delta".into())
}

async fn run_function_tool_probe(
    client: &reqwest::Client,
    target: &ProbeTarget,
) -> Result<(), String> {
    let body = json!({
        "model": target.model,
        "instructions": "Call the provided test function. Do not answer with plain text.",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Run the connection test tool."}]
        }],
        "tools": [{
            "type": "function",
            "name": TEST_TOOL_NAME,
            "description": "No-side-effect RiftX connection test. Call with ping set to ok.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ping": { "type": "string" }
                },
                "required": ["ping"],
                "additionalProperties": false
            }
        }],
        "tool_choice": { "type": "function", "name": TEST_TOOL_NAME },
        "stream": true,
        "store": false,
        "parallel_tool_calls": false,
    });
    let sse = post_responses_sse(client, target, &body).await?;
    validate_function_tool_call(&sse)
}

fn validate_function_tool_call(sse: &str) -> Result<(), String> {
    let normalized = sse.replace("\r\n", "\n");
    for frame in normalized.split("\n\n") {
        let data = frame
            .lines()
            .filter_map(|line| line.strip_prefix("data:").map(str::trim_start))
            .collect::<Vec<_>>()
            .join("\n");
        if data.is_empty() || data == "[DONE]" {
            continue;
        }
        let event: Value = serde_json::from_str(&data)
            .map_err(|_| "probe received malformed Responses SSE JSON".to_string())?;
        if event.get("type").and_then(Value::as_str) == Some("response.failed") {
            return Err("upstream reported response.failed".into());
        }
        if event.get("type").and_then(Value::as_str) != Some("response.output_item.done") {
            continue;
        }
        let Some(item) = event.get("item") else {
            continue;
        };
        if item.get("type").and_then(Value::as_str) != Some("function_call")
            || item.get("name").and_then(Value::as_str) != Some(TEST_TOOL_NAME)
            || item
                .get("call_id")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        {
            continue;
        }
        let Some(arguments) = item.get("arguments").and_then(Value::as_str) else {
            continue;
        };
        let Ok(arguments) = serde_json::from_str::<Value>(arguments) else {
            continue;
        };
        if arguments.get("ping").and_then(Value::as_str) == Some("ok") {
            return Ok(());
        }
    }
    Err("stream completed without a valid structured riftx_connection_test function call".into())
}

async fn post_responses_sse(
    client: &reqwest::Client,
    target: &ProbeTarget,
    body: &Value,
) -> Result<String, String> {
    let url = responses_url(&target.base_url);
    let response = client
        .post(&url)
        .bearer_auth(&target.api_key)
        .header(reqwest::header::ACCEPT, "text/event-stream")
        .json(body)
        .send()
        .await
        .map_err(|error| format!("request failed: {error}"))?;

    let status = response.status();
    if !status.is_success() {
        let snippet = sanitize_error(&response.text().await.unwrap_or_default());
        return Err(format!(
            "upstream returned HTTP {}{}",
            status.as_u16(),
            if snippet.trim().is_empty() {
                String::new()
            } else {
                format!(" ({snippet})")
            }
        ));
    }

    let mut stream = response.bytes_stream();
    let mut collected = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| format!("stream read failed: {error}"))?;
        collected.extend_from_slice(&chunk);
        if collected.len() > MAX_SSE_BYTES {
            return Err("stream exceeded probe size limit".into());
        }
    }
    let collected = String::from_utf8(collected)
        .map_err(|_| "probe stream contained invalid UTF-8".to_string())?;
    if collected.contains("response.failed") {
        return Err("upstream reported response.failed".into());
    }
    Ok(collected)
}

pub fn responses_url(base_url: &str) -> String {
    let base = base_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return "http://127.0.0.1/responses".to_string();
    }
    if base.ends_with("/responses") {
        return base.to_string();
    }
    format!("{base}/responses")
}

pub fn sanitize_error(message: &str) -> String {
    sanitize_diagnostic(message, MAX_ERROR_CHARS)
}

#[cfg(test)]
#[path = "probe_tests.rs"]
mod tests;
