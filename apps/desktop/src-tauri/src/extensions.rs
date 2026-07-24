use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::json_response;
use codex_riftx_ipc::SkillCatalog;
use codex_riftx_ipc::ToolInventory;

#[tauri::command]
pub(crate) async fn tool_inventory(
    state: tauri::State<'_, DesktopState>,
) -> Result<ToolInventory, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/tools").await).await
}

#[tauri::command]
pub(crate) async fn skill_catalog(
    state: tauri::State<'_, DesktopState>,
) -> Result<SkillCatalog, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/skills").await).await
}
