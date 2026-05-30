#!/usr/bin/env bash
#
# Daily SQLite backup for AlgoPro. Uses sqlite3's online .backup so it is safe
# while the app is running (handles WAL correctly).
#
# Install as a cron job (as the algopro user or root):
#   sudo cp deploy/backup-db.sh /opt/algopro/algotrade-pro/deploy/backup-db.sh
#   ( crontab -l 2>/dev/null; echo "30 3 * * * /opt/algopro/algotrade-pro/deploy/backup-db.sh" ) | crontab -
#
# Keeps the last 14 daily snapshots.

set -euo pipefail

DB="/opt/algopro/algotrade-pro/algotrade.db"
DEST="/opt/algopro/backups"
KEEP=14

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/algotrade-$STAMP.db"

if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$OUT'"
else
    # Fallback: plain copy (acceptable for a single-writer app at low traffic).
    cp "$DB" "$OUT"
fi

gzip -f "$OUT"
echo "Backed up to ${OUT}.gz"

# Prune old snapshots.
ls -1t "$DEST"/algotrade-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
