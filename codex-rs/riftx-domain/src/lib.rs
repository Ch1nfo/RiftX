//! Lightweight RiftX business-domain types shared by state, IPC, CLI, and Desktop.

mod authorization;
mod conversation;
mod model;
mod objective;

pub use authorization::*;
pub use conversation::*;
pub use model::*;
pub use objective::*;
