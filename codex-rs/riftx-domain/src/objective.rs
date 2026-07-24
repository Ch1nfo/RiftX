use crate::FindingSeverity;
use serde::Deserialize;
use serde::Serialize;
use thiserror::Error;

const MAX_CONFIDENCE_BASIS_POINTS: u16 = 10_000;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StructuredSuccessCriterion {
    pub id: String,
    pub description: String,
    pub predicate: SuccessPredicate,
}

impl StructuredSuccessCriterion {
    pub fn validate(&self) -> Result<(), SuccessCriterionError> {
        if self.id.trim().is_empty() || self.description.trim().is_empty() {
            return Err(SuccessCriterionError::MissingIdentity);
        }
        let basis_points = match &self.predicate {
            SuccessPredicate::Evidence { .. } => None,
            SuccessPredicate::Coverage {
                minimum_basis_points,
            } => Some(*minimum_basis_points),
            SuccessPredicate::Finding {
                minimum_confidence_basis_points,
                ..
            }
            | SuccessPredicate::AttackPath {
                minimum_confidence_basis_points,
                ..
            } => Some(*minimum_confidence_basis_points),
        };
        if basis_points.is_some_and(|value| value > MAX_CONFIDENCE_BASIS_POINTS) {
            return Err(SuccessCriterionError::InvalidBasisPoints);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(
    tag = "type",
    rename_all = "camelCase",
    rename_all_fields = "camelCase",
    deny_unknown_fields
)]
pub enum SuccessPredicate {
    Evidence {
        minimum_items: u32,
        reproduction_required: bool,
    },
    Coverage {
        minimum_basis_points: u16,
    },
    Finding {
        minimum_count: u32,
        minimum_severity: FindingSeverity,
        minimum_confidence_basis_points: u16,
    },
    AttackPath {
        destination_role: String,
        access_level: String,
        minimum_confidence_basis_points: u16,
        reproducible_evidence: bool,
    },
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SuccessCriterionError {
    #[error("success criteria require non-empty ids and descriptions")]
    MissingIdentity,
    #[error("confidence and coverage values must be between 0 and 10000 basis points")]
    InvalidBasisPoints,
}

#[cfg(test)]
#[path = "objective_tests.rs"]
mod tests;
