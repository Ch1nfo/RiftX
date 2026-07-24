use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::json_response;
use serde::Deserialize;
use serde::Serialize;
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ToolInventoryView {
    roots: Vec<PathBuf>,
    path_entries: Vec<PathBuf>,
    tools: Vec<DiscoveredToolView>,
    snapshot_sha256: String,
    diagnostics: Vec<DiagnosticView>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DiscoveredToolView {
    name: String,
    path: PathBuf,
    sha256: String,
    metadata_path: Option<PathBuf>,
    metadata_sha256: Option<String>,
    metadata: Option<ToolMetadataView>,
    shadowed_by: Option<PathBuf>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ToolMetadataView {
    capabilities: Vec<String>,
    risk: Option<String>,
    help_args: Vec<String>,
    version_args: Vec<String>,
    health_check_args: Vec<String>,
    input_target_field: Option<String>,
    output_format: Option<String>,
    parser: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DiagnosticView {
    level: String,
    code: String,
    path: Option<PathBuf>,
    message: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SkillCatalogView {
    root: PathBuf,
    skills: Vec<DiscoveredSkillView>,
    snapshot_sha256: String,
    diagnostics: Vec<DiagnosticView>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DiscoveredSkillView {
    name: String,
    description: String,
    path: PathBuf,
    source: String,
    enabled: bool,
    sha256: String,
}

#[tauri::command]
pub(crate) async fn tool_inventory(
    state: tauri::State<'_, DesktopState>,
) -> Result<ToolInventoryView, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/tools").await).await
}

#[tauri::command]
pub(crate) async fn skill_catalog(
    state: tauri::State<'_, DesktopState>,
) -> Result<SkillCatalogView, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/skills").await).await
}

#[cfg(test)]
#[path = "extensions_tests.rs"]
mod tests;
