use super::approval_policy_for_mode;
use codex_app_server_protocol::AskForApproval;
use codex_riftx_domain::ExecutionMode;
use pretty_assertions::assert_eq;

#[test]
fn maps_modes_to_tiered_ask_for_approval_policies() {
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::Auto),
        AskForApproval::Never
    );
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::Pentest),
        AskForApproval::UnlessTrusted
    );
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::RedTeam),
        AskForApproval::UnlessTrusted
    );
}
