use crate::api::ApiError;
use crate::api::require_execution_running;
use crate::gateway_state::ActiveCredentialProcess;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use axum::Json;
use axum::extract::Path;
use axum::extract::State;
use codex_riftx_core::CredentialGrantUse;
use codex_riftx_core::CredentialUseOutcome;
use codex_riftx_core::CredentialUseRequest;
use codex_riftx_core::CredentialUseTarget;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::Execution;
use codex_riftx_core::ExecutionStatus;
use codex_riftx_core::ExecutionTool;
use codex_riftx_credentials::CredentialInjection;
use codex_riftx_credentials::CredentialLocator;
use codex_riftx_credentials::CredentialProcessOutput;
use codex_riftx_credentials::CredentialProcessRequest;
use codex_riftx_credentials::CredentialProcessRunner;
use codex_riftx_credentials::CredentialProcessTermination;
use codex_riftx_tools::DiscoveredTool;
use codex_riftx_tools::ToolCredentialInjection;
use codex_riftx_tools::ToolCredentialMetadata;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::time::Duration;
use std::time::Instant;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const PROCESS_TIMEOUT: Duration = Duration::from_secs(300);
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CredentialExecutionParams {
    grant_id: String,
    tool: String,
    target: CredentialUseTarget,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CredentialExecutionResponse {
    usage: CredentialGrantUse,
    execution: Execution,
    stdout: String,
    stderr: String,
}

pub(crate) async fn execute(
    State(state): State<GatewayState>,
    Path(engagement_id): Path<String>,
    Json(params): Json<CredentialExecutionParams>,
) -> Result<Json<CredentialExecutionResponse>, ApiError> {
    require_execution_running(&state).await?;
    let engagement = state.store.engagement(&engagement_id).await?;
    if engagement.status != EngagementStatus::Active {
        return Err(ApiError::conflict(
            "engagement_inactive",
            "credential tools require an active engagement",
        ));
    }
    let (tool, metadata) = resolve_tool(&state, &params.tool)?;
    let arguments = metadata
        .render_arguments(&params.target.host, params.target.port)
        .ok_or_else(|| ApiError::bad_request("credential tool target template is invalid"))?;
    let workspace = state.config.daemon.workspace_root.join(&engagement_id);
    tokio::fs::create_dir_all(&workspace)
        .await
        .map_err(|error| ApiError::internal(error.to_string()))?;
    let injection = injection(metadata)?;
    let environment = safe_environment(&state)?;
    let runner = CredentialProcessRunner::new(PROCESS_TIMEOUT, MAX_OUTPUT_BYTES)
        .map_err(|error| ApiError::internal(error.to_string()))?;
    let use_id = Uuid::new_v4().to_string();
    let usage_request = CredentialUseRequest {
        id: use_id.clone(),
        engagement_id: engagement_id.clone(),
        grant_id: params.grant_id,
        target: params.target,
        capability: metadata.capability.clone(),
        policy_revision: engagement.policy_revision,
        requested_at: unix_timestamp(),
    };
    let reserved = state.store.reserve_credential_use(&usage_request).await?;
    let locator = CredentialLocator::new(&engagement_id, &reserved.credential_id)
        .map_err(|error| ApiError::internal(error.to_string()))?;
    let secret = match state.assessment_credentials.load_secret(&locator) {
        Ok(Some(secret)) => secret,
        Ok(None) => {
            fail_reservation(&state, &engagement_id, &use_id).await;
            return Err(ApiError::conflict(
                "credential_secret_missing",
                "credential secret is not available in the operating-system credential store",
            ));
        }
        Err(error) => {
            fail_reservation(&state, &engagement_id, &use_id).await;
            return Err(ApiError::internal(error.to_string()));
        }
    };
    let request = CredentialProcessRequest {
        program: tool.path.clone(),
        expected_sha256: tool.sha256.clone(),
        args: arguments.iter().map(OsString::from).collect(),
        cwd: workspace.clone(),
        environment,
        injection,
    };
    let cancellation = CancellationToken::new();
    state.credential_processes.write().await.insert(
        use_id.clone(),
        ActiveCredentialProcess {
            engagement_id: engagement_id.clone(),
            cancellation: cancellation.clone(),
        },
    );
    let started = Instant::now();
    let started_at = unix_timestamp();
    state
        .publish(
            &engagement_id,
            "credential/useStarted",
            json!({
                "useId": use_id,
                "grantId": reserved.grant_id,
                "credentialId": reserved.credential_id,
                "tool": tool.name,
                "resolvedPath": tool.path,
                "toolSha256": tool.sha256,
                "target": usage_request.target,
                "capability": metadata.capability,
            }),
        )
        .await;
    let result = runner.run_cancellable(request, secret, cancellation).await;
    state.credential_processes.write().await.remove(&use_id);
    let output = match result {
        Ok(output) => output,
        Err(error) => {
            fail_reservation(&state, &engagement_id, &use_id).await;
            state
                .publish(
                    &engagement_id,
                    "credential/useFailed",
                    json!({"useId": use_id, "error": error.to_string()}),
                )
                .await;
            return Err(ApiError::internal(error.to_string()));
        }
    };
    let outcome = outcome(metadata, &output);
    let completed_at = unix_timestamp();
    let usage = state
        .store
        .complete_credential_use(&engagement_id, &use_id, outcome, completed_at)
        .await?;
    let execution = execution(ExecutionRecordInput {
        state: &state,
        tool,
        arguments: &arguments,
        workspace: &workspace,
        output: &output,
        use_id: &use_id,
        engagement_id: &engagement_id,
        started_at,
        completed_at,
        duration: started.elapsed(),
    });
    state.store.put_execution(&execution).await?;
    state
        .publish(
            &engagement_id,
            "credential/useCompleted",
            json!({"usage": &usage, "execution": &execution}),
        )
        .await;
    Ok(Json(CredentialExecutionResponse {
        usage,
        execution,
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }))
}

fn resolve_tool<'a>(
    state: &'a GatewayState,
    requested_name: &str,
) -> Result<(&'a DiscoveredTool, &'a ToolCredentialMetadata), ApiError> {
    let tool = state
        .tools
        .tools
        .iter()
        .find(|tool| tool.name == requested_name && tool.shadowed_by.is_none())
        .ok_or_else(|| ApiError::not_found(format!("tool {requested_name:?} was not found")))?;
    let metadata = tool
        .metadata
        .as_ref()
        .and_then(|metadata| metadata.credential.as_ref())
        .ok_or_else(|| {
            ApiError::bad_request(format!(
                "tool {requested_name:?} does not declare credential injection metadata"
            ))
        })?;
    Ok((tool, metadata))
}

fn injection(metadata: &ToolCredentialMetadata) -> Result<CredentialInjection, ApiError> {
    let variable = || {
        metadata
            .environment_variable
            .clone()
            .ok_or_else(|| ApiError::internal("credential environment variable is missing"))
    };
    match metadata.injection {
        ToolCredentialInjection::Stdin => Ok(CredentialInjection::Stdin),
        ToolCredentialInjection::Environment => Ok(CredentialInjection::Environment {
            variable: variable()?,
        }),
        ToolCredentialInjection::FileEnvironment => Ok(CredentialInjection::FileEnvironment {
            variable: variable()?,
        }),
    }
}

fn safe_environment(state: &GatewayState) -> Result<BTreeMap<String, OsString>, ApiError> {
    let mut environment = BTreeMap::new();
    let path = state
        .tools
        .process_path(std::env::var_os("PATH"))
        .map_err(|error| ApiError::internal(error.to_string()))?;
    environment.insert("PATH".to_string(), path);
    for variable in [
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
    ] {
        if let Some(value) = std::env::var_os(variable) {
            environment.insert(variable.to_string(), value);
        }
    }
    Ok(environment)
}

fn outcome(
    metadata: &ToolCredentialMetadata,
    output: &CredentialProcessOutput,
) -> CredentialUseOutcome {
    match output.termination {
        CredentialProcessTermination::Exited { code: Some(0) } => CredentialUseOutcome::Succeeded,
        CredentialProcessTermination::Exited { code: Some(code) }
            if metadata.authentication_failure_exit_codes.contains(&code) =>
        {
            CredentialUseOutcome::AuthenticationFailed
        }
        CredentialProcessTermination::Cancelled => CredentialUseOutcome::Interrupted,
        CredentialProcessTermination::Exited { .. } | CredentialProcessTermination::TimedOut => {
            CredentialUseOutcome::ExecutionFailed
        }
    }
}

struct ExecutionRecordInput<'a> {
    state: &'a GatewayState,
    tool: &'a DiscoveredTool,
    arguments: &'a [String],
    workspace: &'a std::path::Path,
    output: &'a CredentialProcessOutput,
    use_id: &'a str,
    engagement_id: &'a str,
    started_at: i64,
    completed_at: i64,
    duration: Duration,
}

fn execution(input: ExecutionRecordInput<'_>) -> Execution {
    let status = match input.output.termination {
        CredentialProcessTermination::Exited { code: Some(0) } => ExecutionStatus::Completed,
        CredentialProcessTermination::Cancelled => ExecutionStatus::Interrupted,
        CredentialProcessTermination::Exited { .. } | CredentialProcessTermination::TimedOut => {
            ExecutionStatus::Failed
        }
    };
    let exit_code = match input.output.termination {
        CredentialProcessTermination::Exited { code } => code,
        CredentialProcessTermination::TimedOut | CredentialProcessTermination::Cancelled => None,
    };
    Execution {
        id: input.use_id.to_string(),
        engagement_id: input.engagement_id.to_string(),
        test_case_id: None,
        task_id: None,
        turn_id: format!("credential:{}", input.use_id),
        runner: "local:credential".to_string(),
        status,
        started_at: input.started_at,
        completed_at: Some(input.completed_at),
        exit_code,
        duration_ms: Some(i64::try_from(input.duration.as_millis()).unwrap_or(i64::MAX)),
        argv: std::iter::once(input.tool.name.clone())
            .chain(input.arguments.iter().cloned())
            .collect(),
        command_sha256: command_digest(&input.tool.sha256, input.arguments),
        cwd: input.workspace.display().to_string(),
        process_id: None,
        tool: Some(ExecutionTool {
            requested_name: input.tool.name.clone(),
            resolved_path: Some(input.tool.path.display().to_string()),
            sha256: Some(input.tool.sha256.clone()),
            metadata_sha256: input.tool.metadata_sha256.clone(),
            version: None,
            managed: true,
        }),
        tool_inventory_sha256: input.state.tools.snapshot_sha256.clone(),
        stdout_sha256: Some(input.output.stdout_sha256.clone()),
        stderr_sha256: Some(input.output.stderr_sha256.clone()),
        stdin_sha256: None,
        stdout_bytes: u64::try_from(input.output.stdout.len()).unwrap_or(u64::MAX),
        stderr_bytes: u64::try_from(input.output.stderr.len()).unwrap_or(u64::MAX),
        stdin_bytes: 0,
    }
}

fn command_digest(tool_sha256: &str, arguments: &[String]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(tool_sha256.as_bytes());
    for argument in arguments {
        hasher.update([0]);
        hasher.update(argument.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

async fn fail_reservation(state: &GatewayState, engagement_id: &str, use_id: &str) {
    let _ = state
        .store
        .complete_credential_use(
            engagement_id,
            use_id,
            CredentialUseOutcome::ExecutionFailed,
            unix_timestamp(),
        )
        .await;
}

#[cfg(test)]
#[path = "credential_execution_tests.rs"]
mod tests;
