use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::json_response;
use crate::bridge::validate_opaque_id;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_ipc::CreateCredentialGrantParams;
use codex_riftx_ipc::CreateCredentialReferenceParams;
use codex_riftx_ipc::CredentialGrant;
use codex_riftx_ipc::CredentialKind;
use codex_riftx_ipc::CredentialReference;
use codex_riftx_ipc::Scope;
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CreateAssessmentCredentialInput {
    engagement_id: String,
    label: String,
    kind: CredentialKind,
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
) -> Result<Vec<CredentialReference>, DesktopError> {
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
) -> Result<CredentialReference, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    let params = create_reference_params(&input);
    let secret = AssessmentSecret::new(input.secret).map_err(credential_error)?;
    let client = state.client()?;
    let reference: CredentialReference = json_response(
        client
            .post_typed(
                &format!("/v1/engagements/{}/credentials", input.engagement_id),
                &params,
            )
            .await,
    )
    .await?;
    let credential_id = reference.id;
    match json_response(
        client
            .post_bytes(
                &format!(
                    "/v1/engagements/{}/credentials/{credential_id}/secret",
                    input.engagement_id
                ),
                secret.into_bytes(),
            )
            .await,
    )
    .await
    {
        Ok(configured) => Ok(configured),
        Err(error) => {
            let _ = client
                .post(&format!(
                    "/v1/engagements/{}/credentials/{credential_id}/delete",
                    input.engagement_id
                ))
                .await;
            Err(error)
        }
    }
}

#[tauri::command]
pub(crate) async fn delete_assessment_credential(
    state: tauri::State<'_, DesktopState>,
    input: DeleteAssessmentCredentialInput,
) -> Result<CredentialReference, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_opaque_id("credential", &input.credential_id)?;
    let client = state.client()?;
    let references: Vec<CredentialReference> = json_response(
        client
            .get(&format!(
                "/v1/engagements/{}/credentials",
                input.engagement_id
            ))
            .await,
    )
    .await?;
    credential_by_id(&references, &input.credential_id)?;
    let grants: Vec<CredentialGrant> = json_response(
        client
            .get(&format!(
                "/v1/engagements/{}/credential-grants",
                input.engagement_id
            ))
            .await,
    )
    .await?;
    for grant in grants_for_credential(&grants, &input.credential_id) {
        let _: CredentialGrant = json_response(
            client
                .post(&format!(
                    "/v1/engagements/{}/credential-grants/{}/revoke",
                    input.engagement_id, grant.id
                ))
                .await,
        )
        .await?;
    }
    json_response(
        client
            .post(&format!(
                "/v1/engagements/{}/credentials/{}/delete",
                input.engagement_id, input.credential_id
            ))
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn list_credential_grants(
    state: tauri::State<'_, DesktopState>,
    engagement_id: String,
) -> Result<Vec<CredentialGrant>, DesktopError> {
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
) -> Result<CredentialGrant, DesktopError> {
    validate_opaque_id("engagement", &input.engagement_id)?;
    validate_opaque_id("credential", &input.credential_id)?;
    let params = grant_params(&input)?;
    let client = state.client()?;
    json_response(
        client
            .post_typed(
                &format!("/v1/engagements/{}/credential-grants", input.engagement_id),
                &params,
            )
            .await,
    )
    .await
}

#[tauri::command]
pub(crate) async fn revoke_credential_grant(
    state: tauri::State<'_, DesktopState>,
    input: RevokeCredentialGrantInput,
) -> Result<CredentialGrant, DesktopError> {
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

fn create_reference_params(
    input: &CreateAssessmentCredentialInput,
) -> CreateCredentialReferenceParams {
    CreateCredentialReferenceParams {
        label: input.label.clone(),
        kind: input.kind,
        username: input.username.clone(),
        domain: input.domain.clone(),
    }
}

fn grant_params(
    input: &CreateCredentialGrantInput,
) -> Result<CreateCredentialGrantParams, DesktopError> {
    let cidrs = input
        .cidrs
        .iter()
        .map(|cidr| {
            cidr.parse()
                .map_err(|error| DesktopError::new("invalid_cidr", format!("{cidr}: {error}")))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(CreateCredentialGrantParams {
        credential_id: input.credential_id.clone(),
        allowed_targets: Scope {
            cidrs,
            domains: input.domains.clone(),
            ports: input.ports.clone(),
        },
        allowed_capabilities: input.capabilities.clone(),
        max_uses: input.max_uses,
        max_failures_per_identity: input.max_failures_per_identity,
        starts_at: input.starts_at,
        expires_at: input.expires_at,
    })
}

fn credential_by_id<'a>(
    references: &'a [CredentialReference],
    id: &str,
) -> Result<&'a CredentialReference, DesktopError> {
    references
        .iter()
        .find(|reference| reference.id == id)
        .ok_or_else(|| {
            DesktopError::new(
                "invalid_daemon_response",
                format!("credential {id:?} was not found"),
            )
        })
}

fn grants_for_credential<'a>(
    grants: &'a [CredentialGrant],
    credential_id: &str,
) -> Vec<&'a CredentialGrant> {
    grants
        .iter()
        .filter(|grant| grant.credential_id == credential_id)
        .collect()
}

fn credential_error(error: impl std::fmt::Display) -> DesktopError {
    DesktopError::new("credential_store", error.to_string())
}

#[cfg(test)]
#[path = "credentials_tests.rs"]
mod tests;
