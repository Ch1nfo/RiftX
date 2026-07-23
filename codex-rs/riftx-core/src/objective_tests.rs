use super::*;
use crate::AssessmentObjective;
use pretty_assertions::assert_eq;

#[test]
fn attack_path_criterion_round_trips_and_validates() {
    let expected = StructuredSuccessCriterion {
        id: "reach-domain-controller".to_string(),
        description: "Validate a reproducible path to domain control".to_string(),
        predicate: SuccessPredicate::AttackPath {
            destination_role: "domainController".to_string(),
            access_level: "domainAdminEquivalent".to_string(),
            minimum_confidence_basis_points: 9_000,
            reproducible_evidence: true,
        },
    };
    expected.validate().expect("valid criterion");
    let encoded = serde_json::to_string(&expected).expect("serialize criterion");
    let decoded: StructuredSuccessCriterion =
        serde_json::from_str(&encoded).expect("deserialize criterion");
    assert_eq!(decoded, expected);
}

#[test]
fn criterion_rejects_out_of_range_confidence() {
    let criterion = StructuredSuccessCriterion {
        id: "invalid-confidence".to_string(),
        description: "Reject invalid confidence".to_string(),
        predicate: SuccessPredicate::Finding {
            minimum_count: 1,
            minimum_severity: FindingSeverity::High,
            minimum_confidence_basis_points: 10_001,
        },
    };
    assert_eq!(
        criterion.validate(),
        Err(SuccessCriterionError::InvalidBasisPoints)
    );
}

#[test]
fn legacy_objective_defaults_structured_criteria() {
    let decoded: AssessmentObjective = serde_json::from_value(serde_json::json!({
        "summary": "Assess the authorized scope",
        "successCriteria": ["Document validated findings"]
    }))
    .expect("legacy objective should decode");

    assert_eq!(
        decoded,
        AssessmentObjective {
            summary: "Assess the authorized scope".to_string(),
            success_criteria: vec!["Document validated findings".to_string()],
            structured_criteria: Vec::new(),
        }
    );
}
