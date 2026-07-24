use crate::api::ApiError;
use crate::gateway_state::GatewayState;
use crate::inventory::ipc_skill_catalog;
use crate::inventory::ipc_tool_inventory;
use axum::Json;
use axum::extract::State;
use codex_riftx_ipc::SkillCatalog;
use codex_riftx_ipc::ToolInventory;
use codex_riftx_skills::SkillCatalogBuilder;
use codex_riftx_tools::ToolScanner;

pub(crate) async fn doctor_tools(State(state): State<GatewayState>) -> Json<ToolInventory> {
    let inventory = ToolScanner::new(state.config.tools.clone()).scan().await;
    Json(ipc_tool_inventory(&inventory))
}

pub(crate) async fn doctor_skills(
    State(state): State<GatewayState>,
) -> Result<Json<SkillCatalog>, ApiError> {
    let profile_name = &state.config.llm.default_profile;
    let app_server = state.app_servers.get(profile_name).ok_or_else(|| {
        ApiError::app_server(format!(
            "LLM profile {profile_name:?} App Server is unavailable"
        ))
    })?;
    let entry = app_server
        .list_skills(
            &state.config.daemon.workspace_root,
            /*force_reload*/ true,
        )
        .await
        .map_err(|error| ApiError::app_server(error.to_string()))?;
    let catalog = SkillCatalogBuilder::new(state.skills.root.clone())
        .build(entry)
        .await;
    Ok(Json(ipc_skill_catalog(&catalog)))
}
