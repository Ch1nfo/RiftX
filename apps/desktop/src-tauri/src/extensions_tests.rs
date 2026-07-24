use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;

#[test]
fn extension_inventory_wire_models_round_trip_as_complete_values() {
    let tools: ToolInventoryView = serde_json::from_value(json!({
        "roots": ["/opt/riftx/tools"],
        "pathEntries": ["/opt/riftx/tools/bin"],
        "tools": [{
            "name": "scanner",
            "path": "/opt/riftx/tools/bin/scanner",
            "sha256": "tool-sha",
            "metadataPath": "/opt/riftx/tools/bin/scanner.riftx.toml",
            "metadataSha256": "metadata-sha",
            "metadata": {
                "capabilities": ["network.discovery"],
                "risk": "medium",
                "helpArgs": ["--help"],
                "versionArgs": ["--version"],
                "healthCheckArgs": [],
                "inputTargetField": "target",
                "outputFormat": "json",
                "parser": "json"
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
    }))
    .expect("tool inventory");
    let skills: SkillCatalogView = serde_json::from_value(json!({
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
    }))
    .expect("skill catalog");

    assert_eq!(
        serde_json::to_value((tools, skills)).expect("extension inventory JSON"),
        json!([{
            "roots": ["/opt/riftx/tools"],
            "pathEntries": ["/opt/riftx/tools/bin"],
            "tools": [{
                "name": "scanner",
                "path": "/opt/riftx/tools/bin/scanner",
                "sha256": "tool-sha",
                "metadataPath": "/opt/riftx/tools/bin/scanner.riftx.toml",
                "metadataSha256": "metadata-sha",
                "metadata": {
                    "capabilities": ["network.discovery"],
                    "risk": "medium",
                    "helpArgs": ["--help"],
                    "versionArgs": ["--version"],
                    "healthCheckArgs": [],
                    "inputTargetField": "target",
                    "outputFormat": "json",
                    "parser": "json"
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
        }, {
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
        }])
    );
}
