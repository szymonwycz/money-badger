#!/bin/bash
# Money Badger — database backup.
# Daily: consistent SQLite snapshot to $BACKUP_DIR (90-day retention).
# Weekly (Sunday): snapshot committed and pushed to a private git mirror, if set up.
#
# Paths come from the environment so this works wherever the app is installed;
# the defaults match what install.sh creates.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB="${BUDGET_DB:-$APP_DIR/budget.db}"
DIR="${BACKUP_DIR:-$HOME/backups/money-badger/daily}"
MIRROR="${BACKUP_MIRROR:-$HOME/backups/money-badger/github-mirror}"
STAMP=$(date +%F)

mkdir -p "$DIR"

# sqlite3 CREATES an empty database if $DB is missing, and .backup of that exits 0.
# The retention sweep below would then quietly delete every real snapshot while
# systemd kept showing green. Refuse to back up something that isn't there.
if [ ! -s "$DB" ]; then
    echo "backup FAILED: $DB missing or empty" >&2
    exit 1
fi

# .backup is safe against a live WAL database, unlike plain cp
sqlite3 "$DB" ".backup '$DIR/budget-$STAMP.db'"
gzip -f "$DIR/budget-$STAMP.db"

# Sanity-check the snapshot before anything old gets deleted.
if ! gzip -t "$DIR/budget-$STAMP.db.gz" 2>/dev/null; then
    echo "backup FAILED: snapshot $STAMP is not a valid gzip" >&2
    exit 1
fi

find "$DIR" -name 'budget-*.db.gz' -mtime +90 -delete

if [ "$(date +%u)" = 7 ] && [ -d "$MIRROR/.git" ]; then
    cp "$DIR/budget-$STAMP.db.gz" "$MIRROR/budget-latest.db.gz"

    # The database alone doesn't get you running again: without these you would
    # be re-authorizing every bank account and rebuilding the rules by hand.
    # They contain IBANs and a password hash, so the mirror must be private.
    mkdir -p "$MIRROR/config"
    for f in .env sync/config.json sync/rules.json sync/.bank_sync_state.json known_issues.json; do
        [ -f "$APP_DIR/$f" ] && cp "$APP_DIR/$f" "$MIRROR/config/$(basename "$f")"
    done
    chmod 600 "$MIRROR"/config/* 2>/dev/null || true
    cd "$MIRROR"
    git add -A
    # `|| exit 0` used to swallow every commit failure — missing user.email, a lock
    # file, a full disk — and report success, so the offsite mirror could stop
    # updating for weeks with a green systemd status. Only "nothing to commit" is fine.
    if git diff --cached --quiet; then
        echo "mirror: snapshot identical to last week's, nothing to push"
    else
        git commit -m "weekly db snapshot $STAMP"
        git push
    fi
fi

echo "backup ok: $DIR/budget-$STAMP.db.gz"
