use codex_riftx_ipc::DaemonInfo;
use serde::Serialize;
use serde_json::Value;
use std::path::Path;

#[cfg(test)]
pub(crate) const CLI_JSON_CONTRACT_VERSION: u32 = 1;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ConfigValidation<'a> {
    pub(crate) ok: bool,
    pub(crate) config: &'a Path,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DoctorReport<'a> {
    pub(crate) ok: bool,
    pub(crate) config: &'a Path,
    pub(crate) daemon: &'a DaemonInfo,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ApprovalDecisionOutput<'a> {
    pub(crate) approval_id: &'a str,
    pub(crate) decision: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct OperationSuccess {
    pub(crate) ok: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct EventEnvelope {
    pub(crate) event: Option<String>,
    pub(crate) data: Value,
    pub(crate) id: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ArtifactExportOutput<'a> {
    pub(crate) output: &'a Path,
}

#[cfg(test)]
#[path = "json_contract_tests.rs"]
mod tests;
