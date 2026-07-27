use crate::gateway_state::GatewayState;
use codex_riftx_core::AutoCriterionAssessment;
use codex_riftx_core::AutoGoalAssessment;
use codex_riftx_core::AutoRun;
use codex_riftx_core::FindingSeverity;
use codex_riftx_core::StateError;
use codex_riftx_core::SuccessPredicate;
use std::collections::BTreeMap;
use std::collections::BTreeSet;

pub(crate) const AUTO_GOAL_EVALUATOR_VERSION: &str = "riftx-auto-goal-v1";

#[derive(Clone, Copy)]
struct EvidenceStatus {
    valid: bool,
    reproducible: bool,
}

pub(crate) async fn evaluate(
    state: &GatewayState,
    run: &AutoRun,
    evaluated_at: i64,
) -> Result<AutoGoalAssessment, StateError> {
    let engagement_id = &run.engagement_id;
    let chain_valid = match state.store.validate_evidence_chain(engagement_id).await {
        Ok(()) => true,
        Err(
            StateError::MissingChainReference { .. }
            | StateError::BrokenChainReference { .. }
            | StateError::InvalidTargetState(_),
        ) => false,
        Err(error) => return Err(error),
    };
    let executions = state.store.executions(engagement_id).await?;
    let execution_ids = executions
        .into_iter()
        .filter(|execution| execution.engagement_id == *engagement_id)
        .map(|execution| execution.id)
        .collect::<BTreeSet<_>>();
    let artifacts = state
        .store
        .artifacts(engagement_id)
        .await?
        .into_iter()
        .filter(|artifact| artifact.engagement_id == *engagement_id)
        .map(|artifact| (artifact.id.clone(), artifact))
        .collect::<BTreeMap<_, _>>();
    let evidence = state.store.evidence(engagement_id).await?;
    let mut evidence_status = BTreeMap::new();
    for item in evidence {
        let execution_valid = item
            .execution_id
            .as_ref()
            .is_some_and(|execution_id| execution_ids.contains(execution_id));
        let artifact_readable = match item.artifact_id.as_ref() {
            Some(artifact_id) => match artifacts.get(artifact_id) {
                Some(artifact) => state.artifact_store.open(artifact).await.is_ok(),
                None => false,
            },
            None => true,
        };
        let valid = chain_valid
            && item.engagement_id == *engagement_id
            && !item.summary.trim().is_empty()
            && execution_valid
            && artifact_readable;
        evidence_status.insert(
            item.id,
            EvidenceStatus {
                valid,
                reproducible: valid && item.reproducible && item.artifact_id.is_some(),
            },
        );
    }

    let mut criteria = Vec::with_capacity(run.config.objective.structured_criteria.len());
    for criterion in &run.config.objective.structured_criteria {
        let mut evidence_ids = match &criterion.predicate {
            SuccessPredicate::Evidence {
                minimum_items,
                reproduction_required,
            } => {
                let matching = evidence_status
                    .iter()
                    .filter(|&(_id, status)| {
                        status.valid && (!reproduction_required || status.reproducible)
                    })
                    .map(|(id, _status)| id.clone())
                    .collect::<Vec<_>>();
                (matching.len() >= *minimum_items as usize).then_some(matching)
            }
            SuccessPredicate::Coverage {
                minimum_basis_points,
            } => state
                .store
                .coverage(engagement_id)
                .await?
                .into_iter()
                .filter(|coverage| coverage.engagement_id == *engagement_id)
                .filter(|coverage| {
                    u128::from(coverage.covered_items) * 10_000
                        >= u128::from(coverage.total_items) * u128::from(*minimum_basis_points)
                })
                .find_map(|coverage| {
                    validated_evidence_ids(&coverage.evidence_ids, &evidence_status)
                }),
            SuccessPredicate::Finding {
                minimum_count,
                minimum_severity,
                minimum_confidence_basis_points,
            } => {
                let mut matched = 0_u32;
                let mut supporting = Vec::new();
                for finding in state.store.findings(engagement_id).await? {
                    if finding.engagement_id != *engagement_id
                        || severity_rank(finding.severity) < severity_rank(*minimum_severity)
                        || finding.confidence_basis_points > 10_000
                        || finding.confidence_basis_points < *minimum_confidence_basis_points
                    {
                        continue;
                    }
                    if let Some(ids) =
                        validated_evidence_ids(&finding.evidence_ids, &evidence_status)
                    {
                        matched = matched.saturating_add(1);
                        supporting.extend(ids);
                    }
                }
                (matched >= *minimum_count).then_some(supporting)
            }
            SuccessPredicate::AttackPath {
                destination_role,
                access_level,
                minimum_confidence_basis_points,
                reproducible_evidence,
            } => state
                .store
                .attack_paths(engagement_id)
                .await?
                .into_iter()
                .filter(|path| {
                    path.engagement_id == *engagement_id
                        && path.destination_role == *destination_role
                        && path.access_level == *access_level
                        && path.confidence_basis_points >= *minimum_confidence_basis_points
                        && (!reproducible_evidence || path.reproducible)
                })
                .find_map(|path| {
                    let ids = path
                        .hops
                        .iter()
                        .flat_map(|hop| hop.evidence_ids.iter().cloned())
                        .collect::<Vec<_>>();
                    let ids = validated_evidence_ids(&ids, &evidence_status)?;
                    if *reproducible_evidence
                        && ids.iter().any(|id| {
                            !evidence_status
                                .get(id)
                                .is_some_and(|status| status.reproducible)
                        })
                    {
                        return None;
                    }
                    Some(ids)
                }),
        }
        .unwrap_or_default();
        evidence_ids.sort();
        evidence_ids.dedup();
        criteria.push(AutoCriterionAssessment {
            criterion_id: criterion.id.clone(),
            satisfied: !evidence_ids.is_empty(),
            evidence_ids,
        });
    }

    let mut evidence_ids = criteria
        .iter()
        .flat_map(|criterion| criterion.evidence_ids.iter().cloned())
        .collect::<Vec<_>>();
    evidence_ids.sort();
    evidence_ids.dedup();
    let succeeded = !criteria.is_empty()
        && criteria.iter().all(|criterion| criterion.satisfied)
        && !evidence_ids.is_empty();
    Ok(AutoGoalAssessment {
        evaluator_version: AUTO_GOAL_EVALUATOR_VERSION.to_string(),
        evaluated_at,
        succeeded,
        criteria,
        evidence_ids,
    })
}

fn validated_evidence_ids(
    ids: &[String],
    evidence: &BTreeMap<String, EvidenceStatus>,
) -> Option<Vec<String>> {
    if ids.is_empty()
        || ids
            .iter()
            .any(|id| !evidence.get(id).is_some_and(|status| status.valid))
    {
        return None;
    }
    Some(ids.to_vec())
}

fn severity_rank(severity: FindingSeverity) -> u8 {
    match severity {
        FindingSeverity::Info => 0,
        FindingSeverity::Low => 1,
        FindingSeverity::Medium => 2,
        FindingSeverity::High => 3,
        FindingSeverity::Critical => 4,
    }
}

#[cfg(test)]
#[path = "auto_evaluator_tests.rs"]
mod tests;
