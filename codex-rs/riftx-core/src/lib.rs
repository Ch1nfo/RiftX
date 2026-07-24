//! RiftX domain, configuration, policy, and persistent state primitives.

mod audit;
mod authorization;
mod config;
mod conversation;
mod credential;
mod credential_use;
mod model;
mod objective;
mod policy;
mod state;
mod target_state;

pub use authorization::*;
pub use config::*;
pub use conversation::*;
pub use credential::*;
pub use credential_use::*;
pub use model::*;
pub use objective::*;
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
