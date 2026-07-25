//! Mode-scoped approval policies for local agent threads.

use codex_app_server_protocol::AskForApproval;
use codex_riftx_domain::ExecutionMode;

/// Map a RiftX engagement mode to the embedded runtime approval policy.
pub fn approval_policy_for_mode(mode: ExecutionMode) -> AskForApproval {
    match mode {
        ExecutionMode::Auto => AskForApproval::Never,
        ExecutionMode::Pentest | ExecutionMode::RedTeam => AskForApproval::UnlessTrusted,
    }
}

#[cfg(test)]
#[path = "approval_policy_tests.rs"]
mod tests;
