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
