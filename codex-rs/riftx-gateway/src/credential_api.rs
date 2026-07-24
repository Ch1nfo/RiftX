use crate::api::ApiError;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use axum::Json;
use axum::extract::Path;
use axum::extract::State;
use axum::http::StatusCode;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::CredentialGrant;
use codex_riftx_core::CredentialKind;
use codex_riftx_core::CredentialReference;
use codex_riftx_core::EffectivePolicy;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::Scope;
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

const MAX_CREDENTIAL_REFERENCES: usize = 128;
const MAX_CREDENTIAL_GRANTS: usize = 128;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateCredentialReferenceParams {
    label: String,
    kind: CredentialKind,
    username: Option<String>,
    domain: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateCredentialGrantParams {
    credential_id: String,
    allowed_targets: Scope,
    allowed_capabilities: Vec<String>,
    max_uses: u32,
    max_failures_per_identity: u32,
    starts_at: Option<i64>,
    expires_at: i64,
}

pub(crate) async fn create_reference(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<CreateCredentialReferenceParams>,
) -> Result<(StatusCode, Json<CredentialReference>), ApiError> {
    let engagement = state.store.engagement(&id).await?;
    ensure_credentials_mutable(&state, &engagement).await?;
    if state.store.credential_references(&id).await?.len() >= MAX_CREDENTIAL_REFERENCES {
        return Err(ApiError::bad_request(format!(
            "an engagement may define at most {MAX_CREDENTIAL_REFERENCES} credential references"
        )));
    }
    let credential_id = Uuid::new_v4().to_string();
    let reference = CredentialReference {
        id: credential_id.clone(),
        engagement_id: id.clone(),
        label: params.label,
        kind: params.kind,
        storage_key: format!("engagement/{id}/credential/{credential_id}"),
        username: params.username,
        domain: params.domain,
        created_at: unix_timestamp(),
    };
    reference
        .validate()
        .map_err(|error| ApiError::bad_request(error.to_string()))?;
    state.store.put_credential_reference(&reference).await?;
    state
        .publish(
            &id,
            "credential/referenceCreated",
            json!({
                "credentialId": reference.id,
                "kind": reference.kind,
            }),
        )
        .await;
    Ok((StatusCode::CREATED, Json(reference)))
}

pub(crate) async fn list_references(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<CredentialReference>>, ApiError> {
    state.store.engagement(&id).await?;
    let mut references = state.store.credential_references(&id).await?;
    references.sort_by(|left, right| {
        left.created_at
            .cmp(&right.created_at)
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(Json(references))
}

pub(crate) async fn delete_reference(
    State(state): State<GatewayState>,
    Path((id, credential_id)): Path<(String, String)>,
) -> Result<Json<CredentialReference>, ApiError> {
    let engagement = state.store.engagement(&id).await?;
    ensure_credentials_mutable(&state, &engagement).await?;
    let reference = state
        .store
        .credential_reference(&id, &credential_id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("credential {credential_id} was not found")))?;
    if state
        .store
        .credential_grants(&id)
        .await?
        .iter()
        .any(|grant| grant.credential_id == credential_id)
    {
        return Err(ApiError::conflict(
            "credential_in_use",
            "credential references with grant history cannot be deleted",
        ));
    }
    state
        .store
        .delete_credential_reference(&id, &credential_id)
        .await?;
    state
        .publish(
            &id,
            "credential/referenceDeleted",
            json!({"credentialId": credential_id}),
        )
        .await;
    Ok(Json(reference))
}

pub(crate) async fn create_grant(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
    Json(params): Json<CreateCredentialGrantParams>,
) -> Result<(StatusCode, Json<CredentialGrant>), ApiError> {
    let mut engagement = state.store.engagement(&id).await?;
    ensure_credentials_mutable(&state, &engagement).await?;
    if state.store.credential_grants(&id).await?.len() >= MAX_CREDENTIAL_GRANTS {
        return Err(ApiError::bad_request(format!(
            "an engagement may define at most {MAX_CREDENTIAL_GRANTS} credential grants"
        )));
    }
    state
        .store
        .credential_reference(&id, &params.credential_id)
        .await?
        .ok_or_else(|| {
            ApiError::bad_request(format!(
                "credential {:?} does not belong to engagement {id}",
                params.credential_id
            ))
        })?;
    let now = unix_timestamp();
    let grant = CredentialGrant {
        id: Uuid::new_v4().to_string(),
        engagement_id: id.clone(),
        credential_id: params.credential_id,
        allowed_targets: params.allowed_targets,
        allowed_capabilities: params.allowed_capabilities,
        max_uses: params.max_uses,
        max_failures_per_identity: params.max_failures_per_identity,
        starts_at: params.starts_at,
        expires_at: params.expires_at,
        created_at: now,
        revoked_at: None,
    };
    grant
        .validate()
        .map_err(|error| ApiError::bad_request(error.to_string()))?;
    validate_grant_constraints(&state, &engagement, &grant, now).await?;
    state.store.put_credential_grant(&grant).await?;
    refresh_policy_revision(&state, &mut engagement).await?;
    state
        .publish(
            &id,
            "credential/grantCreated",
            json!({
                "grantId": grant.id,
                "credentialId": grant.credential_id,
                "policyRevision": engagement.policy_revision,
            }),
        )
        .await;
    Ok((StatusCode::CREATED, Json(grant)))
}

pub(crate) async fn list_grants(
    State(state): State<GatewayState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<CredentialGrant>>, ApiError> {
    state.store.engagement(&id).await?;
    let mut grants = state.store.credential_grants(&id).await?;
    grants.sort_by(|left, right| {
        left.created_at
            .cmp(&right.created_at)
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(Json(grants))
}

pub(crate) async fn revoke_grant(
    State(state): State<GatewayState>,
    Path((id, grant_id)): Path<(String, String)>,
) -> Result<Json<CredentialGrant>, ApiError> {
    let mut engagement = state.store.engagement(&id).await?;
    ensure_credentials_mutable(&state, &engagement).await?;
    let mut grant = state
        .store
        .credential_grant(&id, &grant_id)
        .await?
        .ok_or_else(|| ApiError::not_found(format!("credential grant {grant_id} was not found")))?;
    if grant.revoked_at.is_some() {
        return Ok(Json(grant));
    }
    grant.revoked_at = Some(unix_timestamp());
    state.store.put_credential_grant(&grant).await?;
    refresh_policy_revision(&state, &mut engagement).await?;
    state
        .publish(
            &id,
            "credential/grantRevoked",
            json!({
                "grantId": grant.id,
                "credentialId": grant.credential_id,
                "policyRevision": engagement.policy_revision,
            }),
        )
        .await;
    Ok(Json(grant))
}

pub(crate) async fn resolve_engagement_policy(
    state: &GatewayState,
    engagement: &Engagement,
    mode: ExecutionMode,
) -> Result<EffectivePolicy, ApiError> {
    let policy =
        EffectivePolicy::resolve(&state.config.policy, mode, &engagement.authorization, None)
            .map_err(|error| ApiError::bad_request(error.to_string()))?;
    let grants = state.store.credential_grants(&engagement.id).await?;
    policy
        .bind_credential_grants(&grants)
        .map_err(|error| ApiError::bad_request(error.to_string()))
}

async fn refresh_policy_revision(
    state: &GatewayState,
    engagement: &mut Engagement,
) -> Result<(), ApiError> {
    let policy = resolve_engagement_policy(state, engagement, engagement.mode).await?;
    engagement.policy_revision = policy.revision;
    engagement.updated_at = unix_timestamp();
    state.store.put_engagement(engagement).await?;
    Ok(())
}

async fn validate_grant_constraints(
    state: &GatewayState,
    engagement: &Engagement,
    grant: &CredentialGrant,
    now: i64,
) -> Result<(), ApiError> {
    if grant.expires_at <= now {
        return Err(ApiError::bad_request(
            "credential grant expiry must be in the future",
        ));
    }
    let policy = EffectivePolicy::resolve(
        &state.config.policy,
        engagement.mode,
        &engagement.authorization,
        None,
    )
    .map_err(|error| ApiError::bad_request(error.to_string()))?;
    for capability in &grant.allowed_capabilities {
        if !policy.allows_capability(capability) {
            return Err(ApiError::bad_request(format!(
                "credential grant capability {capability:?} is outside the effective policy"
            )));
        }
    }
    for cidr in &grant.allowed_targets.cidrs {
        policy
            .check_target(&cidr.to_string())
            .map_err(|error| ApiError::bad_request(error.to_string()))?;
    }
    for domain in &grant.allowed_targets.domains {
        if !domain_scope_is_subset(domain, &engagement.authorization) {
            return Err(ApiError::bad_request(format!(
                "credential grant domain {domain:?} is outside the engagement scope"
            )));
        }
    }
    if !policy.allowed_ports.is_empty()
        && grant
            .allowed_targets
            .ports
            .iter()
            .any(|port| !policy.allowed_ports.contains(port))
    {
        return Err(ApiError::bad_request(
            "one or more credential grant ports are outside the engagement scope",
        ));
    }
    if let (Some(authorization_start), Some(grant_start)) =
        (engagement.authorization.window.starts_at, grant.starts_at)
        && grant_start < authorization_start
    {
        return Err(ApiError::bad_request(
            "credential grant cannot start before engagement authorization",
        ));
    }
    if engagement
        .authorization
        .window
        .expires_at
        .is_some_and(|authorization_expiry| grant.expires_at > authorization_expiry)
    {
        return Err(ApiError::bad_request(
            "credential grant cannot outlive engagement authorization",
        ));
    }
    Ok(())
}

async fn ensure_credentials_mutable(
    state: &GatewayState,
    engagement: &Engagement,
) -> Result<(), ApiError> {
    if engagement.status == EngagementStatus::Completed {
        return Err(ApiError::conflict(
            "credentials_locked",
            "completed engagement credentials cannot be changed",
        ));
    }
    if engagement.mode == ExecutionMode::Auto && engagement.status == EngagementStatus::Active {
        return Err(ApiError::conflict(
            "credentials_locked",
            "Auto Mode credentials cannot change while the engagement is active",
        ));
    }
    if state.active_turns.read().await.contains_key(&engagement.id)
        || state
            .pending_approvals
            .read()
            .await
            .values()
            .any(|pending| pending.engagement_id == engagement.id)
        || state
            .store
            .executions(&engagement.id)
            .await?
            .iter()
            .any(|execution| {
                matches!(
                    execution.status,
                    ExecutionStatus::Pending | ExecutionStatus::Running
                )
            })
    {
        return Err(ApiError::conflict(
            "credentials_locked",
            "credentials cannot change while work or approval is active",
        ));
    }
    Ok(())
}

fn domain_scope_is_subset(domain: &str, authorization: &AuthorizationScope) -> bool {
    let domain = domain.trim_end_matches('.').to_ascii_lowercase();
    authorization.network.domains.iter().any(|allowed| {
        let allowed = allowed.trim_end_matches('.').to_ascii_lowercase();
        match (allowed.strip_prefix("*."), domain.strip_prefix("*.")) {
            (Some(allowed_suffix), Some(domain_suffix)) => {
                domain_suffix == allowed_suffix
                    || domain_suffix.ends_with(&format!(".{allowed_suffix}"))
            }
            (Some(allowed_suffix), None) => {
                domain != allowed_suffix && domain.ends_with(&format!(".{allowed_suffix}"))
            }
            (None, None) => domain == allowed,
            (None, Some(_)) => false,
        }
    })
}

#[cfg(test)]
#[path = "credential_api_tests.rs"]
mod tests;
