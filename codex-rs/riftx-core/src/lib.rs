//! RiftX domain, configuration, policy, and persistent state primitives.

mod audit;
mod config;
mod credential;
mod credential_use;
mod policy;
mod state;
mod target_state;

pub use codex_riftx_domain::*;
pub use config::*;
pub use credential::*;
pub use credential_use::*;
pub use policy::*;
pub use state::*;
pub use target_state::*;

#[cfg(test)]
#[path = "config_tests.rs"]
mod tests;
pub use audit::*;

#[cfg(test)]
#[path = "conversation_tests.rs"]
mod conversation_tests;
