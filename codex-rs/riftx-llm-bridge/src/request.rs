use crate::BridgeError;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;

/// Build the upstream Chat Completions URL from a Profile `base_url`.
pub fn chat_completions_url(base_url: &str) -> String {
    let base = base_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return "http://127.0.0.1/chat/completions".to_string();
    }
    if base.ends_with("/chat/completions") {
        return base.to_string();
    }
    format!("{base}/chat/completions")
}

/// Convert a Responses API request body into a Chat Completions request body.
pub fn responses_request_to_chat(request: &Value) -> Result<Value, BridgeError> {
    let obj = request
        .as_object()
        .ok_or_else(|| BridgeError::InvalidRequest("request must be a JSON object".into()))?;

    reject_unmapped_fields(obj)?;

    let model = obj
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| BridgeError::InvalidRequest("model is required".into()))?;

    let mut messages = Vec::new();
    if let Some(instructions) = obj.get("instructions").and_then(Value::as_str)
        && !instructions.is_empty()
    {
        messages.push(json!({
            "role": "system",
            "content": instructions,
        }));
    }

    let input = obj
        .get("input")
        .and_then(Value::as_array)
        .ok_or_else(|| BridgeError::InvalidRequest("input must be an array".into()))?;
    append_input_items(input, &mut messages)?;

    let mut chat = json!({
        "model": model,
        "messages": messages,
        "stream": obj.get("stream").and_then(Value::as_bool).unwrap_or(true),
    });

    if let Some(tools) = obj.get("tools") {
        chat["tools"] = convert_tools(tools)?;
    }
    if let Some(tool_choice) = obj.get("tool_choice") {
        chat["tool_choice"] = convert_tool_choice(tool_choice)?;
    }
    if let Some(parallel) = obj.get("parallel_tool_calls").and_then(Value::as_bool) {
        chat["parallel_tool_calls"] = Value::Bool(parallel);
    }
    if let Some(max_output_tokens) = obj.get("max_output_tokens").and_then(Value::as_u64) {
        chat["max_tokens"] = Value::from(max_output_tokens);
    }

    Ok(chat)
}

fn reject_unmapped_fields(obj: &Map<String, Value>) -> Result<(), BridgeError> {
    const ALLOWED: &[&str] = &[
        "model",
        "instructions",
        "input",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
        "store",
        "include",
        "prompt_cache_key",
        "max_output_tokens",
        "temperature",
        "top_p",
        "metadata",
        "service_tier",
        "truncation",
        "user",
        "text",
        "reasoning",
    ];
    for key in obj.keys() {
        if !ALLOWED.contains(&key.as_str()) {
            return Err(BridgeError::Unsupported(format!(
                "Responses field {key:?} cannot be mapped to Chat Completions"
            )));
        }
    }
    if let Some(text) = obj.get("text")
        && !text.is_null()
        && text != &json!({})
    {
        // Structured text/json_schema formats are not safely mapped yet.
        if text.get("format").is_some() {
            return Err(BridgeError::Unsupported(
                "Responses text.format cannot be mapped to Chat Completions yet".into(),
            ));
        }
    }
    Ok(())
}

fn append_input_items(input: &[Value], messages: &mut Vec<Value>) -> Result<(), BridgeError> {
    let mut pending_tool_calls: Vec<Value> = Vec::new();

    let flush_tool_calls = |pending: &mut Vec<Value>, messages: &mut Vec<Value>| {
        if pending.is_empty() {
            return;
        }
        messages.push(json!({
            "role": "assistant",
            "content": Value::Null,
            "tool_calls": std::mem::take(pending),
        }));
    };

    for item in input {
        let item_type = item
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| BridgeError::InvalidRequest("input item missing type".into()))?;
        match item_type {
            "message" => {
                flush_tool_calls(&mut pending_tool_calls, messages);
                messages.push(convert_message_item(item)?);
            }
            "function_call" => {
                pending_tool_calls.push(convert_function_call_item(item)?);
            }
            "function_call_output" => {
                flush_tool_calls(&mut pending_tool_calls, messages);
                messages.push(convert_function_call_output_item(item)?);
            }
            "reasoning" => {
                // Reasoning summaries are not required for Chat Completions tool loops.
            }
            other => {
                return Err(BridgeError::Unsupported(format!(
                    "Responses input type {other:?} cannot be mapped to Chat Completions"
                )));
            }
        }
    }
    flush_tool_calls(&mut pending_tool_calls, messages);
    Ok(())
}

fn convert_message_item(item: &Value) -> Result<Value, BridgeError> {
    let role = item
        .get("role")
        .and_then(Value::as_str)
        .ok_or_else(|| BridgeError::InvalidRequest("message item missing role".into()))?;
    let role = match role {
        "user" | "assistant" | "system" | "developer" => {
            if role == "developer" {
                "system"
            } else {
                role
            }
        }
        other => {
            return Err(BridgeError::Unsupported(format!(
                "message role {other:?} cannot be mapped to Chat Completions"
            )));
        }
    };
    let content = message_text_content(item)?;
    Ok(json!({
        "role": role,
        "content": content,
    }))
}

fn message_text_content(item: &Value) -> Result<String, BridgeError> {
    let content = item
        .get("content")
        .and_then(Value::as_array)
        .ok_or_else(|| BridgeError::InvalidRequest("message content must be an array".into()))?;
    let mut parts = Vec::new();
    for part in content {
        let part_type = part.get("type").and_then(Value::as_str).unwrap_or("");
        match part_type {
            "input_text" | "output_text" | "text" => {
                if let Some(text) = part.get("text").and_then(Value::as_str) {
                    parts.push(text.to_string());
                }
            }
            "input_image" | "output_image" | "input_file" | "input_audio" => {
                return Err(BridgeError::Unsupported(format!(
                    "message content type {part_type:?} is not supported for Chat Completions profiles"
                )));
            }
            other if other.is_empty() => {}
            other => {
                return Err(BridgeError::Unsupported(format!(
                    "message content type {other:?} cannot be mapped to Chat Completions"
                )));
            }
        }
    }
    Ok(parts.join(""))
}

fn convert_function_call_item(item: &Value) -> Result<Value, BridgeError> {
    let call_id = item
        .get("call_id")
        .and_then(Value::as_str)
        .ok_or_else(|| BridgeError::InvalidRequest("function_call missing call_id".into()))?;
    let name = item
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| BridgeError::InvalidRequest("function_call missing name".into()))?;
    if item.get("namespace").and_then(Value::as_str).is_some() {
        return Err(BridgeError::Unsupported(
            "namespaced function_call tools cannot be mapped to Chat Completions".into(),
        ));
    }
    let arguments = item
        .get("arguments")
        .and_then(Value::as_str)
        .unwrap_or("{}");
    Ok(json!({
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        }
    }))
}

fn convert_function_call_output_item(item: &Value) -> Result<Value, BridgeError> {
    let call_id = item.get("call_id").and_then(Value::as_str).ok_or_else(|| {
        BridgeError::InvalidRequest("function_call_output missing call_id".into())
    })?;
    let content = match item.get("output") {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| part.get("text").and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join(""),
        Some(Value::Object(obj)) => obj
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        Some(other) => other.to_string(),
        None => String::new(),
    };
    Ok(json!({
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }))
}

fn convert_tools(tools: &Value) -> Result<Value, BridgeError> {
    let tools = tools
        .as_array()
        .ok_or_else(|| BridgeError::InvalidRequest("tools must be an array".into()))?;
    let mut converted = Vec::with_capacity(tools.len());
    for tool in tools {
        let tool_type = tool.get("type").and_then(Value::as_str).unwrap_or("");
        match tool_type {
            "function" => {
                let name = tool.get("name").and_then(Value::as_str).ok_or_else(|| {
                    BridgeError::InvalidRequest("function tool missing name".into())
                })?;
                let mut function = json!({
                    "name": name,
                });
                if let Some(description) = tool.get("description") {
                    function["description"] = description.clone();
                }
                if let Some(parameters) = tool.get("parameters") {
                    function["parameters"] = parameters.clone();
                }
                if let Some(strict) = tool.get("strict") {
                    function["strict"] = strict.clone();
                }
                converted.push(json!({
                    "type": "function",
                    "function": function,
                }));
            }
            other => {
                return Err(BridgeError::Unsupported(format!(
                    "tool type {other:?} cannot be mapped to Chat Completions function tools"
                )));
            }
        }
    }
    Ok(Value::Array(converted))
}

fn convert_tool_choice(tool_choice: &Value) -> Result<Value, BridgeError> {
    match tool_choice {
        Value::String(value) => Ok(Value::String(value.clone())),
        Value::Object(obj) => {
            if obj.get("type").and_then(Value::as_str) == Some("function") {
                let name = obj
                    .get("name")
                    .and_then(Value::as_str)
                    .or_else(|| {
                        obj.get("function")
                            .and_then(|function| function.get("name"))
                            .and_then(Value::as_str)
                    })
                    .ok_or_else(|| {
                        BridgeError::InvalidRequest("tool_choice function missing name".into())
                    })?;
                Ok(json!({
                    "type": "function",
                    "function": { "name": name }
                }))
            } else {
                Err(BridgeError::Unsupported(
                    "unsupported Responses tool_choice object".into(),
                ))
            }
        }
        _ => Err(BridgeError::InvalidRequest(
            "tool_choice must be a string or object".into(),
        )),
    }
}

#[cfg(test)]
#[path = "request_tests.rs"]
mod tests;
