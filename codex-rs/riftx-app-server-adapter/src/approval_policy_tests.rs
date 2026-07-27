use super::approval_policy_for_mode;
use codex_app_server_protocol::AskForApproval;
use codex_riftx_domain::ExecutionMode;
use pretty_assertions::assert_eq;

#[test]
fn maps_all_modes_to_always_ask_for_approval() {
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::Auto),
        AskForApproval::Always
    );
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::Pentest),
        AskForApproval::Always
    );
    assert_eq!(
        approval_policy_for_mode(ExecutionMode::RedTeam),
        AskForApproval::Always
    );
}
