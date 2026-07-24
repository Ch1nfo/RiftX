use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

fn event(engagement_id: &str, kind: &str, data: serde_json::Value) -> EngagementEvent {
    EngagementEvent {
        engagement_id: engagement_id.to_string(),
        kind: kind.to_string(),
        timestamp: 1,
        data,
    }
}

#[test]
fn activity_book_tracks_all_active_engagements() {
    let mut activity = ActivityBook::default();
    activity.sync(&HashSet::from([
        "ready".to_string(),
        "running".to_string(),
        "waiting".to_string(),
        "risk".to_string(),
    ]));
    activity.record(&event("running", "turnStarted", json!({})));
    activity.record(&event(
        "waiting",
        "turnStarted",
        json!({"taskId": "task-1"}),
    ));
    activity.record(&event(
        "waiting",
        "approval/command",
        json!({"approvalId": "approval-1"}),
    ));
    activity.record(&event("risk", "appServer/closed", json!({})));

    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            ready: 1,
            running: 1,
            waiting: 1,
            risk: 1,
        }
    );
}

#[test]
fn activity_book_deduplicates_approvals_and_removes_inactive_tasks() {
    let mut activity = ActivityBook::default();
    activity.sync(&HashSet::from([
        "waiting".to_string(),
        "removed".to_string(),
    ]));
    let pending = event(
        "waiting",
        "approval/command",
        json!({"approvalId": "approval-1"}),
    );
    activity.record(&pending);
    activity.record(&pending);
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            waiting: 1,
            ready: 1,
            ..TaskActivitySummary::default()
        }
    );

    activity.record(&event(
        "waiting",
        "approvalDecided",
        json!({"approvalId": "approval-1"}),
    ));
    activity.sync(&HashSet::from(["waiting".to_string()]));
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            ready: 1,
            ..TaskActivitySummary::default()
        }
    );
}

#[test]
fn disconnect_and_interrupt_are_risk_states() {
    let mut activity = ActivityBook::default();
    activity.sync(&HashSet::from(["task".to_string()]));
    activity.disconnected("task");
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            risk: 1,
            ..TaskActivitySummary::default()
        }
    );

    activity.connected("task");
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            ready: 1,
            ..TaskActivitySummary::default()
        }
    );

    activity.record(&event("task", "engagementInterrupted", json!({})));
    activity.connected("task");
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            risk: 1,
            ..TaskActivitySummary::default()
        }
    );

    activity.record(&event("task", "engagementActivated", json!({})));
    assert_eq!(
        activity.summary(),
        TaskActivitySummary {
            ready: 1,
            ..TaskActivitySummary::default()
        }
    );
}

#[test]
fn stream_status_survives_listener_registration_and_removes_inactive_tasks() {
    let mut streams = StreamStatusBook::default();
    streams.sync(&HashSet::from([
        "selected".to_string(),
        "removed".to_string(),
    ]));
    streams.record(DesktopStreamStatus {
        engagement_id: "selected".to_string(),
        state: "connected",
        message: None,
    });
    assert_eq!(
        streams.status("selected"),
        DesktopStreamStatus {
            engagement_id: "selected".to_string(),
            state: "connected",
            message: None,
        }
    );

    streams.sync(&HashSet::from(["selected".to_string()]));
    assert_eq!(
        streams.status("removed"),
        DesktopStreamStatus {
            engagement_id: "removed".to_string(),
            state: "disconnected",
            message: None,
        }
    );
}
