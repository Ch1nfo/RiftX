use crate::state::StateError;
use sqlx::Connection;
use sqlx::SqliteConnection;
use sqlx::sqlite::SqliteConnectOptions;
use std::io::ErrorKind;
use std::path::Path;
use std::path::PathBuf;

const CURRENT_STATE_SCHEMA_VERSION: i64 = 1;
const PRE_V1_BACKUP_SUFFIX: &str = ".pre-1.0.bak";

pub(super) async fn prepare_state_database(
    path: &Path,
    entity_schema: &[&'static str],
) -> Result<(), StateError> {
    let existing_permissions = match tokio::fs::metadata(path).await {
        Ok(metadata) => Some(metadata.permissions()),
        Err(source) if source.kind() == ErrorKind::NotFound => None,
        Err(source) => {
            return Err(StateError::DatabaseFile {
                operation: "inspect",
                path: path.to_path_buf(),
                source,
            });
        }
    };
    let options = SqliteConnectOptions::new()
        .filename(path)
        .create_if_missing(true)
        .foreign_keys(true);
    let mut connection = SqliteConnection::connect_with(&options).await?;
    let found = schema_version(&mut connection).await?;
    if found > CURRENT_STATE_SCHEMA_VERSION {
        return Err(StateError::UnsupportedSchemaVersion {
            found,
            supported: CURRENT_STATE_SCHEMA_VERSION,
        });
    }
    if found == CURRENT_STATE_SCHEMA_VERSION {
        return Ok(());
    }

    if let Some(permissions) = existing_permissions {
        backup_database_once(&mut connection, path, permissions).await?;
    }

    let mut transaction = connection.begin().await?;
    create_v1_schema(&mut transaction, entity_schema).await?;
    sqlx::query("PRAGMA user_version = 1")
        .execute(&mut *transaction)
        .await?;
    transaction.commit().await?;
    Ok(())
}

async fn schema_version(connection: &mut SqliteConnection) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar("PRAGMA user_version")
        .fetch_one(connection)
        .await
}

async fn backup_database_once(
    connection: &mut SqliteConnection,
    path: &Path,
    permissions: std::fs::Permissions,
) -> Result<(), StateError> {
    let backup_path = appended_path(path, PRE_V1_BACKUP_SUFFIX);
    match tokio::fs::metadata(&backup_path).await {
        Ok(metadata) if metadata.is_file() => {
            validate_backup(&backup_path).await?;
            return Ok(());
        }
        Ok(_) => {
            return Err(StateError::DatabaseFile {
                operation: "use non-file backup path for",
                path: backup_path,
                source: std::io::Error::new(
                    ErrorKind::AlreadyExists,
                    "backup path is not a regular file",
                ),
            });
        }
        Err(source) if source.kind() == ErrorKind::NotFound => {}
        Err(source) => {
            return Err(StateError::DatabaseFile {
                operation: "inspect backup for",
                path: backup_path,
                source,
            });
        }
    }
    let backup_name = backup_path
        .to_str()
        .ok_or_else(|| StateError::NonUtf8DatabasePath(backup_path.clone()))?;
    sqlx::query("VACUUM main INTO ?")
        .bind(backup_name)
        .execute(connection)
        .await
        .map_err(|source| StateError::BackupDatabase {
            path: backup_path.clone(),
            source,
        })?;
    if let Err(source) = tokio::fs::set_permissions(&backup_path, permissions).await {
        let _ = tokio::fs::remove_file(&backup_path).await;
        return Err(StateError::DatabaseFile {
            operation: "set permissions on backup",
            path: backup_path,
            source,
        });
    }
    if let Err(error) = validate_backup(&backup_path).await {
        let _ = tokio::fs::remove_file(&backup_path).await;
        return Err(error);
    }
    Ok(())
}

async fn validate_backup(path: &Path) -> Result<(), StateError> {
    let options = SqliteConnectOptions::new()
        .filename(path)
        .read_only(true)
        .foreign_keys(true);
    let mut connection = SqliteConnection::connect_with(&options)
        .await
        .map_err(|source| StateError::BackupDatabase {
            path: path.to_path_buf(),
            source,
        })?;
    let quick_check: String = sqlx::query_scalar("PRAGMA quick_check")
        .fetch_one(&mut connection)
        .await
        .map_err(|source| StateError::BackupDatabase {
            path: path.to_path_buf(),
            source,
        })?;
    let version =
        schema_version(&mut connection)
            .await
            .map_err(|source| StateError::BackupDatabase {
                path: path.to_path_buf(),
                source,
            })?;
    if quick_check != "ok" || version != 0 {
        return Err(StateError::DatabaseFile {
            operation: "validate pre-1.0 backup for",
            path: path.to_path_buf(),
            source: std::io::Error::new(
                ErrorKind::InvalidData,
                format!("quick_check={quick_check:?}, schema_version={version}"),
            ),
        });
    }
    Ok(())
}

async fn create_v1_schema(
    connection: &mut SqliteConnection,
    entity_schema: &[&'static str],
) -> Result<(), sqlx::Error> {
    sqlx::query("CREATE TABLE IF NOT EXISTS engagements (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
        .execute(&mut *connection)
        .await?;
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS conversation_entries (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            engagement_id TEXT NOT NULL,
            data BLOB NOT NULL,
            UNIQUE(engagement_id, id),
            FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE
        )",
    )
    .execute(&mut *connection)
    .await?;
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )",
    )
    .execute(&mut *connection)
    .await?;
    sqlx::query(
        "CREATE INDEX IF NOT EXISTS conversation_entries_engagement_sequence
         ON conversation_entries(engagement_id, sequence)",
    )
    .execute(&mut *connection)
    .await?;
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS credential_grant_uses (
            id TEXT PRIMARY KEY,
            engagement_id TEXT NOT NULL,
            grant_id TEXT NOT NULL,
            credential_id TEXT NOT NULL,
            identity_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            completed_at INTEGER,
            data BLOB NOT NULL,
            FOREIGN KEY (engagement_id) REFERENCES engagements(id) ON DELETE CASCADE
        )",
    )
    .execute(&mut *connection)
    .await?;
    sqlx::query(
        "CREATE INDEX IF NOT EXISTS credential_grant_uses_limits
         ON credential_grant_uses(grant_id, identity_hash, status)",
    )
    .execute(&mut *connection)
    .await?;
    for statement in entity_schema {
        sqlx::query(*statement).execute(&mut *connection).await?;
    }
    Ok(())
}

fn appended_path(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_owned();
    value.push(suffix);
    PathBuf::from(value)
}

#[cfg(test)]
#[path = "state_schema_tests.rs"]
mod tests;
