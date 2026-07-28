use super::*;
use crate::exit_codes::CliExitCode;
use codex_riftx_ipc::DaemonInfo;
use pretty_assertions::assert_eq;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeSet;
use std::path::Path;

#[test]
fn cli_owned_json_outputs_match_the_versioned_schema_fixture() {
    let schema: Value = serde_json::from_str(include_str!("../fixtures/cli-json-v1.schema.json"))
        .expect("CLI JSON schema fixture");
    assert_eq!(
        schema["x-riftx-contract-version"],
        json!(CLI_JSON_CONTRACT_VERSION)
    );

    let cases = [
        (
            "configValidation",
            serde_json::to_value(ConfigValidation {
                ok: true,
                config: Path::new("riftx.toml"),
            })
            .expect("config validation JSON"),
        ),
        (
            "daemonInfo",
            serde_json::to_value(DaemonInfo {
                protocol_version: 13,
                daemon_version: "1.0.0".to_string(),
            })
            .expect("daemon info JSON"),
        ),
        (
            "doctorReport",
            serde_json::to_value(DoctorReport {
                ok: true,
                config: Path::new("riftx.toml"),
                daemon: &DaemonInfo {
                    protocol_version: 13,
                    daemon_version: "1.0.0".to_string(),
                },
            })
            .expect("doctor report JSON"),
        ),
        (
            "approvalDecision",
            serde_json::to_value(ApprovalDecisionOutput {
                approval_id: "approval-1",
                decision: "approve",
            })
            .expect("approval decision JSON"),
        ),
        (
            "operationSuccess",
            serde_json::to_value(OperationSuccess { ok: true }).expect("operation success JSON"),
        ),
        (
            "eventEnvelope",
            serde_json::to_value(EventEnvelope {
                event: Some("turnCompleted".to_string()),
                data: json!({"turnId": "turn-1"}),
                id: None,
            })
            .expect("event envelope JSON"),
        ),
        (
            "artifactExport",
            serde_json::to_value(ArtifactExportOutput {
                output: Path::new("result.json"),
            })
            .expect("artifact export JSON"),
        ),
    ];

    for (definition, output) in cases {
        let output_fields = output
            .as_object()
            .expect("CLI output object")
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        let definition = &schema["$defs"][definition];
        let property_fields = definition["properties"]
            .as_object()
            .expect("schema properties")
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        let required_fields = definition["required"]
            .as_array()
            .expect("required schema fields")
            .iter()
            .map(|field| field.as_str().expect("required field").to_string())
            .collect::<BTreeSet<_>>();
        assert_eq!(output_fields, property_fields);
        assert_eq!(output_fields, required_fields);
        assert_eq!(definition["additionalProperties"], json!(false));
    }
}

#[test]
fn schema_fixture_captures_stable_exit_codes_and_ipc_ownership() {
    let schema: Value = serde_json::from_str(include_str!("../fixtures/cli-json-v1.schema.json"))
        .expect("CLI JSON schema fixture");
    assert_eq!(
        schema["x-riftx-exit-codes"],
        json!({
            "success": 0,
            "internal": CliExitCode::Internal as u8,
            "argumentOrConfig": CliExitCode::Config as u8,
            "daemonOrProtocol": CliExitCode::Daemon as u8,
            "requestRejected": CliExitCode::Request as u8,
            "localIo": CliExitCode::LocalIo as u8,
        })
    );
    assert_eq!(
        schema["x-riftx-ipc-passthrough-contracts"]["llm profiles list --json"],
        json!("codex_riftx_ipc::LlmProfileList")
    );
    assert_eq!(
        schema["x-riftx-ipc-passthrough-contracts"]["engagements create|get|activate --json"],
        json!("codex_riftx_ipc::Engagement")
    );
}
