use super::*;
use pretty_assertions::assert_eq;

#[test]
fn tray_task_status_uses_operator_attention_priority() {
    assert_eq!(
        task_attention(TaskActivitySummary {
            ready: 4,
            running: 3,
            waiting: 2,
            risk: 1,
        }),
        TaskAttention::Risk(1)
    );
    assert_eq!(
        task_attention(TaskActivitySummary {
            ready: 4,
            running: 3,
            waiting: 2,
            risk: 0,
        }),
        TaskAttention::Waiting(2)
    );
    assert_eq!(
        task_attention(TaskActivitySummary {
            ready: 4,
            running: 3,
            waiting: 0,
            risk: 0,
        }),
        TaskAttention::Running(3)
    );
    assert_eq!(
        task_attention(TaskActivitySummary {
            ready: 4,
            ..TaskActivitySummary::default()
        }),
        TaskAttention::Ready(4)
    );
    assert_eq!(
        task_attention(TaskActivitySummary::default()),
        TaskAttention::None
    );
}
