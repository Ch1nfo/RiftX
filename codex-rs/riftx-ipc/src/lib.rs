//! Local-only RiftX daemon transport.

mod client;
mod endpoint;
mod listener;
mod protocol;

pub use client::LocalIpcClient;
pub use client::LocalIpcError;
pub use client::LocalIpcResponse;
pub use client::LocalSseEvent;
pub use client::LocalSseStream;
pub use endpoint::LocalIpcEndpoint;
pub use listener::LocalIpcListener;
pub use protocol::ApprovalDecision;
pub use protocol::ApprovalKind;
pub use protocol::DaemonControlStatus;
pub use protocol::DaemonInfo;
pub use protocol::DaemonPauseReason;
pub use protocol::DaemonRunState;
pub use protocol::EngagementEvent;
pub use protocol::IPC_PROTOCOL_VERSION;
pub use protocol::PendingApproval;

#[cfg(test)]
#[path = "ipc_tests.rs"]
mod tests;
