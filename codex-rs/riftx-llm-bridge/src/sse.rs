use crate::BridgeError;

const MAX_BUFFER_BYTES: usize = 1024 * 1024;

#[derive(Debug, Default)]
pub(crate) struct SseDecoder {
    buffer: Vec<u8>,
}

impl SseDecoder {
    pub(crate) fn push(&mut self, chunk: &[u8]) -> Result<Vec<String>, BridgeError> {
        self.buffer.extend_from_slice(chunk);
        if self.buffer.len() > MAX_BUFFER_BYTES {
            return Err(BridgeError::Upstream(
                "Chat Completions SSE frame exceeded the 1 MiB limit".into(),
            ));
        }

        let mut frames = Vec::new();
        while let Some((frame_end, delimiter_len)) = find_frame_boundary(&self.buffer) {
            let consumed = self
                .buffer
                .drain(..frame_end + delimiter_len)
                .collect::<Vec<_>>();
            let frame = std::str::from_utf8(&consumed[..frame_end]).map_err(|_| {
                BridgeError::Upstream("Chat Completions SSE contained invalid UTF-8".into())
            })?;
            frames.push(frame.to_string());
        }
        Ok(frames)
    }

    pub(crate) fn finish(&mut self) -> Result<(), BridgeError> {
        if self.buffer.iter().all(u8::is_ascii_whitespace) {
            self.buffer.clear();
            return Ok(());
        }
        Err(BridgeError::Upstream(
            "Chat Completions stream ended with an incomplete SSE frame".into(),
        ))
    }
}

fn find_frame_boundary(buffer: &[u8]) -> Option<(usize, usize)> {
    let lf = buffer.windows(2).position(|window| window == b"\n\n");
    let crlf = buffer.windows(4).position(|window| window == b"\r\n\r\n");
    match (lf, crlf) {
        (Some(lf), Some(crlf)) if lf <= crlf => Some((lf, 2)),
        (Some(_), Some(crlf)) => Some((crlf, 4)),
        (Some(lf), None) => Some((lf, 2)),
        (None, Some(crlf)) => Some((crlf, 4)),
        (None, None) => None,
    }
}

#[cfg(test)]
#[path = "sse_tests.rs"]
mod tests;
