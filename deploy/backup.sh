#!/bin/bash
# Daily backup of /data/memory → /data/memory-backups/YYYY-MM-DD.tar.gz
# Keeps 14 days of daily snapshots.
set -euo pipefail

SRC=/data/memory
DEST=/data/memory-backups
DATE=$(date +%Y-%m-%d)
KEEP_DAYS=14

mkdir -p "$DEST"

# Atomic tarball creation
tar -czf "$DEST/.tmp.$DATE.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")"
mv "$DEST/.tmp.$DATE.tar.gz" "$DEST/$DATE.tar.gz"

# Prune old backups
find "$DEST" -maxdepth 1 -name '*.tar.gz' -mtime +$KEEP_DAYS -delete

# Report
echo "[$(date -Iseconds)] backup: $DEST/$DATE.tar.gz ($(du -h "$DEST/$DATE.tar.gz" | cut -f1))"
