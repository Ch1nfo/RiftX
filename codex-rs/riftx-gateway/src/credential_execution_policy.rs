use crate::api::ApiError;
use crate::credential_execution::resolve_tool;
use crate::gateway_state::GatewayState;
use codex_riftx_core::Engagement;
use codex_riftx_execution_policy::CommandIntentInput;
use codex_riftx_execution_policy::CommandSpec;
use codex_riftx_execution_policy::ExecutionIntent;
use codex_riftx_ipc::CredentialExecutionParams;

pub(crate) enum CredentialExecutionOrigin {
    OperatorApi,
    DynamicTool {
        thread_id: String,
        tool_call_id: String,
        turn_id: String,
        approved_binding: Option<String>,
    },
}

impl CredentialExecutionOrigin {
    fn identifiers<'a>(&'a self, use_id: &'a str) -> (&'a str, String, String) {
        match self {
            Self::OperatorApi => (
                "operator-api",
                format!("credential:{use_id}"),
                format!("credential:{use_id}"),
            ),
            Self::DynamicTool {
                thread_id,
                turn_id,
                tool_call_id,
                ..
            } => (thread_id, turn_id.clone(), tool_call_id.clone()),
        }
    }

    pub(crate) fn approves(&self, intent: &ExecutionIntent) -> bool {
        match self {
            Self::OperatorApi => true,
            Self::DynamicTool {
                approved_binding, ..
            } => approved_binding.as_deref() == Some(intent.binding_sha256.as_str()),
        }
    }

    pub(crate) fn tool_call_id(&self) -> Option<&str> {
        match self {
            Self::OperatorApi => None,
            Self::DynamicTool { tool_call_id, .. } => Some(tool_call_id),
        }
    }

    pub(crate) fn turn_id(&self) -> Option<&str> {
        match self {
            Self::OperatorApi => None,
            Self::DynamicTool { turn_id, .. } => Some(turn_id),
        }
    }
}

pub(crate) fn credential_execution_intent(
    state: &GatewayState,
    engagement: &Engagement,
    params: &CredentialExecutionParams,
    origin: &CredentialExecutionOrigin,
    use_id: &str,
) -> Result<ExecutionIntent, ApiError> {
    let (tool, metadata) = resolve_tool(state, &params.tool)?;
    let arguments = metadata
        .render_arguments(&params.target.host, params.target.port)
        .ok_or_else(|| ApiError::bad_request("credential tool target template is invalid"))?;
    let mut argv = vec![tool.path.to_string_lossy().into_owned()];
    argv.extend(arguments);
    let workspace = state.config.daemon.workspace_root.join(&engagement.id);
    let (thread_id, turn_id, tool_call_id) = origin.identifiers(use_id);
    Ok(ExecutionIntent::from_command(CommandIntentInput {
        engagement_id: &engagement.id,
        thread_id,
        turn_id: &turn_id,
        tool_call_id: &tool_call_id,
        mode: engagement.mode,
        command: CommandSpec::Argv(&argv),
        cwd: &workspace,
        search_path: &state.tool_search_path,
        inventory: &state.tools,
        requested_capabilities: std::slice::from_ref(&metadata.capability),
        authorization_deadline: engagement.authorization.window.expires_at,
        policy_revision: &engagement.policy_revision,
    }))
}
