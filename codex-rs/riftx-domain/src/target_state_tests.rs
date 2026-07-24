use super::*;
use pretty_assertions::assert_eq;

#[test]
fn attack_path_round_trips_with_evidence_per_hop() {
    let path = AttackPath {
        id: "path-1".to_string(),
        engagement_id: "eng-1".to_string(),
        hops: vec![AttackPathHop {
            source: StateSubject::Identity {
                identity_id: "identity-1".to_string(),
            },
            destination: StateSubject::Asset {
                asset_id: "dc-1".to_string(),
            },
            capability: "credentialReuse".to_string(),
            evidence_ids: vec!["evidence-1".to_string()],
        }],
        destination_role: "domainController".to_string(),
        access_level: "domainAdminEquivalent".to_string(),
        confidence_basis_points: 9_000,
        reproducible: true,
        validated_at: 100,
    };

    path.validate().expect("attack path should be valid");
    let encoded = serde_json::to_vec(&path).expect("attack path should encode");
    let decoded: AttackPath = serde_json::from_slice(&encoded).expect("attack path should decode");

    assert_eq!(decoded, path);
}

#[test]
fn execution_requires_completion_time_only_when_terminal() {
    let mut execution = execution();
    execution.status = ExecutionStatus::Completed;
    assert_eq!(
        execution.validate(),
        Err(TargetStateError::InvalidExecutionWindow)
    );

    execution.completed_at = Some(110);
    execution.validate().expect("completed execution is valid");

    execution.status = ExecutionStatus::Running;
    assert_eq!(
        execution.validate(),
        Err(TargetStateError::InvalidExecutionWindow)
    );
}

#[test]
fn coverage_rejects_impossible_counts() {
    let coverage = Coverage {
        id: "coverage-1".to_string(),
        engagement_id: "eng-1".to_string(),
        dimension: "authorizedAssets".to_string(),
        covered_items: 3,
        total_items: 2,
        measured_at: 100,
    };

    assert_eq!(coverage.validate(), Err(TargetStateError::InvalidCoverage));
}

fn execution() -> Execution {
    Execution {
        id: "execution-1".to_string(),
        engagement_id: "eng-1".to_string(),
        test_case_id: Some("test-1".to_string()),
        task_id: Some("task-1".to_string()),
        turn_id: "turn-1".to_string(),
        runner: "local:nmap".to_string(),
        status: ExecutionStatus::Running,
        started_at: 100,
        completed_at: None,
        exit_code: None,
        duration_ms: None,
        argv: vec!["nmap".to_string(), "127.0.0.1".to_string()],
        command_sha256: "command-sha256".to_string(),
        cwd: "/tmp".to_string(),
        process_id: None,
        tool: None,
        tool_inventory_sha256: "inventory-sha256".to_string(),
        stdout_sha256: None,
        stderr_sha256: None,
        stdin_sha256: None,
        stdout_bytes: 0,
        stderr_bytes: 0,
        stdin_bytes: 0,
    }
}
