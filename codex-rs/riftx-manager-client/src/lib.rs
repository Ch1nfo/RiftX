//! Typed Unix-socket client for the RiftX sandbox manager control plane.

use reqwest::Client;
use serde::Deserialize;
use serde::Serialize;
use std::path::Path;
use std::time::Duration;
use thiserror::Error;

const MANAGER_ORIGIN: &str = "http://riftx-managerd";

#[derive(Debug, Error)]
pub enum ManagerClientError {
    #[error("failed to construct managerd client: {0}")]
    Build(#[source] reqwest::Error),
    #[error("managerd request failed: {0}")]
    Request(#[from] reqwest::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SandboxResources {
    pub cpu_limit: u16,
    pub memory_mib: u32,
    pub pids_limit: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SandboxScope {
    pub cidrs: Vec<String>,
    pub domains: Vec<String>,
    pub ports: Vec<u16>,
    pub denied_cidrs: Vec<String>,
    pub denied_domains: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CreateSandboxRequest {
    pub engagement_id: String,
    pub image: String,
    pub profile: String,
    pub policy_revision: String,
    pub resources: SandboxResources,
    pub scope: SandboxScope,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum SandboxStatus {
    Creating,
    Ready,
    Interrupted,
    Stopped,
    Failed,
}

#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct BootstrapToken(String);

impl BootstrapToken {
    pub fn into_inner(self) -> String {
        self.0
    }
}

impl std::fmt::Debug for BootstrapToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Sandbox {
    pub id: String,
    pub engagement_id: String,
    pub status: SandboxStatus,
    pub environment_id: String,
    pub exec_server_url: String,
    pub bootstrap_token: Option<BootstrapToken>,
    pub policy_revision: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SandboxEvent {
    pub cursor: String,
    pub sandbox_id: String,
    pub kind: String,
    pub timestamp: i64,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SandboxEventsResponse {
    pub events: Vec<SandboxEvent>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ExportArtifactRequest {
    pub path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ExportedArtifact {
    pub path: String,
    pub media_type: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Clone)]
pub struct ManagerClient {
    client: Client,
}

impl ManagerClient {
    pub fn new(socket: &Path, request_timeout: Duration) -> Result<Self, ManagerClientError> {
        let client = Client::builder()
            .unix_socket(socket)
            .timeout(request_timeout)
            .build()
            .map_err(ManagerClientError::Build)?;
        Ok(Self { client })
    }

    pub async fn create_sandbox(
        &self,
        request: &CreateSandboxRequest,
    ) -> Result<Sandbox, ManagerClientError> {
        Ok(self
            .client
            .post(format!("{MANAGER_ORIGIN}/v1/sandboxes"))
            .json(request)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    pub async fn sandbox(&self, id: &str) -> Result<Sandbox, ManagerClientError> {
        Ok(self
            .client
            .get(format!("{MANAGER_ORIGIN}/v1/sandboxes/{id}"))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    pub async fn interrupt(&self, id: &str) -> Result<Sandbox, ManagerClientError> {
        self.lifecycle_action(id, "interrupt").await
    }

    pub async fn kill(&self, id: &str) -> Result<Sandbox, ManagerClientError> {
        self.lifecycle_action(id, "kill").await
    }

    pub async fn delete(&self, id: &str) -> Result<(), ManagerClientError> {
        self.client
            .delete(format!("{MANAGER_ORIGIN}/v1/sandboxes/{id}"))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    pub async fn export_artifact(
        &self,
        id: &str,
        request: &ExportArtifactRequest,
    ) -> Result<ExportedArtifact, ManagerClientError> {
        Ok(self
            .client
            .post(format!(
                "{MANAGER_ORIGIN}/v1/sandboxes/{id}/artifacts/export"
            ))
            .json(request)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    pub async fn events(
        &self,
        cursor: Option<&str>,
    ) -> Result<SandboxEventsResponse, ManagerClientError> {
        let mut request = self.client.get(format!("{MANAGER_ORIGIN}/v1/events"));
        if let Some(cursor) = cursor {
            request = request.query(&[("cursor", cursor)]);
        }
        Ok(request.send().await?.error_for_status()?.json().await?)
    }

    async fn lifecycle_action(
        &self,
        id: &str,
        action: &str,
    ) -> Result<Sandbox, ManagerClientError> {
        Ok(self
            .client
            .post(format!("{MANAGER_ORIGIN}/v1/sandboxes/{id}/{action}"))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }
}

#[cfg(test)]
#[path = "lib_tests.rs"]
mod tests;
