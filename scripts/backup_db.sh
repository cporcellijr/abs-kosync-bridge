#!/bin/bash
# scripts/backup_db.sh

# Default to /data if DATA_DIR is not set
DATA_DIR="${DATA_DIR:-/data}"
BACKUP_DIR="${DATA_DIR}/backups"
DB_FILE="database.db"

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Timestamp for the backup
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/abs_kosync_${TIMESTAMP}.db"

# Check if database exists
if [ -f "${DATA_DIR}/${DB_FILE}" ]; then
    cp "${DATA_DIR}/${DB_FILE}" "$BACKUP_FILE"
    echo "✅ Backup created: $BACKUP_FILE"
else
    echo "⚠️ Database file not found at ${DATA_DIR}/${DB_FILE}"
    exit 1
fi
