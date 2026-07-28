use super::*;
use pretty_assertions::assert_eq;
use serde_json::Value;

fn event(kind: &str, timestamp: i64) -> EngagementEvent {
    EngagementEvent {
        engagement_id: "engagement-1".to_string(),
        kind: kind.to_string(),
        timestamp,
        data: Value::Null,
    }
}

#[test]
fn notification_copy_excludes_event_payloads() {
    assert_eq!(
        notification_copy("approval/command"),
        Some(NotificationCopy {
            title: "RiftX approval required",
            body: "An active task is waiting for a command decision.",
        })
    );
    assert_eq!(notification_copy("execution/outputDelta"), None);
}

#[test]
fn duplicate_events_are_claimed_once_with_a_bounded_cache() {
    let manager = NotificationManager::default();
    let approval = event("approval/command", 1);
    assert!(manager.claim(&approval));
    assert!(!manager.claim(&approval));

    for timestamp in 2..=RECENT_NOTIFICATION_LIMIT as i64 + 2 {
        assert!(manager.claim(&event("turn/completed", timestamp)));
    }
    assert!(manager.claim(&approval));
    assert_eq!(
        manager.recent.lock().expect("notification cache").len(),
        RECENT_NOTIFICATION_LIMIT
    );
}
