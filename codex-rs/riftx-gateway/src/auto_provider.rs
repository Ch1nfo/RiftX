use crate::auto_run::AutoLifecycleStop;
use crate::engagement_stop::AgentThreadDisposition;
use crate::gateway_state::GatewayState;
use codex_riftx_app_server_adapter::CodexErrorInfo;
use codex_riftx_app_server_adapter::ErrorNotification;
use codex_riftx_app_server_adapter::TurnError;
use codex_riftx_core::AutoRunState;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use serde_json::json;
use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProviderErrorClass {
    Authentication,
    Protocol,
    RateLimited,
}

pub(crate) async fn handle(
    state: &GatewayState,
    engagement_id: &str,
    notification: &ErrorNotification,
) {
    let Some((class, status_code)) = classify_error(&notification.error) else {
        return;
    };
    if class == ProviderErrorClass::RateLimited {
        state
            .emit_event(
                engagement_id,
                "auto/providerRetrying",
                json!({
                    "class": class_name(class),
                    "httpStatusCode": status_code,
                    "runtimeManaged": notification.will_retry,
                }),
            )
            .await;
        return;
    }

    let Ok(_permit) = state.turn_slot.clone().acquire_owned().await else {
        return;
    };
    let Ok(engagement) = state.store.engagement(engagement_id).await else {
        return;
    };
    let _ = stop_terminal_locked(state, &engagement, class, status_code).await;
}

pub(crate) async fn handle_turn_error_locked(
    state: &GatewayState,
    engagement: &Engagement,
    error: &TurnError,
) -> bool {
    let Some((class, status_code)) = classify_error(error) else {
        return false;
    };
    if class == ProviderErrorClass::RateLimited {
        backoff_after_rate_limit(state, &engagement.id, status_code).await;
        return false;
    }
    stop_terminal_locked(state, engagement, class, status_code).await
}

async fn stop_terminal_locked(
    state: &GatewayState,
    engagement: &Engagement,
    class: ProviderErrorClass,
    status_code: Option<u16>,
) -> bool {
    if engagement.mode != ExecutionMode::Auto || engagement.status != EngagementStatus::Active {
        return false;
    }
    let lifecycle_stop = match class {
        ProviderErrorClass::Authentication => AutoLifecycleStop::ProviderAuthentication,
        ProviderErrorClass::Protocol => AutoLifecycleStop::ProviderProtocolError,
        ProviderErrorClass::RateLimited => return false,
    };
    let audit_result = state
        .append_engagement_critical(
            engagement,
            "auto/providerStopped",
            &json!({
                "class": class_name(class),
                "httpStatusCode": status_code,
            }),
        )
        .await;
    if audit_result.is_err() {
        let _ = crate::auto_run::lifecycle_stop(
            state,
            &engagement.id,
            AutoLifecycleStop::AuditUnavailable,
        )
        .await;
        return true;
    }
    state
        .stop_engagement_work(
            &engagement.id,
            match class {
                ProviderErrorClass::Authentication => AgentThreadDisposition::Preserve,
                ProviderErrorClass::Protocol => AgentThreadDisposition::Remove,
                ProviderErrorClass::RateLimited => return false,
            },
        )
        .await;
    let _ = crate::auto_run::lifecycle_stop(state, &engagement.id, lifecycle_stop).await;
    true
}

async fn backoff_after_rate_limit(
    state: &GatewayState,
    engagement_id: &str,
    status_code: Option<u16>,
) {
    let Ok(Some(run)) = state.store.auto_run(engagement_id).await else {
        return;
    };
    if run.state != AutoRunState::Running {
        return;
    }
    let retry_attempt = run.consecutive_failures.saturating_add(1);
    let exponent = retry_attempt.saturating_sub(1).min(5);
    let delay_seconds = 1_u64.checked_shl(exponent).unwrap_or(32);
    state
        .emit_event(
            engagement_id,
            "auto/providerRetrying",
            json!({
                "class": class_name(ProviderErrorClass::RateLimited),
                "httpStatusCode": status_code,
                "runtimeManaged": false,
                "retryAttempt": retry_attempt,
                "retryBudget": run.config.limits.max_consecutive_failures,
                "delaySeconds": delay_seconds,
            }),
        )
        .await;
    tokio::time::sleep(Duration::from_secs(delay_seconds)).await;
}

fn classify_error(error: &TurnError) -> Option<(ProviderErrorClass, Option<u16>)> {
    let status_code = error
        .codex_error_info
        .as_ref()
        .and_then(error_status_code)
        .or_else(|| status_code_from_message(&error.message));
    let class = match error.codex_error_info.as_ref() {
        Some(CodexErrorInfo::Unauthorized) => Some(ProviderErrorClass::Authentication),
        Some(CodexErrorInfo::ServerOverloaded) => Some(ProviderErrorClass::RateLimited),
        Some(
            CodexErrorInfo::ContextWindowExceeded
            | CodexErrorInfo::SessionBudgetExceeded
            | CodexErrorInfo::UsageLimitExceeded
            | CodexErrorInfo::CyberPolicy
            | CodexErrorInfo::InternalServerError
            | CodexErrorInfo::BadRequest
            | CodexErrorInfo::ThreadRollbackFailed
            | CodexErrorInfo::SandboxError
            | CodexErrorInfo::ActiveTurnNotSteerable { .. }
            | CodexErrorInfo::Other,
        )
        | None => class_from_status(status_code),
        Some(
            CodexErrorInfo::HttpConnectionFailed { .. }
            | CodexErrorInfo::ResponseStreamConnectionFailed { .. }
            | CodexErrorInfo::ResponseStreamDisconnected { .. }
            | CodexErrorInfo::ResponseTooManyFailedAttempts { .. },
        ) => class_from_status(status_code),
    }?;
    Some((class, status_code))
}

fn error_status_code(error: &CodexErrorInfo) -> Option<u16> {
    match error {
        CodexErrorInfo::HttpConnectionFailed { http_status_code }
        | CodexErrorInfo::ResponseStreamConnectionFailed { http_status_code }
        | CodexErrorInfo::ResponseStreamDisconnected { http_status_code }
        | CodexErrorInfo::ResponseTooManyFailedAttempts { http_status_code } => *http_status_code,
        CodexErrorInfo::ContextWindowExceeded
        | CodexErrorInfo::SessionBudgetExceeded
        | CodexErrorInfo::UsageLimitExceeded
        | CodexErrorInfo::ServerOverloaded
        | CodexErrorInfo::CyberPolicy
        | CodexErrorInfo::InternalServerError
        | CodexErrorInfo::Unauthorized
        | CodexErrorInfo::BadRequest
        | CodexErrorInfo::ThreadRollbackFailed
        | CodexErrorInfo::SandboxError
        | CodexErrorInfo::ActiveTurnNotSteerable { .. }
        | CodexErrorInfo::Other => None,
    }
}

fn class_from_status(status_code: Option<u16>) -> Option<ProviderErrorClass> {
    match status_code {
        Some(401 | 403) => Some(ProviderErrorClass::Authentication),
        Some(404) => Some(ProviderErrorClass::Protocol),
        Some(429) => Some(ProviderErrorClass::RateLimited),
        Some(_) | None => None,
    }
}

fn status_code_from_message(message: &str) -> Option<u16> {
    ["unexpected status ", "(status "]
        .into_iter()
        .find_map(|marker| {
            let suffix = message.split_once(marker)?.1;
            suffix
                .split_whitespace()
                .next()?
                .trim_matches(|character: char| !character.is_ascii_digit())
                .parse()
                .ok()
        })
}

fn class_name(class: ProviderErrorClass) -> &'static str {
    match class {
        ProviderErrorClass::Authentication => "authentication",
        ProviderErrorClass::Protocol => "protocol",
        ProviderErrorClass::RateLimited => "rateLimited",
    }
}

#[cfg(test)]
#[path = "auto_provider_tests.rs"]
mod tests;
