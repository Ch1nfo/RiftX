use crate::api::ApiError;
use codex_riftx_credentials::AssessmentSecret;
use codex_riftx_credentials::AssessmentSecretProvider;
use codex_riftx_credentials::CredentialLocator;
use std::sync::Arc;
use std::time::Duration;

const CREDENTIAL_LOAD_TIMEOUT: Duration = Duration::from_secs(30);

pub(crate) async fn load(
    provider: Arc<dyn AssessmentSecretProvider>,
    locator: CredentialLocator,
) -> Result<Option<AssessmentSecret>, ApiError> {
    let task = tokio::task::spawn_blocking(move || provider.load_secret(&locator));
    match tokio::time::timeout(CREDENTIAL_LOAD_TIMEOUT, task).await {
        Ok(Ok(result)) => result.map_err(|error| ApiError::internal(error.to_string())),
        Ok(Err(error)) => Err(ApiError::internal(format!(
            "credential store task failed: {error}"
        ))),
        Err(_) => Err(ApiError::conflict(
            "credential_store_timeout",
            "operating-system credential store did not respond",
        )),
    }
}

pub(crate) async fn save(
    provider: Arc<dyn AssessmentSecretProvider>,
    locator: CredentialLocator,
    secret: AssessmentSecret,
) -> Result<(), ApiError> {
    tokio::task::spawn_blocking(move || provider.save_secret(&locator, secret))
        .await
        .map_err(|error| ApiError::internal(format!("credential store task failed: {error}")))?
        .map_err(|error| ApiError::internal(error.to_string()))
}

pub(crate) async fn delete(
    provider: Arc<dyn AssessmentSecretProvider>,
    locator: CredentialLocator,
) -> Result<bool, ApiError> {
    tokio::task::spawn_blocking(move || provider.delete_secret(&locator))
        .await
        .map_err(|error| ApiError::internal(format!("credential store task failed: {error}")))?
        .map_err(|error| ApiError::internal(error.to_string()))
}
