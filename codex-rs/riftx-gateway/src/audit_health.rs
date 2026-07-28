use crate::gateway_state::DAEMON_CONTROL_STATE_KEY;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_core::AuditError;
use codex_riftx_core::AuditRecord;
use codex_riftx_core::Engagement;
use codex_riftx_ipc::AuditHealthState;
use codex_riftx_ipc::AuditHealthStatus;
use codex_riftx_ipc::EngagementEvent;
use serde_json::Value;

const AUDIT_APPEND_UNAVAILABLE: &str = "audit log cannot be written";
const AUDIT_ENCRYPTION_UNAVAILABLE: &str = "audit encryption is unavailable";

impl GatewayState {
    pub(crate) async fn publish(&self, engagement_id: &str, kind: &str, data: Value) {
        if let Ok(engagement) = self.store.engagement(engagement_id).await {
            let record = audit_record(&engagement, kind, &data);
            let _ = self.append_audit_record(&record).await;
        }
        self.emit_event(engagement_id, kind, data).await;
    }

    pub(crate) async fn append_system_critical(
        &self,
        kind: &str,
        data: Value,
    ) -> Result<(), AuditError> {
        self.append_audit_record(&AuditRecord {
            timestamp: unix_timestamp(),
            event: kind.to_string(),
            engagement_id: "system".to_string(),
            thread_id: None,
            turn_id: None,
            tool_call_id: None,
            mode: None,
            policy_revision: None,
            outcome: Some("success".to_string()),
            details: Some(data),
        })
        .await
    }

    /// Durably records a security-critical event before exposing the event or allowing work.
    pub(crate) async fn publish_critical(
        &self,
        engagement: &Engagement,
        kind: &str,
        data: Value,
    ) -> Result<(), AuditError> {
        self.append_engagement_critical(engagement, kind, &data)
            .await?;
        self.emit_event(&engagement.id, kind, data).await;
        Ok(())
    }

    pub(crate) async fn append_engagement_critical(
        &self,
        engagement: &Engagement,
        kind: &str,
        data: &Value,
    ) -> Result<(), AuditError> {
        self.append_audit_record(&audit_record(engagement, kind, data))
            .await
    }

    async fn append_audit_record(&self, record: &AuditRecord) -> Result<(), AuditError> {
        match self.audit.append(record).await {
            Ok(()) => {
                let encryption_is_still_unverified = record.engagement_id == "system"
                    && self.control.read().await.audit.message.as_deref()
                        == Some(AUDIT_ENCRYPTION_UNAVAILABLE);
                if !encryption_is_still_unverified {
                    self.set_audit_health(AuditHealthState::Healthy, None)
                        .await?;
                }
                Ok(())
            }
            Err(error) => {
                self.set_audit_health(
                    AuditHealthState::Degraded,
                    Some(public_audit_error(&error).to_string()),
                )
                .await?;
                Err(error)
            }
        }
    }

    async fn set_audit_health(
        &self,
        state: AuditHealthState,
        message: Option<String>,
    ) -> Result<(), AuditError> {
        let _permit = self
            .control_write_slot
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| audit_health_persistence_error())?;
        let mut snapshot = self.control.read().await.clone();
        let unchanged = snapshot.audit.state == state && snapshot.audit.message == message;
        if unchanged && state == AuditHealthState::Healthy {
            return Ok(());
        }
        if !unchanged {
            snapshot.audit = AuditHealthStatus {
                state,
                message,
                updated_at: unix_timestamp(),
            };
        }
        if state == AuditHealthState::Degraded {
            *self.control.write().await = snapshot.clone();
        }
        self.store
            .put_system_state(DAEMON_CONTROL_STATE_KEY, &snapshot)
            .await
            .map_err(|_| audit_health_persistence_error())?;
        if state == AuditHealthState::Healthy {
            *self.control.write().await = snapshot;
        }
        Ok(())
    }

    pub(crate) async fn emit_event(&self, engagement_id: &str, kind: &str, data: Value) {
        let sender = self.event_sender(engagement_id).await;
        let _ = sender.send(EngagementEvent {
            engagement_id: engagement_id.to_string(),
            kind: kind.to_string(),
            timestamp: unix_timestamp(),
            data,
        });
    }
}

fn audit_record(engagement: &Engagement, kind: &str, data: &Value) -> AuditRecord {
    AuditRecord {
        timestamp: unix_timestamp(),
        event: kind.to_string(),
        engagement_id: engagement.id.clone(),
        thread_id: first_string(data, &["/threadId", "/payload/threadId"])
            .or_else(|| engagement.thread_id.clone()),
        turn_id: first_string(
            data,
            &[
                "/turnId",
                "/execution/turnId",
                "/payload/turnId",
                "/payload/turn/id",
            ],
        ),
        tool_call_id: first_string(
            data,
            &[
                "/toolCallId",
                "/callId",
                "/payload/toolCallId",
                "/payload/callId",
                "/useId",
                "/usage/id",
                "/id",
            ],
        ),
        mode: Some(engagement.mode),
        policy_revision: Some(engagement.policy_revision.clone()),
        outcome: event_outcome(kind, data),
        details: (kind.starts_with("execution/")
            || kind.starts_with("approval")
            || kind.starts_with("credential/use")
            || kind.starts_with("tool/credential")
            || kind.starts_with("artifact/")
            || kind == "engagement/modeChanged")
            .then(|| data.clone()),
    }
}

fn first_string(data: &Value, pointers: &[&str]) -> Option<String> {
    pointers
        .iter()
        .find_map(|pointer| data.pointer(pointer).and_then(Value::as_str))
        .map(str::to_string)
}

fn event_outcome(kind: &str, data: &Value) -> Option<String> {
    first_string(data, &["/outcome", "/status", "/decision"]).or_else(|| {
        if kind.ends_with("/completed") || kind.ends_with("Completed") {
            Some("success".to_string())
        } else if kind.ends_with("/failed") || kind.ends_with("/rejected") {
            Some("failure".to_string())
        } else {
            None
        }
    })
}

fn public_audit_error(error: &AuditError) -> &'static str {
    match error {
        AuditError::CreateDirectory(_) => "audit log directory is unavailable",
        AuditError::Append(_) => AUDIT_APPEND_UNAVAILABLE,
        AuditError::Read(_) => "audit log cannot be read",
        AuditError::Serialize(_) => "audit record serialization failed",
        AuditError::Decode(_) => "audit record decoding failed",
        AuditError::Crypto(_) | AuditError::CryptoTask(_) => AUDIT_ENCRYPTION_UNAVAILABLE,
        AuditError::UnsupportedFormat => "audit log format is unsupported",
        AuditError::UnencryptedEngagementRecord => "audit log encryption invariant failed",
        AuditError::LineTooLarge => "audit record exceeds the size limit",
        AuditError::InvalidReadLimit => "audit read request is invalid",
    }
}

fn audit_health_persistence_error() -> AuditError {
    AuditError::Append(std::io::Error::other(
        "audit health state cannot be persisted",
    ))
}
