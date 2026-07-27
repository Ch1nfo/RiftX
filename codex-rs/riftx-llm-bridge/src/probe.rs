//! Connection probes against Responses or Chat Completions endpoints.
//!
//! Probes never return Authorization headers, API keys, or full response bodies.

use crate::BridgeUpstream;
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
                stream_text: ProbeLayerResult {
                    ok: false,
                    detail,
                },
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
                stream_text: ProbeLayerResult {
                    ok: false,
                    detail,
                },
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
    if sse.contains("\"type\":\"function_call\"")
        || sse.contains(r#""type": "function_call""#)
        || sse.contains(TEST_TOOL_NAME)
    {
        return Ok(());
    }
    Err("stream completed without a function_call item".into())
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
        let snippet = response
            .text()
            .await
            .unwrap_or_default()
            .chars()
            .take(120)
            .collect::<String>();
        return Err(format!(
            "upstream returned HTTP {}{}",
            status.as_u16(),
            if snippet.trim().is_empty() {
                String::new()
            } else {
                format!(" ({})", redact_secrets(&snippet))
            }
        ));
    }

    let mut stream = response.bytes_stream();
    let mut collected = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| format!("stream read failed: {error}"))?;
        collected.push_str(&String::from_utf8_lossy(&chunk));
        if collected.len() > MAX_SSE_BYTES {
            return Err("stream exceeded probe size limit".into());
        }
        if collected.contains("response.completed")
            || collected.contains("response.failed")
            || collected.contains("[DONE]")
        {
            break;
        }
    }
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
    let redacted = redact_secrets(message);
    truncate(&redacted, MAX_ERROR_CHARS)
}

fn redact_secrets(message: &str) -> String {
    let mut output = message.to_string();
    for needle in [
        "Authorization:",
        "authorization:",
        "Bearer ",
        "bearer ",
        "api_key",
        "api-key",
        "sk-",
    ] {
        if let Some(index) = output
            .to_ascii_lowercase()
            .find(&needle.to_ascii_lowercase())
        {
            let end = (index + needle.len() + 24).min(output.len());
            output.replace_range(index..end, "[REDACTED]");
        }
    }
    output
}

fn truncate(value: &str, max_chars: usize) -> String {
    let mut chars = value.chars();
    let head: String = chars.by_ref().take(max_chars).collect();
    if chars.next().is_some() {
        format!("{head}…")
    } else {
        head
    }
}

#[cfg(test)]
#[path = "probe_tests.rs"]
mod tests;
