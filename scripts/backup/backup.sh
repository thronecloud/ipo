#!/bin/sh
set -eu

DIR=/backups
KEEP=14
RETRY_DELAY=30
MAX_TRIES=20

log() { echo "[backup] $(date -u) $*"; }

notify() {
  [ -n "${NTFY_TOPIC:-}" ] || return 0
  wget -q -O /dev/null --post-data="$1" "https://ntfy.sh/${NTFY_TOPIC}" || true
}

wait_for_db() {
  i=1
  while [ "$i" -le "$MAX_TRIES" ]; do
    if pg_isready -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      return 0
    fi
    log "db not ready (attempt $i/$MAX_TRIES), sleeping ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
    i=$((i + 1))
  done
  return 1
}

take_backup() {
  ts=$(date -u +%Y%m%d_%H%M%S)
  tmp="$DIR/.ipo_${ts}.dump.partial"
  final="$DIR/ipo_${ts}.dump"

  if ! pg_dump -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$tmp"; then
    log "FAILED pg_dump"
    rm -f "$tmp"
    notify "ipo backup FAILED: pg_dump error"
    return 1
  fi

  # A dump we cannot list is a dump we cannot restore. Never promote it.
  if ! pg_restore -l "$tmp" >/dev/null 2>&1; then
    log "FAILED verification: pg_restore -l could not read the dump"
    rm -f "$tmp"
    notify "ipo backup FAILED: dump did not verify"
    return 1
  fi

  size=$(wc -c < "$tmp")
  if [ "$size" -lt 1000000 ]; then
    log "FAILED verification: dump is only ${size} bytes"
    rm -f "$tmp"
    notify "ipo backup FAILED: dump implausibly small (${size} bytes)"
    return 1
  fi

  mv "$tmp" "$final"
  log "OK $final (${size} bytes)"

  # Prune only timestamped automated dumps. ipo_migration_*.dump and
  # manual_*.dump are operator artifacts and must survive retention.
  ls -1t "$DIR"/ipo_[0-9]*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    log "pruning $old"
    rm -f "$old"
  done
  return 0
}

log "backup service starting"
if ! wait_for_db; then
  log "db never became ready"
  notify "ipo backup FAILED: db unreachable after $MAX_TRIES tries"
fi

while true; do
  take_backup || log "backup cycle failed; will retry next cycle"
  sleep 86400
done
