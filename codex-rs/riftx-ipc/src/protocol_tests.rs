use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

#[test]
fn extension_inventory_round_trips_as_complete_typed_values() {
    let tool_json = json!({
        "roots": ["/opt/riftx/tools"],
        "pathEntries": ["/opt/riftx/tools/bin"],
        "tools": [{
            "name": "scanner",
            "path": "/opt/riftx/tools/bin/scanner",
            "sha256": "tool-sha",
            "metadataPath": "/opt/riftx/tools/bin/scanner.riftx.toml",
            "metadataSha256": "metadata-sha",
            "metadata": {
                "capabilities": ["network.discovery", "credential.testing"],
                "risk": "medium",
                "helpArgs": ["--help"],
                "versionArgs": ["--version"],
                "healthCheckArgs": [],
                "inputTargetField": "target",
                "outputFormat": "json",
                "parser": "json",
                "credential": {
                    "capability": "credential.testing",
                    "injection": "fileEnvironment",
                    "environmentVariable": "RIFTX_CREDENTIAL_FILE",
                    "arguments": ["--target", "{target}"],
                    "authenticationFailureExitCodes": [10]
                }
            },
            "shadowedBy": null
        }],
        "snapshotSha256": "tools-snapshot",
        "diagnostics": [{
            "level": "warning",
            "code": "tool_shadowed",
            "path": "/opt/riftx/tools/bin/scanner",
            "message": "another tool shadows this entry"
        }]
    });
    let skill_json = json!({
        "root": "/opt/riftx/skills",
        "skills": [{
            "name": "web-recon",
            "description": "Enumerate an authorized web target.",
            "path": "/opt/riftx/skills/web-recon/SKILL.md",
            "source": "user",
            "enabled": true,
            "sha256": "skill-sha"
        }],
        "snapshotSha256": "skills-snapshot",
        "diagnostics": []
    });

    let tools: ToolInventory =
        serde_json::from_value(tool_json.clone()).expect("tool inventory should decode");
    let skills: SkillCatalog =
        serde_json::from_value(skill_json.clone()).expect("skill catalog should decode");

    assert!(tools.is_healthy());
    assert!(skills.is_healthy());
    assert_eq!(
        serde_json::to_value((&tools, &skills)).expect("inventories should encode"),
        json!([tool_json, skill_json])
    );
}

#[test]
fn credential_execution_round_trips_without_untyped_payloads() {
    let params_json = json!({
        "grantId": "grant-1",
        "tool": "scanner",
        "target": {"host": "host.lab.example", "port": 445}
    });
    let response_json = json!({
        "usage": {
            "id": "use-1",
            "engagementId": "engagement-1",
            "grantId": "grant-1",
            "credentialId": "credential-1",
            "identityHash": "identity-sha256",
            "target": {"host": "host.lab.example", "port": 445},
            "capability": "credential.testing",
            "policyRevision": "policy-sha256",
            "status": "succeeded",
            "startedAt": 100,
            "completedAt": 110
        },
        "execution": {
            "id": "execution-1",
            "engagementId": "engagement-1",
            "testCaseId": null,
            "taskId": null,
            "turnId": "credential:use-1",
            "runner": "native-credential-tool",
            "status": "completed",
            "startedAt": 100,
            "completedAt": 110,
            "exitCode": 0,
            "durationMs": 10000,
            "argv": ["scanner", "--target", "host.lab.example"],
            "commandSha256": "command-sha256",
            "cwd": "/workspace",
            "processId": null,
            "tool": null,
            "toolInventorySha256": "inventory-sha256",
            "stdoutSha256": "stdout-sha256",
            "stderrSha256": "stderr-sha256",
            "stdinSha256": null,
            "stdoutBytes": 10,
            "stderrBytes": 0,
            "stdinBytes": 0
        },
        "stdout": "success",
        "stderr": ""
    });

    let params: CredentialExecutionParams =
        serde_json::from_value(params_json.clone()).expect("params should decode");
    let response: CredentialExecutionResponse =
        serde_json::from_value(response_json.clone()).expect("response should decode");

    assert_eq!(
        serde_json::to_value((params, response)).expect("credential execution should encode"),
        json!([params_json, response_json])
    );
}

#[test]
fn llm_profile_state_has_closed_wire_values() {
    use crate::protocol::LlmProfileState;
    let states = [
        LlmProfileState::Unconfigured,
        LlmProfileState::Ready,
        LlmProfileState::Invalid,
        LlmProfileState::Unreachable,
        LlmProfileState::Disabled,
        LlmProfileState::InUse,
    ];
    assert_eq!(
        serde_json::to_value(states).expect("serialize"),
        serde_json::json!([
            "unconfigured",
            "ready",
            "invalid",
            "unreachable",
            "disabled",
            "in_use"
        ])
    );
}
