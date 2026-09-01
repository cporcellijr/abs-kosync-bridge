#!/bin/bash
# scripts/backup_db.sh
#
# Create an online-consistent SQLite snapshot plus the active default
# credential key. This helper is intentionally narrower than a full DATA_DIR
# backup; see docs/backup-restore.md for disaster recovery.

set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR}/backups}"
DB_PATH="${DATA_DIR}/database.db"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -f "$DB_PATH" ]; then
    echo "⚠️ Database file not found at $DB_PATH"
    exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "❌ Python interpreter not found: $PYTHON_BIN"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
# New snapshots carry the BookBridge name.  Snapshots an existing install already
# took are named abs_kosync_<timestamp>.db; those are still perfectly good backups
# and restore by exactly the same steps, so they are never renamed or cleaned up
# here.  A backup directory may legitimately hold both prefixes.
BACKUP_FILE="${BACKUP_DIR}/bookbridge_${TIMESTAMP}.db"
KEY_BACKUP_FILE="${BACKUP_DIR}/bookbridge_${TIMESTAMP}.secret.key"
TMP_BACKUP_FILE="${BACKUP_FILE}.tmp.$$"
TMP_KEY_BACKUP_FILE="${KEY_BACKUP_FILE}.tmp.$$"

if [ -e "$BACKUP_FILE" ] || [ -e "$KEY_BACKUP_FILE" ]; then
    echo "❌ Backup target already exists for timestamp $TIMESTAMP"
    exit 1
fi

cleanup() {
    rm -f "$TMP_BACKUP_FILE" "$TMP_KEY_BACKUP_FILE"
}
trap cleanup EXIT

"$PYTHON_BIN" - "$DB_PATH" "$TMP_BACKUP_FILE" <<'PY'
from pathlib import Path
import sqlite3
import sys

source_path, backup_path = sys.argv[1:3]
source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"

with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
    with sqlite3.connect(backup_path) as backup:
        source.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite integrity_check failed: {result!r}")
PY

# The snapshot carries every stored credential in encrypted form, so it gets the
# same restrictive mode as the key it sits beside rather than the default umask.
chmod 600 "$TMP_BACKUP_FILE" 2>/dev/null || true

key_message=""
if [ -n "${BOOKBRIDGE_SECRET_KEY:-}" ]; then
    key_message="Credential key is provided by BOOKBRIDGE_SECRET_KEY and is not included."
elif [ -n "${BOOKBRIDGE_SECRET_KEY_FILE:-}" ]; then
    key_message="Credential key is provided by BOOKBRIDGE_SECRET_KEY_FILE and is not included."
elif [ -f "${DATA_DIR}/secret.key" ]; then
    cp "${DATA_DIR}/secret.key" "$TMP_KEY_BACKUP_FILE"
    chmod 600 "$TMP_KEY_BACKUP_FILE" 2>/dev/null || true
fi

mv "$TMP_BACKUP_FILE" "$BACKUP_FILE"
if [ -f "$TMP_KEY_BACKUP_FILE" ]; then
    mv "$TMP_KEY_BACKUP_FILE" "$KEY_BACKUP_FILE"
fi

trap - EXIT

echo "✅ Database backup created: $BACKUP_FILE"
if [ -f "$KEY_BACKUP_FILE" ]; then
    echo "🔐 Credential key backup created: $KEY_BACKUP_FILE"
elif [ -n "$key_message" ]; then
    echo "ℹ️ $key_message"
fi
echo "ℹ️ Database snapshot only. Back up the full DATA_DIR to preserve transcript and cache state."
