use super::*;
use crate::state::StateStore;
use crate::state::open_test_store;
use pretty_assertions::assert_eq;
use sqlx::Connection;
use sqlx::Row;
use tempfile::TempDir;

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true),
    )
    .await
    .expect("connect sqlite")
}

async fn user_version(path: &Path) -> i64 {
    let mut connection = connect(path).await;
    schema_version(&mut connection)
        .await
        .expect("schema version")
}

async fn marker(path: &Path) -> String {
    let mut connection = connect(path).await;
    sqlx::query("SELECT value FROM legacy_marker")
        .fetch_one(&mut connection)
        .await
        .expect("legacy marker")
        .try_get("value")
        .expect("marker value")
}

#[tokio::test]
async fn fresh_database_gets_current_schema_without_backup() {
    let temp = TempDir::new().expect("tempdir");
    let path = temp.path().join("state.sqlite");

    let store = open_test_store(&path).await.expect("open state store");
    drop(store);

    assert_eq!(user_version(&path).await, CURRENT_STATE_SCHEMA_VERSION);
    assert!(!appended_path(&path, PRE_V1_BACKUP_SUFFIX).exists());
}

#[tokio::test]
async fn legacy_database_is_backed_up_before_transactional_migration() {
    let temp = TempDir::new().expect("tempdir");
    let path = temp.path().join("state.sqlite");
    let backup_path = appended_path(&path, PRE_V1_BACKUP_SUFFIX);
    let mut legacy = connect(&path).await;
    sqlx::query("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        .execute(&mut legacy)
        .await
        .expect("create marker table");
    sqlx::query("INSERT INTO legacy_marker(value) VALUES('pre-1.0')")
        .execute(&mut legacy)
        .await
        .expect("insert marker");
    legacy.close().await.expect("close legacy database");

    let store = open_test_store(&path).await.expect("migrate state store");
    drop(store);

    assert_eq!(user_version(&path).await, CURRENT_STATE_SCHEMA_VERSION);
    assert_eq!(user_version(&backup_path).await, 0);
    assert_eq!(marker(&path).await, "pre-1.0");
    assert_eq!(marker(&backup_path).await, "pre-1.0");

    let backup_before = tokio::fs::read(&backup_path).await.expect("read backup");
    let reopened = open_test_store(&path)
        .await
        .expect("reopen current state store");
    reopened
        .put_system_state("migration-test", &serde_json::json!({"value": 1}))
        .await
        .expect("write current database");
    drop(reopened);
    assert_eq!(
        tokio::fs::read(&backup_path)
            .await
            .expect("read backup again"),
        backup_before
    );
}

#[tokio::test]
async fn newer_database_schema_is_rejected_without_writes() {
    let temp = TempDir::new().expect("tempdir");
    let path = temp.path().join("state.sqlite");
    let mut newer = connect(&path).await;
    sqlx::query("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        .execute(&mut newer)
        .await
        .expect("create marker table");
    sqlx::query("INSERT INTO legacy_marker(value) VALUES('newer')")
        .execute(&mut newer)
        .await
        .expect("insert marker");
    sqlx::query("PRAGMA user_version = 2")
        .execute(&mut newer)
        .await
        .expect("set newer version");
    newer.close().await.expect("close newer database");

    let error = match StateStore::open_with_cipher(&path, crate::state::test_record_cipher()).await
    {
        Ok(_) => panic!("newer schema must fail closed"),
        Err(error) => error,
    };

    assert!(matches!(
        error,
        StateError::UnsupportedSchemaVersion {
            found: 2,
            supported: CURRENT_STATE_SCHEMA_VERSION,
        }
    ));
    assert_eq!(marker(&path).await, "newer");
    assert!(!appended_path(&path, PRE_V1_BACKUP_SUFFIX).exists());
}

#[tokio::test]
async fn failed_migration_keeps_legacy_database_and_backup_unchanged() {
    let temp = TempDir::new().expect("tempdir");
    let path = temp.path().join("state.sqlite");
    let backup_path = appended_path(&path, PRE_V1_BACKUP_SUFFIX);
    let mut legacy = connect(&path).await;
    sqlx::query("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        .execute(&mut legacy)
        .await
        .expect("create marker table");
    sqlx::query("INSERT INTO legacy_marker(value) VALUES('preserved')")
        .execute(&mut legacy)
        .await
        .expect("insert marker");
    sqlx::query("CREATE TABLE conversation_entries_engagement_sequence (value TEXT NOT NULL)")
        .execute(&mut legacy)
        .await
        .expect("create conflicting legacy object");
    legacy.close().await.expect("close legacy database");

    assert!(
        StateStore::open_with_cipher(&path, crate::state::test_record_cipher())
            .await
            .is_err(),
        "conflicting schema must fail migration"
    );

    assert_eq!(user_version(&path).await, 0);
    assert_eq!(user_version(&backup_path).await, 0);
    assert_eq!(marker(&path).await, "preserved");
    assert_eq!(marker(&backup_path).await, "preserved");
    let mut original = connect(&path).await;
    let object_type: String = sqlx::query_scalar(
        "SELECT type FROM sqlite_master WHERE name = 'conversation_entries_engagement_sequence'",
    )
    .fetch_one(&mut original)
    .await
    .expect("read legacy object type");
    assert_eq!(object_type, "table");
    let engagements_created: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name = 'engagements')")
            .fetch_one(&mut original)
            .await
            .expect("check migration rollback");
    assert!(!engagements_created);
}

#[tokio::test]
async fn invalid_existing_backup_blocks_legacy_migration() {
    let temp = TempDir::new().expect("tempdir");
    let path = temp.path().join("state.sqlite");
    let backup_path = appended_path(&path, PRE_V1_BACKUP_SUFFIX);
    let mut legacy = connect(&path).await;
    sqlx::query("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        .execute(&mut legacy)
        .await
        .expect("create marker table");
    sqlx::query("INSERT INTO legacy_marker(value) VALUES('not-migrated')")
        .execute(&mut legacy)
        .await
        .expect("insert marker");
    legacy.close().await.expect("close legacy database");
    tokio::fs::write(&backup_path, b"not a sqlite backup")
        .await
        .expect("seed invalid backup");

    assert!(
        StateStore::open_with_cipher(&path, crate::state::test_record_cipher())
            .await
            .is_err(),
        "invalid backup must block migration"
    );

    assert_eq!(user_version(&path).await, 0);
    assert_eq!(marker(&path).await, "not-migrated");
    assert_eq!(
        tokio::fs::read(&backup_path)
            .await
            .expect("read invalid backup"),
        b"not a sqlite backup"
    );
}
