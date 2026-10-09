#!/usr/bin/env bash
# Daily gzipped pg_dump of the LuxPower database; keeps the newest KEEP dumps.
# Run as root (systemd timer luxpower-db-backup.timer).
set -euo pipefail

DB_NAME="${DB_NAME:-luxpower}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/luxpower/daily}"
KEEP="${KEEP:-14}"
PG_DUMP_CMD="${PG_DUMP_CMD:-runuser -u postgres -- pg_dump}"

# Dumps contain subscriber chat ids: owner-only files
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

file="$BACKUP_DIR/$DB_NAME-$(date +%Y%m%d-%H%M%S).sql.gz"
tmp="$file.tmp"
trap 'rm -f "$tmp"' EXIT

# shellcheck disable=SC2086  # PG_DUMP_CMD is a command line
$PG_DUMP_CMD "$DB_NAME" | gzip > "$tmp"
mv "$tmp" "$file"

# Rotation: newest first by mtime, drop everything after KEEP
ls -1t "$BACKUP_DIR"/"$DB_NAME"-*.sql.gz | tail -n +"$((KEEP + 1))" |
    while read -r old; do rm -f -- "$old"; done

echo "backup: $file ($(du -h "$file" | cut -f1)), kept newest $KEEP"
