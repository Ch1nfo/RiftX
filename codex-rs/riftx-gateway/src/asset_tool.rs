use crate::engagement_stop::AgentThreadDisposition;
use crate::gateway_state::GatewayState;
use crate::gateway_state::unix_timestamp;
use codex_riftx_app_server_adapter::PendingDynamicToolCall;
use codex_riftx_core::Asset;
use codex_riftx_core::ExecutionMode;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use std::net::IpAddr;
use url::Host;
use uuid::Uuid;

const MAX_ASSET_VALUE_BYTES: usize = 2_048;

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum AssetKind {
    Host,
    Domain,
    Url,
}

impl AssetKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Host => "host",
            Self::Domain => "domain",
            Self::Url => "url",
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordAssetParams {
    kind: AssetKind,
    value: String,
}

pub(crate) async fn handle(
    state: &GatewayState,
    profile_name: &str,
    pending: PendingDynamicToolCall,
) {
    let Some(app_server) = state.app_server(profile_name) else {
        return;
    };
    let Some(engagement_id) = state
        .thread_engagements
        .read()
        .await
        .get(&pending.params.thread_id)
        .cloned()
    else {
        let _ = app_server
            .reject_dynamic_tool(
                pending,
                "RiftX could not bind the asset to an engagement".to_string(),
            )
            .await;
        return;
    };
    let Ok(engagement) = state.store.engagement(&engagement_id).await else {
        let _ = app_server
            .reject_dynamic_tool(
                pending,
                "RiftX could not load the engagement for this asset".to_string(),
            )
            .await;
        return;
    };
    let params = match serde_json::from_value::<RecordAssetParams>(pending.params.arguments.clone())
    {
        Ok(params) => params,
        Err(error) => {
            let _ = app_server
                .resolve_dynamic_tool_text(
                    pending,
                    format!("RiftX rejected invalid asset arguments: {error}"),
                    false,
                )
                .await;
            return;
        }
    };
    let value = match normalize_asset(params.kind, &params.value) {
        Ok(value) => value,
        Err(message) => {
            let _ = app_server
                .resolve_dynamic_tool_text(pending, message, false)
                .await;
            return;
        }
    };
    let policy =
        match crate::credential_api::resolve_engagement_policy(state, &engagement, engagement.mode)
            .await
        {
            Ok(policy) => policy,
            Err(error) => {
                let _ = app_server
                    .resolve_dynamic_tool_text(
                        pending,
                        format!(
                            "RiftX could not resolve the current authorization policy: {error}"
                        ),
                        false,
                    )
                    .await;
                return;
            }
        };
    if let Err(error) = policy.check_target(&value) {
        let question = format!(
            "Discovered {} {:?} is outside the declared scope. Confirm a revised authorization before continuing",
            params.kind.as_str(),
            value,
        );
        let _ = crate::auto_run::needs_input(state, &engagement, &question).await;
        if engagement.mode == ExecutionMode::Auto {
            state
                .stop_engagement_work(&engagement_id, AgentThreadDisposition::Preserve)
                .await;
        }
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                format!("RiftX rejected the asset because it is outside scope: {error}"),
                false,
            )
            .await;
        return;
    }
    if !crate::auto_run::record_tool_call(state, &engagement)
        .await
        .unwrap_or(false)
    {
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                "RiftX denied the asset tool because the Auto tool budget is exhausted".to_string(),
                false,
            )
            .await;
        return;
    }
    let assets = match state.store.assets(&engagement_id).await {
        Ok(assets) => assets,
        Err(error) => {
            let _ = app_server
                .resolve_dynamic_tool_text(
                    pending,
                    format!("RiftX could not inspect existing assets: {error}"),
                    false,
                )
                .await;
            return;
        }
    };
    let existing = assets
        .into_iter()
        .find(|asset| asset.kind == params.kind.as_str() && asset.value == value);
    if let Some(asset) = existing {
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                format!("RiftX already recorded in-scope asset {}", asset.id),
                true,
            )
            .await;
        return;
    }
    let asset = Asset {
        id: Uuid::new_v4().to_string(),
        engagement_id: engagement_id.clone(),
        kind: params.kind.as_str().to_string(),
        value,
        discovered_at: unix_timestamp(),
    };
    let audit_data = json!({
        "assetId": asset.id,
        "kind": asset.kind,
        "value": asset.value,
        "source": "dynamicTool",
        "policyRevision": engagement.policy_revision,
    });
    if state
        .append_engagement_critical(&engagement, "asset/recordAccepted", &audit_data)
        .await
        .is_err()
    {
        let _ = crate::auto_run::lifecycle_stop(
            state,
            &engagement_id,
            crate::auto_run::AutoLifecycleStop::AuditUnavailable,
        )
        .await;
        if engagement.mode == ExecutionMode::Auto {
            state
                .stop_engagement_work(&engagement_id, AgentThreadDisposition::Preserve)
                .await;
        }
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                "RiftX could not record the asset because the audit log is unavailable".to_string(),
                false,
            )
            .await;
        return;
    }
    if let Err(error) = state.store.put_asset(&asset).await {
        state
            .emit_event(
                &engagement_id,
                "auto/controllerError",
                json!({"message": "accepted asset could not be persisted"}),
            )
            .await;
        let _ = app_server
            .resolve_dynamic_tool_text(
                pending,
                format!("RiftX could not persist the accepted asset: {error}"),
                false,
            )
            .await;
        return;
    }
    state
        .emit_event(&engagement_id, "asset/recorded", audit_data)
        .await;
    let _ = app_server
        .resolve_dynamic_tool_text(
            pending,
            format!(
                "RiftX recorded in-scope asset {} ({}: {})",
                asset.id, asset.kind, asset.value
            ),
            true,
        )
        .await;
}

fn normalize_asset(kind: AssetKind, raw: &str) -> Result<String, String> {
    let value = raw.trim();
    if value.is_empty() || value.len() > MAX_ASSET_VALUE_BYTES {
        return Err(format!(
            "RiftX requires an asset value between 1 and {MAX_ASSET_VALUE_BYTES} bytes"
        ));
    }
    match kind {
        AssetKind::Host => value
            .parse::<IpAddr>()
            .map(|address| address.to_string())
            .map_err(|_| "RiftX host assets must be IPv4 or IPv6 addresses".to_string()),
        AssetKind::Domain => match Host::parse(value) {
            Ok(Host::Domain(domain)) => Ok(domain.trim_end_matches('.').to_ascii_lowercase()),
            Ok(Host::Ipv4(_) | Host::Ipv6(_)) | Err(_) => {
                Err("RiftX domain assets must be valid DNS names".to_string())
            }
        },
        AssetKind::Url => {
            let mut parsed = url::Url::parse(value)
                .map_err(|_| "RiftX URL assets must be absolute URLs".to_string())?;
            if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
                return Err(
                    "RiftX URL assets must use HTTP or HTTPS and include a host".to_string()
                );
            }
            if !parsed.username().is_empty() || parsed.password().is_some() {
                return Err("RiftX URL assets must not contain credentials".to_string());
            }
            parsed.set_fragment(None);
            Ok(parsed.to_string())
        }
    }
}

#[cfg(test)]
#[path = "asset_tool_tests.rs"]
mod tests;
