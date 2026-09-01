# Backup and Restore

BookBridge keeps its own persistent state under `DATA_DIR` (`/data` in Docker). With the example Compose file, that is the host's `./data` directory.

## What to back up

For disaster recovery, back up the whole `DATA_DIR`, not only `database.db`.

- `database.db` stores mappings, sync state, settings, users, statistics, and computed book alignments.
- `secret.key` is the default key used to decrypt stored credentials.
- `audio_cache/` keeps completed local Whisper transcript data (`_progress.json`) after a successful transcription so a later re-alignment can reuse it instead of running Whisper again.
- The remaining files under `/data` are caches or application state that may save re-download or rebuild work after a restore.

The `/books` mount is library input, not BookBridge state, and should follow your normal library backup policy. The same applies to optional Storyteller or Calibre mounts and to your Compose/environment configuration.

> [!IMPORTANT]
> A backup that contains both `database.db` and `secret.key` can decrypt BookBridge's stored credentials. Protect it like the server itself.

If you use `BOOKBRIDGE_SECRET_KEY` or `BOOKBRIDGE_SECRET_KEY_FILE`, treat that key as separately managed and preserve the exact value or file outside the normal `/data` backup.

## Recommended: full `/data` backup

The safest simple backup is a filesystem copy while BookBridge is stopped. This keeps the SQLite database, credential key, completed transcript cache, and the rest of the data directory at one point in time.

For the bind mount used by the example Compose file:

```bash
docker compose stop
tar -czf "bookbridge-data-$(date +%Y%m%d_%H%M%S).tar.gz" --exclude=./backups -C ./data .
docker compose start
```

`./backups` is excluded because the online snapshots below are written inside `DATA_DIR`; without the exclusion each archive would also carry every previous snapshot. If you use a named volume or a different host path, snapshot or archive that volume instead. Store the resulting archive somewhere outside the BookBridge data disk; a second copy inside `/data` does not protect against disk loss.

## Online database snapshot

For a quick database snapshot without stopping BookBridge, the image includes a small helper. `exec` needs the service name from your own `docker-compose.yml` — `abs-kosync` in the example Compose file, but yours may differ:

```bash
docker compose exec abs-kosync /app/scripts/backup_db.sh
```

The helper uses SQLite's online backup API instead of copying the live database file directly, so committed data in either WAL or rollback-journal mode is included consistently. It also runs `PRAGMA integrity_check` on the snapshot before publishing it.

By default it writes:

```text
/data/backups/bookbridge_YYYYMMDD_HHMMSS.db
/data/backups/bookbridge_YYYYMMDD_HHMMSS.secret.key
```

Snapshots taken before the BookBridge rename are called `abs_kosync_<timestamp>.db`. They are still valid backups and restore by exactly the same steps below — nothing renames or removes them, so a backup directory may hold both prefixes.

The key file is created only when BookBridge is using the default `DATA_DIR/secret.key`. Explicit `BOOKBRIDGE_SECRET_KEY` and `BOOKBRIDGE_SECRET_KEY_FILE` overrides are deliberately not bundled.

> [!NOTE]
> This helper is a database snapshot, not a full disaster-recovery backup. It does not include `audio_cache/`, cached EPUBs, external library mounts, or your Compose/environment configuration. Copy snapshots off the BookBridge data disk if you want them to survive host or disk failure.

The database contains the computed alignment maps, so an ordinary database restore keeps existing book alignments. A full `/data` backup also preserves the completed Whisper transcript cache, which is what lets BookBridge rebuild an alignment later without transcribing the audiobook again.

## Restore

Stop BookBridge before replacing its data. For the example bind mount, restore a full archive into an empty `./data` directory; keeping the old directory alongside it gives you an easy rollback if the restore is wrong.

```bash
docker compose stop
mv ./data ./data.before-restore
mkdir ./data
tar -xzf bookbridge-data-YYYYMMDD_HHMMSS.tar.gz -C ./data
docker compose start
```

If you use an external credential key, restore the same `BOOKBRIDGE_SECRET_KEY` value or `BOOKBRIDGE_SECRET_KEY_FILE` before starting BookBridge.

For a database-only snapshot, stop BookBridge, remove stale SQLite sidecars, then restore the database and matching default key:

```bash
docker compose stop
rm -f ./data/database.db-wal ./data/database.db-shm ./data/database.db-journal
cp ./data/backups/bookbridge_YYYYMMDD_HHMMSS.db ./data/database.db
cp ./data/backups/bookbridge_YYYYMMDD_HHMMSS.secret.key ./data/secret.key
chmod 600 ./data/secret.key
docker compose start
```

Skip the `secret.key` copy when the snapshot did not contain one because you manage the key externally. Restore into the same BookBridge version when possible; newer versions can migrate an older database forward on startup, while deliberately restoring a newer database into an older BookBridge build is not supported.
