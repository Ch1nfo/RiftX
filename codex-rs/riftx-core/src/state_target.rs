use super::*;
use crate::AttackPath;
use crate::Coverage;
use crate::Execution;
use crate::Hypothesis;
use crate::Identity;
use crate::Observation;
use crate::StateSubject;
use crate::TestCase;
use std::collections::BTreeSet;

impl StateStore {
    pub async fn put_identity(&self, value: &Identity) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::Identities,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_observation(&self, value: &Observation) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::Observations,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_hypothesis(&self, value: &Hypothesis) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::Hypotheses,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_test_case(&self, value: &TestCase) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::TestCases,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_execution(&self, value: &Execution) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::Executions,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_attack_path(&self, value: &AttackPath) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::AttackPaths,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn put_coverage(&self, value: &Coverage) -> Result<(), StateError> {
        value.validate()?;
        self.put_entity(
            EntityTable::Coverage,
            &value.id,
            &value.engagement_id,
            value,
        )
        .await
    }

    pub async fn identities(&self, engagement_id: &str) -> Result<Vec<Identity>, StateError> {
        self.entities(EntityTable::Identities, engagement_id).await
    }

    pub async fn observations(&self, engagement_id: &str) -> Result<Vec<Observation>, StateError> {
        self.entities(EntityTable::Observations, engagement_id)
            .await
    }

    pub async fn hypotheses(&self, engagement_id: &str) -> Result<Vec<Hypothesis>, StateError> {
        self.entities(EntityTable::Hypotheses, engagement_id).await
    }

    pub async fn test_cases(&self, engagement_id: &str) -> Result<Vec<TestCase>, StateError> {
        self.entities(EntityTable::TestCases, engagement_id).await
    }

    pub async fn executions(&self, engagement_id: &str) -> Result<Vec<Execution>, StateError> {
        self.entities(EntityTable::Executions, engagement_id).await
    }

    pub async fn attack_paths(&self, engagement_id: &str) -> Result<Vec<AttackPath>, StateError> {
        self.entities(EntityTable::AttackPaths, engagement_id).await
    }

    pub async fn coverage(&self, engagement_id: &str) -> Result<Vec<Coverage>, StateError> {
        self.entities(EntityTable::Coverage, engagement_id).await
    }

    pub async fn validate_evidence_chain(&self, engagement_id: &str) -> Result<(), StateError> {
        let assets = ids(self.assets(engagement_id).await?);
        let services = ids(self.services(engagement_id).await?);
        let identities = ids(self.identities(engagement_id).await?);
        let observations = self.observations(engagement_id).await?;
        let observation_ids = ids(observations.clone());
        let hypotheses = self.hypotheses(engagement_id).await?;
        let hypothesis_ids = ids(hypotheses.clone());
        let test_cases = self.test_cases(engagement_id).await?;
        let test_case_ids = ids(test_cases.clone());
        let executions = self.executions(engagement_id).await?;
        let execution_ids = ids(executions.clone());
        let evidence = self.evidence(engagement_id).await?;
        let evidence_ids = ids(evidence.clone());

        for observation in observations {
            check_subject(
                "observation",
                &observation.id,
                &observation.subject,
                &assets,
                &services,
                &identities,
            )?;
            if let Some(execution_id) = observation.execution_id {
                check_reference(
                    "observation",
                    &observation.id,
                    "execution",
                    &execution_id,
                    &execution_ids,
                )?;
            }
        }
        for hypothesis in hypotheses {
            check_references(
                "hypothesis",
                &hypothesis.id,
                "observation",
                &hypothesis.observation_ids,
                &observation_ids,
            )?;
        }
        for test_case in test_cases {
            check_reference(
                "testCase",
                &test_case.id,
                "hypothesis",
                &test_case.hypothesis_id,
                &hypothesis_ids,
            )?;
            check_subject(
                "testCase",
                &test_case.id,
                &test_case.target,
                &assets,
                &services,
                &identities,
            )?;
        }
        for execution in executions {
            if let Some(test_case_id) = execution.test_case_id {
                check_reference(
                    "execution",
                    &execution.id,
                    "testCase",
                    &test_case_id,
                    &test_case_ids,
                )?;
            }
        }
        for evidence in evidence {
            let execution_id = evidence.execution_id.as_deref().ok_or_else(|| {
                StateError::MissingChainReference {
                    entity_kind: "evidence",
                    entity_id: evidence.id.clone(),
                    reference_kind: "execution",
                }
            })?;
            check_reference(
                "evidence",
                &evidence.id,
                "execution",
                execution_id,
                &execution_ids,
            )?;
        }
        for finding in self.findings(engagement_id).await? {
            check_references(
                "finding",
                &finding.id,
                "evidence",
                &finding.evidence_ids,
                &evidence_ids,
            )?;
        }
        for path in self.attack_paths(engagement_id).await? {
            for hop in path.hops {
                check_subject(
                    "attackPath",
                    &path.id,
                    &hop.source,
                    &assets,
                    &services,
                    &identities,
                )?;
                check_subject(
                    "attackPath",
                    &path.id,
                    &hop.destination,
                    &assets,
                    &services,
                    &identities,
                )?;
                check_references(
                    "attackPath",
                    &path.id,
                    "evidence",
                    &hop.evidence_ids,
                    &evidence_ids,
                )?;
            }
        }
        Ok(())
    }
}

/// Supplies the stable identifier used to compare references across state tables.
trait StateEntity {
    fn id(&self) -> &str;
}

macro_rules! state_entity {
    ($($entity:ty),+ $(,)?) => {
        $(
            impl StateEntity for $entity {
                fn id(&self) -> &str {
                    &self.id
                }
            }
        )+
    };
}

state_entity!(
    crate::Asset,
    crate::Service,
    Identity,
    Observation,
    Hypothesis,
    TestCase,
    Execution,
    crate::Evidence,
);

fn ids(values: Vec<impl StateEntity>) -> BTreeSet<String> {
    values
        .into_iter()
        .map(|value| value.id().to_string())
        .collect()
}

fn check_subject(
    entity_kind: &'static str,
    entity_id: &str,
    subject: &StateSubject,
    assets: &BTreeSet<String>,
    services: &BTreeSet<String>,
    identities: &BTreeSet<String>,
) -> Result<(), StateError> {
    match subject {
        StateSubject::Engagement => Ok(()),
        StateSubject::Asset { asset_id } => {
            check_reference(entity_kind, entity_id, "asset", asset_id, assets)
        }
        StateSubject::Service { service_id } => {
            check_reference(entity_kind, entity_id, "service", service_id, services)
        }
        StateSubject::Identity { identity_id } => {
            check_reference(entity_kind, entity_id, "identity", identity_id, identities)
        }
    }
}

fn check_references(
    entity_kind: &'static str,
    entity_id: &str,
    reference_kind: &'static str,
    reference_ids: &[String],
    known_ids: &BTreeSet<String>,
) -> Result<(), StateError> {
    if reference_ids.is_empty() {
        return Err(StateError::MissingChainReference {
            entity_kind,
            entity_id: entity_id.to_string(),
            reference_kind,
        });
    }
    for reference_id in reference_ids {
        check_reference(
            entity_kind,
            entity_id,
            reference_kind,
            reference_id,
            known_ids,
        )?;
    }
    Ok(())
}

fn check_reference(
    entity_kind: &'static str,
    entity_id: &str,
    reference_kind: &'static str,
    reference_id: &str,
    known_ids: &BTreeSet<String>,
) -> Result<(), StateError> {
    if !known_ids.contains(reference_id) {
        return Err(StateError::BrokenChainReference {
            entity_kind,
            entity_id: entity_id.to_string(),
            reference_kind,
            reference_id: reference_id.to_string(),
        });
    }
    Ok(())
}

#[cfg(test)]
#[path = "state_target_tests.rs"]
mod tests;
