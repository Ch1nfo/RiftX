# RiftX 0.8 to 1.0 upgrade and rollback

RiftX 1.0 performs forward-only local data migrations. Stop every `riftxd` process before manually copying, restoring, or moving its configuration and State DB.

## Automatic pre-1.0 backups

When a legacy configuration needs migration, RiftX writes the original bytes once beside it:

```text
riftx.toml.pre-1.0.bak
```

When an existing SQLite State DB has schema version `0`, `riftxd` creates a consistent SQLite backup before changing the schema:

```text
state.sqlite.pre-1.0.bak
```

The SQLite migration and schema-version update run in one transaction. If the backup cannot be created or the migration fails, `riftxd` does not open the normal State Store and refuses to serve requests. A State DB with a schema version newer than the binary supports is also rejected without migration or writes.

Backups are not rotated and are not overwritten by later 1.0 starts. Move them to operator-controlled encrypted storage after verifying the upgrade. They can contain target, audit, provider, path, and engagement metadata even though protected engagement payloads remain encrypted.

## Upgrade verification

After first launch:

1. Confirm `riftxd` starts without a migration error.
2. Confirm the adjacent configuration and State DB backups exist when upgrading real v0.8 data.
3. Run `riftx system status --json` and verify the daemon version is `1.0.0`.
4. Open an existing engagement and verify its conversation, approvals, report snapshot, and artifacts.
5. Create a harmless local task, restart the daemon, and verify the new state survives restart.

Do not delete the backups until those checks and a separate filesystem backup have passed.

## Downgrade boundary

RiftX does not support opening a State DB written by a newer schema with an older binary. There is no in-place 1.0-to-0.8 downgrade migration.

To return to 0.8 during an RC rollback:

1. stop Desktop and all daemon processes;
2. preserve the failed/current 1.0 files for investigation;
3. restore both `.pre-1.0.bak` files as the active configuration and State DB using filesystem-level copies;
4. start the 0.8 binary only after confirming no 1.0 process is running.

Any work created after the 1.0 migration is absent from the pre-1.0 backup. Export required reports and artifacts before restoring. Never merge SQLite files or copy only the database while retaining an incompatible configuration.
