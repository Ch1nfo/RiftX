use super::*;
use pretty_assertions::assert_eq;
use std::path::Path;
use tempfile::TempDir;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[cfg(unix)]
#[tokio::test]
async fn scanner_obeys_depth_order_metadata_and_shadowing() {
    let temp = TempDir::new().expect("tempdir");
    let root = temp.path().join("tools");
    let suite = root.join("suite");
    let bin = suite.join("bin");
    tokio::fs::create_dir_all(&bin).await.expect("directories");
    write_executable(&root.join("probe"), b"#!/bin/sh\nexit 0\n").await;
    write_executable(&suite.join("probe"), b"#!/bin/sh\nexit 1\n").await;
    write_executable(&bin.join("deep-probe"), b"#!/bin/sh\nexit 0\n").await;
    let ignored = bin.join("nested").join("ignored");
    tokio::fs::create_dir_all(ignored.parent().expect("parent"))
        .await
        .expect("nested");
    write_executable(&ignored, b"#!/bin/sh\nexit 0\n").await;
    tokio::fs::write(
        root.join("probe.riftx.toml"),
        concat!(
            "capabilities = [\"network.discovery\"]\n",
            "risk = \"low\"\n",
            "help_args = [\"--help\"]\n",
            "version_args = [\"--version\"]\n",
            "health_check_args = [\"doctor\"]\n",
            "input_target_field = \"target\"\n",
            "output_format = \"json\"\n",
            "parser = \"json\"\n",
        ),
    )
    .await
    .expect("metadata");

    let inventory = ToolScanner::new(ToolScanConfig {
        directories: vec![root.clone()],
        extra_paths: Vec::new(),
    })
    .scan()
    .await;

    assert_eq!(
        inventory.path_entries,
        vec![root.clone(), suite.clone(), bin.clone()]
    );
    assert_eq!(
        inventory
            .tools
            .iter()
            .map(|tool| tool.name.as_str())
            .collect::<Vec<_>>(),
        vec!["probe", "probe", "deep-probe"]
    );
    assert_eq!(
        inventory.tools[0].metadata.as_ref().expect("metadata"),
        &ToolMetadata {
            capabilities: vec!["network.discovery".to_string()],
            risk: Some(ToolRisk::Low),
            help_args: vec!["--help".to_string()],
            version_args: vec!["--version".to_string()],
            health_check_args: vec!["doctor".to_string()],
            input_target_field: Some("target".to_string()),
            output_format: Some("json".to_string()),
            parser: Some("json".to_string()),
        }
    );
    assert_eq!(inventory.tools[1].shadowed_by, Some(root.join("probe")));
    assert!(!inventory.tools.iter().any(|tool| tool.path == ignored));
    assert!(inventory.is_healthy());
    assert_eq!(inventory.snapshot_sha256.len(), 64);
}

#[cfg(unix)]
#[tokio::test]
async fn scanner_rejects_non_executable_files_and_symlinks() {
    let temp = TempDir::new().expect("tempdir");
    let root = temp.path().join("tools");
    tokio::fs::create_dir_all(&root).await.expect("root");
    tokio::fs::write(root.join("not-executable"), "#!/bin/sh\n")
        .await
        .expect("file");
    write_executable(&root.join("real"), b"#!/bin/sh\n").await;
    std::os::unix::fs::symlink(root.join("real"), root.join("linked")).expect("symlink");

    let inventory = ToolScanner::new(ToolScanConfig {
        directories: vec![root],
        extra_paths: Vec::new(),
    })
    .scan()
    .await;

    assert_eq!(
        inventory
            .tools
            .iter()
            .map(|tool| tool.name.as_str())
            .collect::<Vec<_>>(),
        vec!["real"]
    );
    assert!(
        inventory
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.code == "symlinkSkipped")
    );
}

#[tokio::test]
async fn missing_directory_is_a_healthy_empty_inventory() {
    let temp = TempDir::new().expect("tempdir");
    let root = temp.path().join("missing");
    let inventory = ToolScanner::new(ToolScanConfig {
        directories: vec![root.clone()],
        extra_paths: Vec::new(),
    })
    .scan()
    .await;
    assert_eq!(inventory.roots, vec![root]);
    assert!(inventory.path_entries.is_empty());
    assert!(inventory.tools.is_empty());
    assert!(inventory.is_healthy());
    assert_eq!(inventory.diagnostics[0].code, "directoryMissing");
}

#[cfg(unix)]
async fn write_executable(path: &Path, content: &[u8]) {
    tokio::fs::write(path, content).await.expect("tool");
    tokio::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
        .await
        .expect("permissions");
}
