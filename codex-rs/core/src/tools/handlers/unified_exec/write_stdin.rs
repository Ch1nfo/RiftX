use crate::function_tool::FunctionCallError;
use crate::tools::context::ToolInvocation;
use crate::tools::context::ToolPayload;
use crate::tools::context::boxed_tool_output;
use crate::tools::handlers::parse_arguments;
use crate::tools::registry::CoreToolRuntime;
use crate::tools::registry::PostToolUsePayload;
use crate::tools::registry::PreToolUsePayload;
use crate::tools::registry::ToolExecutor;
use crate::unified_exec::WriteStdinApprovalContext;
use crate::unified_exec::WriteStdinRequest;
use codex_protocol::protocol::AskForApproval;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::ReviewDecision;
use codex_protocol::protocol::TerminalInteractionEvent;
use codex_tools::ToolName;
use codex_tools::ToolSpec;
use serde::Deserialize;

use super::super::shell_spec::create_write_stdin_tool;
use super::post_unified_exec_tool_use_payload;

const INTERRUPT: &str = "\u{3}";
const MAX_APPROVED_STDIN_BYTES: usize = 8 * 1024;

#[derive(Debug, Deserialize)]
struct WriteStdinArgs {
    // The model is trained on `session_id`.
    session_id: i32,
    #[serde(default)]
    chars: String,
    #[serde(default = "super::default_write_stdin_yield_time_ms")]
    yield_time_ms: u64,
    #[serde(default)]
    max_output_tokens: Option<usize>,
}

pub struct WriteStdinHandler;

impl ToolExecutor<ToolInvocation> for WriteStdinHandler {
    fn tool_name(&self) -> ToolName {
        ToolName::plain("write_stdin")
    }

    fn spec(&self) -> ToolSpec {
        create_write_stdin_tool()
    }

    fn supports_parallel_tool_calls(&self) -> bool {
        true
    }

    fn handle(&self, invocation: ToolInvocation) -> codex_tools::ToolExecutorFuture<'_> {
        Box::pin(self.handle_call(invocation))
    }
}

impl WriteStdinHandler {
    async fn handle_call(
        &self,
        invocation: ToolInvocation,
    ) -> Result<Box<dyn crate::tools::context::ToolOutput>, FunctionCallError> {
        let ToolInvocation {
            session,
            turn,
            call_id,
            payload,
            ..
        } = invocation;

        let arguments = match payload {
            ToolPayload::Function { arguments } => arguments,
            _ => {
                return Err(FunctionCallError::RespondToModel(
                    "write_stdin handler received unsupported payload".to_string(),
                ));
            }
        };

        let args: WriteStdinArgs = parse_arguments(&arguments)?;
        if write_stdin_input_needs_policy_review(turn.approval_policy.value(), &args.chars) {
            let approval = session
                .services
                .unified_exec_manager
                .write_stdin_approval_context(args.session_id)
                .await
                .map_err(|err| {
                    FunctionCallError::RespondToModel(format!("write_stdin failed: {err}"))
                })?;
            if approval.tty {
                if args.chars.len() > MAX_APPROVED_STDIN_BYTES {
                    return Err(FunctionCallError::RespondToModel(format!(
                        "write_stdin input exceeds the {MAX_APPROVED_STDIN_BYTES}-byte approval limit"
                    )));
                }
                let cwd = approval.cwd.to_abs_path().map_err(|err| {
                    FunctionCallError::RespondToModel(format!(
                        "write_stdin cannot approve a non-local terminal working directory: {err}"
                    ))
                })?;
                let approval_id = format!("{call_id}:pty-stdin");
                let decision = session
                    .request_command_approval(
                        turn.as_ref(),
                        call_id,
                        Some(approval_id),
                        /*environment_id*/ None,
                        write_stdin_approval_command(&approval, args.session_id, &args.chars),
                        cwd,
                        Some(
                            "Interactive terminal input requires execution policy review"
                                .to_string(),
                        ),
                        /*network_approval_context*/ None,
                        /*proposed_execpolicy_amendment*/ None,
                        /*additional_permissions*/ None,
                        /*available_decisions*/ None,
                    )
                    .await;
                if !matches!(
                    decision,
                    ReviewDecision::Approved
                        | ReviewDecision::ApprovedForSession
                        | ReviewDecision::ApprovedExecpolicyAmendment { .. }
                ) {
                    return Err(FunctionCallError::RespondToModel(
                        "write_stdin was denied by execution policy".to_string(),
                    ));
                }
            }
        }
        let response = session
            .services
            .unified_exec_manager
            .write_stdin(WriteStdinRequest {
                process_id: args.session_id,
                input: &args.chars,
                yield_time_ms: args.yield_time_ms,
                max_output_tokens: args.max_output_tokens,
                truncation_policy: turn.model_info.truncation_policy.into(),
            })
            .await
            .map_err(|err| {
                FunctionCallError::RespondToModel(format!("write_stdin failed: {err}"))
            })?;

        // Empty stdin is a background poll, so emit it only while there is
        // still a live process for the UI to wait on. Non-empty stdin is a real
        // terminal interaction and should remain visible even if it completes
        // the process before the response returns.
        if !args.chars.is_empty() || response.process_id.is_some() {
            let process_id = response.process_id.unwrap_or(args.session_id);
            let interaction = TerminalInteractionEvent {
                call_id: response.event_call_id.clone(),
                process_id: process_id.to_string(),
                stdin: args.chars.clone(),
            };
            session
                .send_event(turn.as_ref(), EventMsg::TerminalInteraction(interaction))
                .await;
        }

        Ok(boxed_tool_output(response))
    }
}

impl CoreToolRuntime for WriteStdinHandler {
    fn matches_kind(&self, payload: &ToolPayload) -> bool {
        matches!(payload, ToolPayload::Function { .. })
    }

    fn pre_tool_use_payload(&self, _invocation: &ToolInvocation) -> Option<PreToolUsePayload> {
        // `write_stdin` is transport for an existing exec session. Empty writes
        // are background polls. Non-empty PTY writes are reviewed through the
        // command approval channel above, while Bash hooks remain paired with
        // the original exec command.
        None
    }

    fn post_tool_use_payload(
        &self,
        invocation: &ToolInvocation,
        result: &dyn crate::tools::context::ToolOutput,
    ) -> Option<PostToolUsePayload> {
        // A `write_stdin` poll can observe final completion for the original
        // `exec_command`; emit that command's matching Bash PostToolUse.
        post_unified_exec_tool_use_payload(invocation, result)
    }
}

fn write_stdin_input_needs_policy_review(policy: AskForApproval, input: &str) -> bool {
    policy == AskForApproval::Always && !input.is_empty() && input != INTERRUPT
}

fn write_stdin_approval_command(
    context: &WriteStdinApprovalContext,
    process_id: i32,
    input: &str,
) -> Vec<String> {
    let mut command = context.command.clone();
    command.push("<pty-stdin>".to_string());
    command.push(format!("session={process_id}"));
    command.push(input.to_string());
    command
}

#[cfg(test)]
#[path = "write_stdin_tests.rs"]
mod tests;
