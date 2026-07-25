use super::*;
use crate::AssessmentObjective;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::Engagement;
use crate::EngagementStatus;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::Scope;
use crate::StateStore;
use codex_riftx_crypto::KeyringEngagementCipher;
use ipnet::IpNet;
use pretty_assertions::assert_eq;
use std::sync::Arc;
use tempfile::TempDir;

#[tokio::test]
async fn engagement_records_are_encrypted_and_system_records_remain_readable() {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open_with_cipher(
        &temp.path().join("state.sqlite"),
        Arc::new(KeyringEngagementCipher::new(
            codex_keyring_store::tests::MockKeyringStore::default(),
        )),
    )
    .await
    .expect("state store");
    let engagement = engagement();
    store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let path = temp.path().join("audit.jsonl");
    let writer = store.audit_writer(&AuditConfig {
        jsonl_path: path.clone(),
        fsync: true,
    });
    let engagement_record = AuditRecord {
        timestamp: 1,
        event: "engagement/created".to_string(),
        engagement_id: engagement.id,
        thread_id: None,
        turn_id: None,
        tool_call_id: None,
        mode: Some(ExecutionMode::Pentest),
        policy_revision: Some("policy-1".to_string()),
        outcome: Some("success".to_string()),
        details: Some(serde_json::json!({"marker": "sensitive-audit-detail"})),
    };
    let system_record = AuditRecord {
        timestamp: 2,
        event: "daemon/paused".to_string(),
        engagement_id: SYSTEM_AUDIT_ID.to_string(),
        thread_id: None,
        turn_id: None,
        tool_call_id: None,
        mode: None,
        policy_revision: None,
        outcome: Some("success".to_string()),
        details: None,
    };

    writer
        .append(&engagement_record)
        .await
        .expect("append engagement audit");
    writer
        .append(&system_record)
        .await
        .expect("append system audit");

    let content = tokio::fs::read_to_string(path).await.expect("audit file");
    assert!(content.contains(ENCRYPTED_AUDIT_FORMAT));
    assert!(!content.contains("engagement/created"));
    assert!(!content.contains("sensitive-audit-detail"));
    assert!(content.contains("daemon/paused"));
    assert_eq!(
        writer
            .read_records(/*limit*/ 10)
            .await
            .expect("read audit records"),
        vec![engagement_record.clone(), system_record]
    );
    assert!(matches!(
        writer
            .decode_record(&serde_json::to_vec(&engagement_record).expect("serialize audit"))
            .await,
        Err(AuditError::UnencryptedEngagementRecord)
    ));
}

#[tokio::test]
async fn tampered_encrypted_audit_record_is_rejected() {
    let temp = TempDir::new().expect("temp dir");
    let store = StateStore::open_with_cipher(
        &temp.path().join("state.sqlite"),
        Arc::new(KeyringEngagementCipher::new(
            codex_keyring_store::tests::MockKeyringStore::default(),
        )),
    )
    .await
    .expect("state store");
    let engagement = engagement();
    store
        .put_engagement(&engagement)
        .await
        .expect("store engagement");
    let path = temp.path().join("audit.jsonl");
    let writer = store.audit_writer(&AuditConfig {
        jsonl_path: path.clone(),
        fsync: false,
    });
    writer
        .append(&AuditRecord {
            timestamp: 1,
            event: "engagement/created".to_string(),
            engagement_id: engagement.id,
            thread_id: None,
            turn_id: None,
            tool_call_id: None,
            mode: Some(ExecutionMode::Pentest),
            policy_revision: None,
            outcome: Some("success".to_string()),
            details: None,
        })
        .await
        .expect("append audit");

    let content = tokio::fs::read_to_string(&path).await.expect("audit file");
    let mut line: EncryptedAuditLine =
        serde_json::from_str(content.trim()).expect("encrypted audit line");
    let mut envelope = STANDARD_NO_PAD
        .decode(&line.envelope)
        .expect("audit envelope");
    let last = envelope.len() - 1;
    envelope[last] ^= 1;
    line.envelope = STANDARD_NO_PAD.encode(envelope);
    tokio::fs::write(
        &path,
        format!(
            "{}\n",
            serde_json::to_string(&line).expect("serialize tampered audit"),
        ),
    )
    .await
    .expect("tamper audit");

    assert!(matches!(
        writer.read_records(/*limit*/ 10).await,
        Err(AuditError::Crypto(CryptoError::AuthenticationFailed))
    ));
}

fn engagement() -> Engagement {
    Engagement {
        id: "eng-audit".to_string(),
        name: "Audit".to_string(),
        objective: AssessmentObjective {
            summary: "Record encrypted audit".to_string(),
            success_criteria: vec!["Audit can be replayed".to_string()],
            structured_criteria: Vec::new(),
        },
        status: EngagementStatus::Draft,
        entry_points: Vec::new(),
        mode: ExecutionMode::Pentest,
        llm_profile: "default".to_string(),
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: vec!["10.10.0.0/24".parse::<IpNet>().expect("CIDR")],
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: vec!["audit.read".to_string()],
            environment: EnvironmentClass::Lab,
            window: AuthorizationWindow {
                starts_at: None,
                expires_at: Some(100),
            },
        },
        policy_revision: "policy-1".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    }
}
