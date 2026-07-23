use super::*;

#[tokio::test]
async fn records_are_appended_as_json_lines() {
    let temp = tempfile::tempdir().expect("temporary directory");
    let path = temp.path().join("audit/events.jsonl");
    let writer = AuditWriter::new(&AuditConfig {
        jsonl_path: path.clone(),
        fsync: false,
    });
    let record = AuditRecord {
        timestamp: 1,
        event: "tool/completed".to_string(),
        engagement_id: "eng-1".to_string(),
        thread_id: Some("thread-1".to_string()),
        turn_id: Some("turn-1".to_string()),
        tool_call_id: Some("call-1".to_string()),
        sandbox_id: Some("sandbox-1".to_string()),
        profile: Some("recon".to_string()),
        policy_revision: Some("revision-1".to_string()),
        outcome: Some("success".to_string()),
    };

    writer.append(&record).await.expect("first append");
    writer.append(&record).await.expect("second append");

    let content = tokio::fs::read_to_string(path).await.expect("audit file");
    assert_eq!(content.lines().count(), 2);
    assert_eq!(
        serde_json::from_str::<AuditRecord>(content.lines().next().expect("first record"))
            .expect("valid record"),
        record
    );
}
