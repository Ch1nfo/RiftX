use super::*;

#[test]
fn only_terminal_auto_states_cancel_the_wall_clock_budget() {
    assert!(!terminal_state(AutoRunState::Paused));
    assert!(!terminal_state(AutoRunState::NeedsInput));
    assert!(terminal_state(AutoRunState::Succeeded));
    assert!(terminal_state(AutoRunState::BudgetExhausted));
}
