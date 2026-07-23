//! Local-only RiftX daemon transport.

mod client;
mod endpoint;
mod listener;
mod protocol;

pub use client::LocalIpcClient;
pub use client::LocalIpcError;
pub use client::LocalIpcResponse;
pub use endpoint::LocalIpcEndpoint;
pub use listener::LocalIpcListener;
pub use protocol::DaemonInfo;
pub use protocol::IPC_PROTOCOL_VERSION;

#[cfg(test)]
#[path = "ipc_tests.rs"]
mod tests;
