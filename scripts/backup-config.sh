#!/bin/sh
# Back up the live config (and .env) of the Inkplate weather stack. Meant to run on the Pi.
#
#   scripts/backup-config.sh [STACK_DIR [KEEP]]
#
# STACK_DIR  the folder holding docker-compose.yml, config/ and .env (default: the current directory)
# KEEP       how many backups to keep, newest first (default: $BACKUP_KEEP, else 14)
#
# Writes STACK_DIR/backups/config-YYYYmmdd-HHMMSS.tar.gz (mode 600: config.yaml and .env hold secrets) and removes
# the oldest archives beyond KEEP. Only config/config.yaml and .env are archived; the cache is disposable.
# Exits non-zero on any failure, so cron mail or a calling script notices.
set -eu

die() {
    echo "backup-config: $*" >&2
    exit 1
}

stack=${1:-.}
keep=${2:-${BACKUP_KEEP:-14}}

case $keep in
    '' | *[!0-9]*) die "KEEP must be a whole number, got '$keep'" ;;
esac
[ "$keep" -ge 1 ] || die "KEEP must be at least 1"

cd "$stack" || die "cannot enter $stack"
[ -f config/config.yaml ] || die "no config/config.yaml in $(pwd)"

files="config/config.yaml"
if [ -f .env ]; then
    files="$files .env"
fi

umask 077
mkdir -p backups
chmod 700 backups

out="backups/config-$(date +%Y%m%d-%H%M%S).tar.gz"
tmp="$out.tmp"
[ ! -e "$out" ] || die "$out already exists (two backups in one second?)"
trap 'rm -f "$tmp"' EXIT

# shellcheck disable=SC2086  # $files is a fixed, space-free list
tar -czf "$tmp" $files || die "tar failed"
chmod 600 "$tmp"
mv "$tmp" "$out"
echo "backup-config: wrote $stack/$out ($files)"

# Prune: the names sort by time, so drop all but the newest $keep.
list=$(LC_ALL=C ls -1 backups/config-[0-9]*-[0-9]*.tar.gz 2>/dev/null || true)
total=0
if [ -n "$list" ]; then
    total=$(printf '%s\n' "$list" | wc -l | tr -d ' ')
fi
if [ "$total" -gt "$keep" ]; then
    drop=$((total - keep))
    printf '%s\n' "$list" | head -n "$drop" | while IFS= read -r old; do
        rm -f -- "$old"
        echo "backup-config: removed old $old"
    done
    total=$keep
fi
echo "backup-config: $total backup(s) kept (limit $keep)"
