use super::*;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn capture_is_content_addressed_and_rejects_secret_paths() {
    let temp = TempDir::new().expect("temp dir");
    let workspace = temp.path().join("workspace");
    tokio::fs::create_dir_all(workspace.join("artifacts"))
        .await
        .expect("create workspace");
    tokio::fs::write(workspace.join("artifacts/evidence.txt"), b"evidence")
        .await
        .expect("write evidence");
    let store = ArtifactStore::new(&ArtifactConfig {
        root: temp.path().join("store"),
        max_bytes_per_engagement: 1024,
    });
    let artifact = store
        .capture(CaptureArtifact {
            engagement_id: "eng-1",
            workspace: &workspace,
            relative_path: Path::new("artifacts/evidence.txt"),
            media_type: None,
            execution_id: Some("execution-1"),
            existing: &[],
            created_at: 10,
        })
        .await
        .expect("capture artifact");

    assert_eq!(
        artifact,
        Artifact {
            id: artifact.id.clone(),
            engagement_id: "eng-1".to_string(),
            execution_id: Some("execution-1".to_string()),
            path: "artifacts/evidence.txt".to_string(),
            media_type: "text/plain".to_string(),
            sha256: hex_digest(Sha256::digest(b"evidence")),
            size_bytes: 8,
            created_at: 10,
        }
    );
    let mut content = Vec::new();
    store
        .open(&artifact)
        .await
        .expect("open artifact")
        .read_to_end(&mut content)
        .await
        .expect("read artifact");
    assert_eq!(content, b"evidence");
    assert_eq!(
        store.discover(&workspace).expect("discover artifacts"),
        vec![PathBuf::from("artifacts/evidence.txt")]
    );
    let stored = temp
        .path()
        .join("store")
        .join("eng-1")
        .join(&artifact.sha256);
    tokio::fs::remove_file(&stored)
        .await
        .expect("remove stored artifact");
    tokio::fs::write(&stored, b"tampered")
        .await
        .expect("replace stored artifact");
    assert!(matches!(
        store.open(&artifact).await,
        Err(ArtifactError::DigestMismatch)
    ));

    assert!(matches!(
        store
            .capture(CaptureArtifact {
                engagement_id: "eng-1",
                workspace: &workspace,
                relative_path: Path::new("../secret"),
                media_type: None,
                execution_id: None,
                existing: &[artifact],
                created_at: 11,
            })
            .await,
        Err(ArtifactError::InvalidRelativePath)
    ));
}

#[tokio::test]
async fn capture_enforces_unique_blob_capacity() {
    let temp = TempDir::new().expect("temp dir");
    let workspace = temp.path().join("workspace");
    tokio::fs::create_dir_all(&workspace)
        .await
        .expect("create workspace");
    tokio::fs::write(workspace.join("first.bin"), b"12345")
        .await
        .expect("write first");
    tokio::fs::write(workspace.join("duplicate.bin"), b"12345")
        .await
        .expect("write duplicate");
    tokio::fs::write(workspace.join("second.bin"), b"67890")
        .await
        .expect("write second");
    let store = ArtifactStore::new(&ArtifactConfig {
        root: temp.path().join("store"),
        max_bytes_per_engagement: 8,
    });
    let first = store
        .capture(CaptureArtifact {
            engagement_id: "eng-1",
            workspace: &workspace,
            relative_path: Path::new("first.bin"),
            media_type: None,
            execution_id: None,
            existing: &[],
            created_at: 1,
        })
        .await
        .expect("capture first");
    store
        .capture(CaptureArtifact {
            engagement_id: "eng-1",
            workspace: &workspace,
            relative_path: Path::new("duplicate.bin"),
            media_type: None,
            execution_id: None,
            existing: std::slice::from_ref(&first),
            created_at: 2,
        })
        .await
        .expect("duplicate content does not consume capacity");
    assert!(matches!(
        store
            .capture(CaptureArtifact {
                engagement_id: "eng-1",
                workspace: &workspace,
                relative_path: Path::new("second.bin"),
                media_type: None,
                execution_id: None,
                existing: &[first],
                created_at: 3,
            })
            .await,
        Err(ArtifactError::CapacityExceeded { limit: 8 })
    ));
}

#[cfg(unix)]
#[tokio::test]
async fn capture_rejects_symbolic_links() {
    use std::os::unix::fs::symlink;

    let temp = TempDir::new().expect("temp dir");
    let workspace = temp.path().join("workspace");
    tokio::fs::create_dir_all(&workspace)
        .await
        .expect("create workspace");
    tokio::fs::write(temp.path().join("outside"), b"secret")
        .await
        .expect("write outside file");
    symlink(temp.path().join("outside"), workspace.join("linked")).expect("create symlink");
    let store = ArtifactStore::new(&ArtifactConfig {
        root: temp.path().join("store"),
        max_bytes_per_engagement: 1024,
    });

    assert!(matches!(
        store
            .capture(CaptureArtifact {
                engagement_id: "eng-1",
                workspace: &workspace,
                relative_path: Path::new("linked"),
                media_type: None,
                execution_id: None,
                existing: &[],
                created_at: 1,
            })
            .await,
        Err(ArtifactError::SymbolicLink(_))
    ));
}
