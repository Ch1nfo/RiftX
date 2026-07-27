const REDACTED: &str = "[REDACTED]";

pub(crate) fn sanitize_diagnostic(message: &str, max_chars: usize) -> String {
    let mut sanitized = message.to_string();
    for marker in ["authorization:", "authorization=", "\"authorization\":"] {
        redact_to_line_end(&mut sanitized, marker);
    }
    for marker in [
        "bearer ",
        "api_key:",
        "api_key=",
        "\"api_key\":",
        "api-key:",
        "api-key=",
        "\"api-key\":",
        "x-api-key:",
        "x-api-key=",
        "\"x-api-key\":",
    ] {
        redact_values_after(&mut sanitized, marker);
    }
    redact_token_prefix(&mut sanitized, "sk-");
    truncate_chars(sanitized.trim(), max_chars)
}

fn redact_to_line_end(value: &mut String, marker: &str) {
    let marker_lower = marker.to_ascii_lowercase();
    let mut search_from = 0;
    loop {
        let lower = value[search_from..].to_ascii_lowercase();
        let Some(relative_start) = lower.find(&marker_lower) else {
            break;
        };
        let start = search_from + relative_start;
        let end = value[start..]
            .find(['\r', '\n'])
            .map_or(value.len(), |offset| start + offset);
        value.replace_range(start..end, REDACTED);
        search_from = start + REDACTED.len();
    }
}

fn redact_values_after(value: &mut String, marker: &str) {
    let marker_lower = marker.to_ascii_lowercase();
    let mut search_from = 0;
    loop {
        let lower = value[search_from..].to_ascii_lowercase();
        let Some(relative_start) = lower.find(&marker_lower) else {
            break;
        };
        let start = search_from + relative_start;
        let secret_start = start + marker.len();
        let secret_start = value[secret_start..]
            .char_indices()
            .find_map(|(offset, ch)| {
                (!ch.is_whitespace() && ch != '"').then_some(secret_start + offset)
            })
            .unwrap_or(value.len());
        let secret_end = value[secret_start..]
            .char_indices()
            .find_map(|(offset, ch)| {
                (ch.is_whitespace() || matches!(ch, '"' | '\'' | ',' | '}' | ']'))
                    .then_some(secret_start + offset)
            })
            .unwrap_or(value.len());
        value.replace_range(start..secret_end, REDACTED);
        search_from = start + REDACTED.len();
    }
}

fn redact_token_prefix(value: &mut String, prefix: &str) {
    let mut search_from = 0;
    loop {
        let Some(relative_start) = value[search_from..].find(prefix) else {
            break;
        };
        let start = search_from + relative_start;
        let end = value[start..]
            .char_indices()
            .skip(1)
            .find_map(|(offset, ch)| {
                (ch.is_whitespace() || matches!(ch, '"' | '\'' | ',' | '}' | ']'))
                    .then_some(start + offset)
            })
            .unwrap_or(value.len());
        value.replace_range(start..end, REDACTED);
        search_from = start + REDACTED.len();
    }
}

fn truncate_chars(value: &str, max_chars: usize) -> String {
    let mut chars = value.chars();
    let head = chars.by_ref().take(max_chars).collect::<String>();
    if chars.next().is_some() {
        format!("{head}…")
    } else {
        head
    }
}

#[cfg(test)]
#[path = "diagnostics_tests.rs"]
mod tests;
