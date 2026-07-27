//! RiftX LLM Bridge: translate OpenAI Responses requests into Chat Completions.
//!
//! The embedded Agent Runtime only speaks Responses (`POST /v1/responses`). For
//! Chat Completions providers, `riftxd` starts a loopback bridge that:
//! - accepts Responses requests authenticated with a per-daemon bearer token;
//! - forwards to the upstream Chat Completions endpoint with the Profile API key;
//! - streams Responses-compatible SSE events back to the Runtime.
//!
//! The bridge never logs Authorization headers, API keys, or full prompt bodies.

mod error;
mod request;
mod server;
mod stream;

pub use error::BridgeError;
pub use request::chat_completions_url;
pub use request::responses_request_to_chat;
pub use server::BridgeHandle;
pub use server::BridgeUpstream;
pub use server::start_loopback_bridge;
pub use stream::ChatStreamConverter;
pub use stream::ResponsesSseEvent;

#[cfg(test)]
#[path = "server_tests.rs"]
mod server_tests;
