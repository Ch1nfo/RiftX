use crate::bridge::DesktopError;
use crate::bridge::DesktopState;
use crate::bridge::json_response;
use codex_riftx_ipc::LlmConnectionTestResult;
use codex_riftx_ipc::LlmProfileList;
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
pub(crate) async fn tool_doctor(
    state: tauri::State<'_, DesktopState>,
) -> Result<ToolInventory, DesktopError> {
    let client = state.client()?;
    json_response(client.post("/v1/tools/doctor").await).await
}

#[tauri::command]
pub(crate) async fn skill_catalog(
    state: tauri::State<'_, DesktopState>,
) -> Result<SkillCatalog, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/skills").await).await
}

#[tauri::command]
pub(crate) async fn skill_doctor(
    state: tauri::State<'_, DesktopState>,
) -> Result<SkillCatalog, DesktopError> {
    let client = state.client()?;
    json_response(client.post("/v1/skills/doctor").await).await
}

#[tauri::command]
pub(crate) async fn llm_profiles(
    state: tauri::State<'_, DesktopState>,
) -> Result<LlmProfileList, DesktopError> {
    let client = state.client()?;
    json_response(client.get("/v1/llm/profiles").await).await
}

#[tauri::command]
pub(crate) async fn test_llm_profile(
    state: tauri::State<'_, DesktopState>,
    profile_name: String,
) -> Result<LlmConnectionTestResult, DesktopError> {
    let client = state.client()?;
    json_response(
        client
            .post(&format!("/v1/llm/profiles/{profile_name}/test"))
            .await,
    )
    .await
}
