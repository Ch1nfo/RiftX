use crate::gateway_state::GatewayState;
use codex_riftx_core::AutoProgressAction;
use codex_riftx_core::AutoProgressAssessment;
use codex_riftx_core::AutoProgressCategory;
use codex_riftx_core::AutoProgressSignal;
use codex_riftx_core::AutoProgressSnapshot;
use codex_riftx_core::AutoRun;
use codex_riftx_core::StateError;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;

pub(crate) const AUTO_PROGRESS_EVALUATOR_VERSION: &str = "riftx-auto-progress-v1";

pub(crate) async fn snapshot(
    state: &GatewayState,
    engagement_id: &str,
) -> Result<AutoProgressSnapshot, StateError> {
    Ok(AutoProgressSnapshot {
        assets: fingerprint(state.store.assets(engagement_id).await?)?,
        services: fingerprint(state.store.services(engagement_id).await?)?,
        identities: fingerprint(state.store.identities(engagement_id).await?)?,
        findings: fingerprint(state.store.findings(engagement_id).await?)?,
        evidence: fingerprint(state.store.evidence(engagement_id).await?)?,
        artifacts: fingerprint(state.store.artifacts(engagement_id).await?)?,
        attack_paths: fingerprint(state.store.attack_paths(engagement_id).await?)?,
        coverage: fingerprint(state.store.coverage(engagement_id).await?)?,
        observations: fingerprint(state.store.observations(engagement_id).await?)?,
        hypotheses: fingerprint(state.store.hypotheses(engagement_id).await?)?,
    })
}

pub(crate) async fn evaluate(
    state: &GatewayState,
    run: &AutoRun,
    evaluated_at: i64,
) -> Result<AutoProgressAssessment, StateError> {
    let current = snapshot(state, &run.engagement_id).await?;
    let Some(baseline) = run.progress_baseline.as_ref() else {
        return Ok(AutoProgressAssessment {
            evaluator_version: AUTO_PROGRESS_EVALUATOR_VERSION.to_string(),
            evaluated_at,
            progressed: true,
            signals: Vec::new(),
            no_progress_turns: run.no_progress_turns,
            action: AutoProgressAction::Continue,
        });
    };
    let mut signals = Vec::new();
    push_signal(
        &mut signals,
        AutoProgressSignal::Asset,
        &baseline.assets,
        &current.assets,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Service,
        &baseline.services,
        &current.services,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Identity,
        &baseline.identities,
        &current.identities,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Finding,
        &baseline.findings,
        &current.findings,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Evidence,
        &baseline.evidence,
        &current.evidence,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Artifact,
        &baseline.artifacts,
        &current.artifacts,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::AttackPath,
        &baseline.attack_paths,
        &current.attack_paths,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Coverage,
        &baseline.coverage,
        &current.coverage,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Observation,
        &baseline.observations,
        &current.observations,
    );
    push_signal(
        &mut signals,
        AutoProgressSignal::Hypothesis,
        &baseline.hypotheses,
        &current.hypotheses,
    );
    let progressed = !signals.is_empty();
    let no_progress_turns = if progressed {
        0
    } else {
        run.no_progress_turns.saturating_add(1)
    };
    Ok(AutoProgressAssessment {
        evaluator_version: AUTO_PROGRESS_EVALUATOR_VERSION.to_string(),
        evaluated_at,
        progressed,
        signals,
        no_progress_turns,
        action: progress_action(no_progress_turns, run.config.limits.no_progress_window),
    })
}

fn fingerprint(values: Vec<impl Serialize>) -> Result<AutoProgressCategory, StateError> {
    let item_count = u32::try_from(values.len()).unwrap_or(u32::MAX);
    let mut encoded = values
        .into_iter()
        .map(|value| serde_json::to_vec(&value))
        .collect::<Result<Vec<_>, _>>()?;
    encoded.sort();
    let mut hasher = Sha256::new();
    for value in encoded {
        hasher.update(u64::try_from(value.len()).unwrap_or(u64::MAX).to_be_bytes());
        hasher.update(value);
    }
    Ok(AutoProgressCategory {
        item_count,
        sha256: hasher
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
    })
}

fn push_signal(
    signals: &mut Vec<AutoProgressSignal>,
    signal: AutoProgressSignal,
    baseline: &AutoProgressCategory,
    current: &AutoProgressCategory,
) {
    if current.item_count > baseline.item_count
        || (current.item_count == baseline.item_count && current.sha256 != baseline.sha256)
    {
        signals.push(signal);
    }
}

fn progress_action(no_progress_turns: u32, window: u32) -> AutoProgressAction {
    if no_progress_turns == 0 {
        AutoProgressAction::Continue
    } else if no_progress_turns >= window.max(1) {
        AutoProgressAction::NeedsInput
    } else if no_progress_turns == 1 {
        AutoProgressAction::Replan
    } else {
        AutoProgressAction::SwitchStrategy
    }
}

#[cfg(test)]
#[path = "auto_progress_tests.rs"]
mod tests;
