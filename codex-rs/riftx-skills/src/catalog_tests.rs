use super::*;
use codex_app_server_protocol::SkillMetadata;
use codex_app_server_protocol::SkillScope;
use codex_app_server_protocol::SkillsListEntry;
use codex_utils_absolute_path::AbsolutePathBuf;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

fn list_entry(root: &std::path::Path) -> SkillsListEntry {
    let skill_path = root.join("lab-recon").join("SKILL.md");
    SkillsListEntry {
        cwd: root.to_path_buf(),
        skills: vec![SkillMetadata {
            name: "lab-recon".to_string(),
            description: "Authorized lab reconnaissance".to_string(),
            short_description: None,
            interface: None,
            dependencies: None,
            path: AbsolutePathBuf::from_absolute_path(skill_path).expect("absolute path"),
            scope: SkillScope::User,
            enabled: true,
        }],
        errors: Vec::new(),
    }
}

#[tokio::test]
async fn catalog_hash_covers_skill_support_files() {
    let temp = TempDir::new().expect("tempdir");
    let skill_dir = temp.path().join("lab-recon");
    std::fs::create_dir_all(skill_dir.join("scripts")).expect("create skill");
    std::fs::write(
        skill_dir.join("SKILL.md"),
        "---\nname: lab-recon\ndescription: Authorized lab reconnaissance\n---\n",
    )
    .expect("write skill");
    std::fs::write(skill_dir.join("scripts/run.sh"), "echo one\n").expect("write script");
    let builder = SkillCatalogBuilder::new(temp.path().to_path_buf());
    let first = builder.build(list_entry(temp.path())).await;

    std::fs::write(skill_dir.join("scripts/run.sh"), "echo two\n").expect("update script");
    let second = builder.build(list_entry(temp.path())).await;

    assert_eq!(first.skills.len(), 1);
    assert_ne!(first.snapshot_sha256, second.snapshot_sha256);
    assert!(first.is_healthy());
}

#[cfg(unix)]
#[tokio::test]
async fn catalog_rejects_symlinks_inside_skills() {
    use std::os::unix::fs::symlink;

    let temp = TempDir::new().expect("tempdir");
    let skill_dir = temp.path().join("lab-recon");
    std::fs::create_dir_all(&skill_dir).expect("create skill");
    std::fs::write(skill_dir.join("SKILL.md"), "# skill\n").expect("write skill");
    symlink(skill_dir.join("SKILL.md"), skill_dir.join("alias.md")).expect("create symlink");

    let catalog = SkillCatalogBuilder::new(temp.path().to_path_buf())
        .build(list_entry(temp.path()))
        .await;

    assert!(catalog.skills.is_empty());
    assert_eq!(catalog.diagnostics[0].code, "skillSymlinkRejected");
    assert!(!catalog.is_healthy());
}
