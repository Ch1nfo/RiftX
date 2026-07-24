use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::json_response;
use crate::bridge::validate_opaque_id;
use codex_riftx_credentials::AssessmentCredentialStore;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_credentials::CredentialLocator;
use serde::Deserialize;
use serde_json::Value;
use serde_json::json;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateAssessmentCredentialInput {
    engagement_id: String,
    label: String,
    kind: String,
    username: Option<String>,
    domain: Option<String>,
    secret: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct DeleteAssessmentCredentialInput {
    engagement_id: String,
    credential_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateCredentialGrantInput {
    engagement_id: String,
    credential_id: String,
    cidrs: Vec<String>,
    domains: Vec<String>,
    ports: Vec<u16>,
    capabilities: Vec<String>,
    max_uses: u32,
    max_failures_per_identity: u32,
    starts_at: Option<i64>,
    expires_at: i64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct RevokeCredentialGrantInput {
    engagement_id: String,
    grant_id: String,
}

#[tauri::command]
pub(crate) async fn list_assessment_credentials(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &engagement_id)?;
    let client = state.client()?;
    json_response(
        client
            .get(&format!("/v1/engagements/{engagement_id}/credentials"))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn create_assessment_credential(
    state: tauri::State<'_, DesktopState>,
    input: CreateAssessmentCredentialInput,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_credential_kind(&input.kind)?;
    let body = create_reference_body(&input)?;
    let secret = AssessmentSecret::new(input.secret).map_err(credential_error)?;
    let client = state.client()?;
    let reference: Value = json_response(
        client
            .post_json(
                &format!("/v1/engagements/{}/credentials", input.engagement_id),
                body,
            )
            .await,
    )
    .await?;
    let credential_id = response_id(&reference, "credential")?.to_string();
    let locator =
        CredentialLocator::new(&input.engagement_id, &credential_id).map_err(credential_error)?;
    let save_result = tokio::task::spawn_blocking(move || {
        AssessmentCredentialStore::default().save(&locator, secret)
    })
    .await
    .map_err(|error| DesktopError::new("credential_store", error.to_string()))?;
    if let Err(error) = save_result {
        let _ = client
            .post(&format!(
                "/v1/engagements/{}/credentials/{credential_id}/delete",
                input.engagement_id
            ))
            .await;
        return Err(credential_error(error));
    }
    Ok(reference)
}

#[tauri::command]
pub(crate) async fn delete_assessment_credential(
    state: tauri::State<'_, DesktopState>,
    input: DeleteAssessmentCredentialInput,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_opaque_id("credential", &input.credential_id)?;
    let locator = CredentialLocator::new(&input.engagement_id, &input.credential_id)
        .map_err(credential_error)?;
    let locator_for_load = locator.clone();
    tokio::task::spawn_blocking(move || {
        AssessmentCredentialStore::default().load(&locator_for_load)
    })
    .await
    .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
    .map_err(credential_error)?;
    let client = state.client()?;
    let reference = json_response(
        client
            .post(&format!(
                "/v1/engagements/{}/credentials/{}/delete",
                input.engagement_id, input.credential_id
            ))
            .await,
    )
    .await?;
    tokio::task::spawn_blocking(move || AssessmentCredentialStore::default().delete(&locator))
        .await
        .map_err(|error| DesktopError::new("credential_store", error.to_string()))?
        .map_err(credential_error)?;
    Ok(reference)
}

#[tauri::command]
pub(crate) async fn list_credential_grants(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &engagement_id)?;
    let client = state.client()?;
    json_response(
        client
            .get(&format!(
                "/v1/engagements/{engagement_id}/credential-grants"
            ))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn create_credential_grant(
    state: tauri::State<'_, DesktopState>,
    input: CreateCredentialGrantInput,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_opaque_id("credential", &input.credential_id)?;
    let body = grant_body(&input)?;
    let client = state.client()?;
    json_response(
        client
            .post_json(
                &format!("/v1/engagements/{}/credential-grants", input.engagement_id),
                body,
            )
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn revoke_credential_grant(
    state: tauri::State<'_, DesktopState>,
    input: RevokeCredentialGrantInput,
) -> Result<Value, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_opaque_id("credential grant", &input.grant_id)?;
    let client = state.client()?;
    json_response(
        client
            .post(&format!(
                "/v1/engagements/{}/credential-grants/{}/revoke",
                input.engagement_id, input.grant_id
            ))
            .await,
    )
    .await
}

fn create_reference_body(input: &CreateAssessmentCredentialInput) -> Result<Vec<u8>, DesktopError> {
    serde_json::to_vec(&json!({
        "label": input.label,
        "kind": input.kind,
        "username": input.username,
        "domain": input.domain,
    }))
    .map_err(|error| DesktopError::new("encode_request", error.to_string()))
}

fn grant_body(input: &CreateCredentialGrantInput) -> Result<Vec<u8>, DesktopError> {
    serde_json::to_vec(&json!({
        "credentialId": input.credential_id,
        "allowedTargets": {
            "cidrs": input.cidrs,
            "domains": input.domains,
            "ports": input.ports,
        },
        "allowedCapabilities": input.capabilities,
        "maxUses": input.max_uses,
        "maxFailuresPerIdentity": input.max_failures_per_identity,
        "startsAt": input.starts_at,
        "expiresAt": input.expires_at,
    }))
    .map_err(|error| DesktopError::new("encode_request", error.to_string()))
}

fn validate_credential_kind(kind: &str) -> Result<(), DesktopError> {
    if matches!(
        kind,
        "password" | "apiToken" | "sshKey" | "certificate" | "other"
    ) {
        return Ok(());
    }
    Err(DesktopError::new(
        "invalid_credential_kind",
        "credential kind is invalid",
    ))
}

fn response_id<'a>(value: &'a Value, kind: &str) -> Result<&'a str, DesktopError> {
    value.get("id").and_then(Value::as_str).ok_or_else(|| {
        DesktopError::new(
            "invalid_daemon_response",
            format!("riftxd returned a response without a valid {kind} id"),
        )
    })
}

fn credential_error(error: impl std::fmt::Display) -> DesktopError {
    DesktopError::new("credential_store", error.to_string())
}

#[cfg(test)]
#[path = "credentials_tests.rs"]
mod tests;
