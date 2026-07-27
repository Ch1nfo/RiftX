use crate::BridgeError;
use crate::diagnostics::sanitize_diagnostic;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;

/// One Responses SSE event ready to flush to the Runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResponsesSseEvent {
    pub event: String,
    pub data: Value,
}

impl ResponsesSseEvent {
    pub fn to_sse_frame(&self) -> String {
        format!("event: {}\ndata: {}\n\n", self.event, self.data)
    }
}

/// Convert Chat Completions SSE chunks into Responses SSE events.
#[derive(Debug, Default)]
pub struct ChatStreamConverter {
    response_id: String,
    created_emitted: bool,
    text_item_id: Option<String>,
    text_buffer: String,
    tool_calls: BTreeMap<u64, PendingToolCall>,
    finish_reason: Option<String>,
    usage: Option<Value>,
    completed: bool,
}

#[derive(Debug, Default, Clone)]
struct PendingToolCall {
    id: String,
    name: String,
    arguments: String,
    added: bool,
}

impl ChatStreamConverter {
    pub fn new(response_id: impl Into<String>) -> Self {
        Self {
            response_id: response_id.into(),
            ..Self::default()
        }
    }

    pub(crate) fn is_completed(&self) -> bool {
        self.completed
    }

    pub(crate) fn ingest_sse_frame(
        &mut self,
        frame: &str,
    ) -> Result<Vec<ResponsesSseEvent>, BridgeError> {
        if frame.trim().is_empty() || frame.trim_start().starts_with(':') {
            return Ok(Vec::new());
        }
        let Some(data) = sse_data_payload(frame) else {
            return Ok(Vec::new());
        };
        if data.trim() == "[DONE]" {
            let finish_reason = self.finish_reason.clone().ok_or_else(|| {
                BridgeError::Upstream(
                    "Chat Completions stream ended without a finish_reason".into(),
                )
            })?;
            return self.finish(&finish_reason);
        }
        let chunk: Value = serde_json::from_str(&data).map_err(|error| {
            BridgeError::Upstream(format!("invalid Chat Completions SSE JSON: {error}"))
        })?;
        if let Some(error) = chunk.get("error") {
            return Err(BridgeError::Upstream(sanitize_diagnostic(
                &error.to_string(),
                512,
            )));
        }
        self.ingest_chunk(&chunk)
    }

    pub fn ingest_chunk(&mut self, chunk: &Value) -> Result<Vec<ResponsesSseEvent>, BridgeError> {
        if self.completed {
            return Ok(Vec::new());
        }
        if let Some(usage) = chunk.get("usage") {
            self.usage = Some(convert_usage(usage)?);
        }

        let mut events = Vec::new();
        let choices = chunk
            .get("choices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if !choices.is_empty() && !self.created_emitted {
            self.created_emitted = true;
            if let Some(id) = chunk.get("id").and_then(Value::as_str) {
                self.response_id = id.to_string();
            }
            events.push(ResponsesSseEvent {
                event: "response.created".into(),
                data: json!({
                    "type": "response.created",
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "status": "in_progress",
                    }
                }),
            });
        }

        for choice in choices {
            if choice.get("index").and_then(Value::as_u64).unwrap_or(0) != 0 {
                return Err(BridgeError::Upstream(
                    "Chat Completions returned more than one choice".into(),
                ));
            }
            if let Some(delta) = choice.get("delta") {
                events.extend(self.ingest_delta(delta)?);
            }
            if let Some(finish_reason) = choice.get("finish_reason").and_then(Value::as_str) {
                self.finish_reason = Some(finish_reason.to_string());
            }
        }
        Ok(events)
    }

    fn ingest_delta(&mut self, delta: &Value) -> Result<Vec<ResponsesSseEvent>, BridgeError> {
        let mut events = Vec::new();
        if let Some(content) = delta.get("content").and_then(Value::as_str)
            && !content.is_empty()
        {
            if self.text_item_id.is_none() {
                let item_id = format!("msg_{}", self.response_id);
                self.text_item_id = Some(item_id.clone());
                events.push(ResponsesSseEvent {
                    event: "response.output_item.added".into(),
                    data: json!({
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "content": [],
                            "status": "in_progress",
                        }
                    }),
                });
            }
            self.text_buffer.push_str(content);
            events.push(ResponsesSseEvent {
                event: "response.output_text.delta".into(),
                data: json!({
                    "type": "response.output_text.delta",
                    "delta": content,
                }),
            });
        }

        if let Some(tool_calls) = delta.get("tool_calls").and_then(Value::as_array) {
            for tool_call in tool_calls {
                let index = tool_call.get("index").and_then(Value::as_u64).unwrap_or(0);
                let pending = self.tool_calls.entry(index).or_default();
                if let Some(id) = tool_call.get("id").and_then(Value::as_str)
                    && !id.is_empty()
                {
                    pending.id = id.to_string();
                }
                if let Some(name) = tool_call
                    .get("function")
                    .and_then(|function| function.get("name"))
                    .and_then(Value::as_str)
                    && !name.is_empty()
                {
                    pending.name = name.to_string();
                }
                if let Some(arguments) = tool_call
                    .get("function")
                    .and_then(|function| function.get("arguments"))
                    .and_then(Value::as_str)
                {
                    pending.arguments.push_str(arguments);
                }
                if !pending.added && !pending.id.is_empty() && !pending.name.is_empty() {
                    pending.added = true;
                    let item_id = format!("fc_{}", pending.id);
                    events.push(ResponsesSseEvent {
                        event: "response.output_item.added".into(),
                        data: json!({
                            "type": "response.output_item.added",
                            "item": {
                                "type": "function_call",
                                "id": item_id,
                                "call_id": pending.id,
                                "name": pending.name,
                                "arguments": "",
                                "status": "in_progress",
                            }
                        }),
                    });
                }
            }
        }
        Ok(events)
    }

    fn finish(&mut self, finish_reason: &str) -> Result<Vec<ResponsesSseEvent>, BridgeError> {
        if self.completed {
            return Ok(Vec::new());
        }
        self.completed = true;
        let item_status = match finish_reason {
            "stop" | "tool_calls" => "completed",
            "length" => "incomplete",
            "content_filter" => "failed",
            other => {
                return Err(BridgeError::Upstream(format!(
                    "unsupported Chat Completions finish_reason {other:?}"
                )));
            }
        };
        let mut events = Vec::new();

        if let Some(item_id) = self.text_item_id.clone() {
            events.push(ResponsesSseEvent {
                event: "response.output_item.done".into(),
                data: json!({
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": item_id,
                        "role": "assistant",
                        "content": [{
                            "type": "output_text",
                            "text": self.text_buffer,
                        }],
                        "status": item_status,
                    }
                }),
            });
        }

        for pending in self.tool_calls.values() {
            if pending.id.is_empty() || pending.name.is_empty() {
                return Err(BridgeError::Upstream(
                    "Chat Completions tool call missing id or name".into(),
                ));
            }
            if serde_json::from_str::<Value>(&pending.arguments).is_err() {
                return Err(BridgeError::Upstream(format!(
                    "Chat Completions tool call {} returned invalid JSON arguments",
                    pending.id
                )));
            }
            let item_id = format!("fc_{}", pending.id);
            if !pending.added {
                events.push(ResponsesSseEvent {
                    event: "response.output_item.added".into(),
                    data: json!({
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": pending.id,
                            "name": pending.name,
                            "arguments": "",
                            "status": "in_progress",
                        }
                    }),
                });
            }
            events.push(ResponsesSseEvent {
                event: "response.output_item.done".into(),
                data: json!({
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": pending.id,
                        "name": pending.name,
                        "arguments": pending.arguments,
                        "status": item_status,
                    }
                }),
            });
        }

        let mut response = json!({
            "id": self.response_id,
            "object": "response",
            "status": item_status,
        });
        if let Some(usage) = self.usage.clone() {
            response["usage"] = usage;
        }
        let terminal = match finish_reason {
            "stop" | "tool_calls" => ResponsesSseEvent {
                event: "response.completed".into(),
                data: json!({ "type": "response.completed", "response": response }),
            },
            "length" => {
                response["incomplete_details"] = json!({ "reason": "max_output_tokens" });
                ResponsesSseEvent {
                    event: "response.incomplete".into(),
                    data: json!({ "type": "response.incomplete", "response": response }),
                }
            }
            "content_filter" => {
                response["error"] = json!({
                    "code": "content_filter",
                    "message": "upstream content filter interrupted the response",
                });
                ResponsesSseEvent {
                    event: "response.failed".into(),
                    data: json!({ "type": "response.failed", "response": response }),
                }
            }
            _ => unreachable!("finish reason validated above"),
        };
        events.push(terminal);
        Ok(events)
    }
}

fn convert_usage(usage: &Value) -> Result<Value, BridgeError> {
    let object = usage
        .as_object()
        .ok_or_else(|| BridgeError::Upstream("Chat Completions usage must be an object".into()))?;
    let input_tokens = object
        .get("prompt_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let output_tokens = object
        .get("completion_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let total_tokens = object
        .get("total_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(input_tokens + output_tokens);
    let cached_tokens = object
        .get("prompt_tokens_details")
        .and_then(|details| details.get("cached_tokens"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let reasoning_tokens = object
        .get("completion_tokens_details")
        .and_then(|details| details.get("reasoning_tokens"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    Ok(json!({
        "input_tokens": input_tokens,
        "input_tokens_details": { "cached_tokens": cached_tokens },
        "output_tokens": output_tokens,
        "output_tokens_details": { "reasoning_tokens": reasoning_tokens },
        "total_tokens": total_tokens,
    }))
}

fn sse_data_payload(frame: &str) -> Option<String> {
    let mut data_lines = Vec::new();
    for line in frame.lines() {
        if let Some(rest) = line.strip_prefix("data:") {
            data_lines.push(rest.trim_start());
        }
    }
    (!data_lines.is_empty()).then(|| data_lines.join("\n"))
}

#[cfg(test)]
#[path = "stream_tests.rs"]
mod tests;
