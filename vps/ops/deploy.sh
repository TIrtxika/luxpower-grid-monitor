#!/usr/bin/env bash
# Deploy the vps/ bot code of a GitHub commit to /opt/luxpower.
#
# Usage: sudo deploy.sh <commit> <sha256-manifest>
#   The manifest ("<sha256>  <file>" per deployed file) is made on the dev
#   machine from git; the deploy stops if GitHub served anything else.
#
# Steps: download -> verify checksums -> compile -> back up current code ->
#        install -> restart units -> check logs (tracebacks, errors, tokens).
set -euo pipefail

REPO="${REPO:-TIrtxika/luxpower-grid-monitor}"
APP_DIR="${APP_DIR:-/opt/luxpower}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/luxpower}"
UNITS="${UNITS:-luxpower-bot-public luxpower-bot-private}"
INSTALL_OPTS="${INSTALL_OPTS--o luxpower -g luxpower}"
# Runs as root: never execute anything the bot user can write (its venv).
# The root-owned system interpreter is enough for a syntax check.
PYTHON="${PYTHON:-/usr/bin/python3}"
SHA256="${SHA256:-sha256sum}"
SETTLE_SECONDS="${SETTLE_SECONDS:-15}"
SOURCE_TGZ="${SOURCE_TGZ:-}"   # tests: a local tarball instead of GitHub

commit="${1:-}"
if [[ ! "$commit" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "usage: deploy.sh <commit-sha> <sha256-manifest>" >&2
    exit 2
fi
if [[ -z "${2:-}" || ! -f "$2" ]]; then
    echo "missing sha256 manifest" >&2
    exit 2
fi
manifest="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"

work="$(mktemp -d "${TMPDIR:-/tmp}/lux-deploy.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# 1. Download
if [[ -n "$SOURCE_TGZ" ]]; then
    cp "$SOURCE_TGZ" "$work/src.tgz"
else
    curl -fsSL -o "$work/src.tgz" "https://github.com/$REPO/archive/$commit.tar.gz"
fi
tar -xzf "$work/src.tgz" -C "$work"
src="$(ls -d "$work"/*/vps)"

files=()
for path in "$src"/*.py "$src"/requirements.txt; do
    name="$(basename "$path")"
    [[ "$name" == test_* ]] && continue
    files+=("$name")
done

# 2. Verify: every deployed file is in the manifest and matches it
for name in "${files[@]}"; do
    if ! grep -q "  $name\$" "$manifest"; then
        echo "checksum manifest has no entry for $name — nothing deployed" >&2
        exit 3
    fi
done
# shellcheck disable=SC2086  # SHA256 may be "shasum -a 256"
if ! (cd "$src" && $SHA256 -c "$manifest" >/dev/null); then
    echo "checksum mismatch — nothing deployed" >&2
    exit 3
fi

# 3. Compile (syntax check with the app's interpreter)
"$PYTHON" -c 'import sys; [compile(open(f, encoding="utf-8").read(), f, "exec")
                            for f in sys.argv[1:] if f.endswith(".py")]' \
    "${files[@]/#/$src/}"

# 4. Back up the current code
backup="$BACKUP_ROOT/code-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
cp -a "$APP_DIR"/*.py "$APP_DIR"/requirements.txt "$backup"/

# 5. Install
# shellcheck disable=SC2086  # INSTALL_OPTS is a list of options
(cd "$src" && install $INSTALL_OPTS -m 644 "${files[@]}" "$APP_DIR"/)

# 6. Restart and check
rollback="rollback: cp -a $backup/* $APP_DIR/ && systemctl restart $UNITS"
since="$(date '+%Y-%m-%d %H:%M:%S')"
# shellcheck disable=SC2086
systemctl restart $UNITS
sleep "$SETTLE_SECONDS"
# shellcheck disable=SC2086
if ! systemctl is-active $UNITS; then
    echo "a unit is not active — $rollback" >&2
    exit 1
fi

journal_args=()
for unit in $UNITS; do journal_args+=(-u "$unit"); done
logs="$(journalctl "${journal_args[@]}" --since "$since" --no-pager -o cat)"
count() { grep -c -- "$1" <<<"$logs" || true; }
tracebacks="$(count 'Traceback')"
errors="$(count '\[ERROR\]')"
tokens="$(count 'api.telegram.org/bot')"

echo "deployed $commit (${#files[@]} files), backup: $backup"
echo "since $since: tracebacks=$tracebacks errors=$errors token_urls=$tokens"
if (( tracebacks + errors + tokens > 0 )); then
    echo "problems in the logs — $rollback" >&2
    exit 1
fi
