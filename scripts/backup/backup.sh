#!/bin/sh
set -eu

DIR=/backups
KEEP=14
RETRY_DELAY=30
MAX_TRIES=20
CYCLE_TRIES=3
CYCLE_RETRY_DELAY=60
MIN_BYTES=1000000
MIN_FRACTION_NUM=1
MIN_FRACTION_DEN=2

log() { echo "[backup] $(date -u) $*"; }

notify() {
  [ -n "${NTFY_TOPIC:-}" ] || return 0
  curl -fsS -m 10 -o /dev/null -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" || true
}

# Process death (host reboot, OOM, restart mid-write) leaves a ~243 MB dotfile
# that the retention glob does not match and plain ls does not show.
sweep_partials() {
  for orphan in "$DIR"/.ipo_*.dump.partial; do
    [ -e "$orphan" ] || continue
    log "sweeping orphaned partial $orphan ($(wc -c < "$orphan") bytes)"
    rm -f "$orphan"
  done
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

newest_dump_size() {
  newest=$(ls -1t "$DIR"/ipo_[0-9]*.dump 2>/dev/null | head -n 1)
  [ -n "$newest" ] || return 1
  wc -c < "$newest"
}

# Sets FAIL_REASON and returns 1 on failure; the caller decides when to notify,
# so a transient failure that a retry clears never pages anyone.
take_backup() {
  FAIL_REASON=""
  ts=$(date -u +%Y%m%d_%H%M%S)
  tmp="$DIR/.ipo_${ts}.dump.partial"
  final="$DIR/ipo_${ts}.dump"

  if ! pg_dump -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$tmp"; then
    FAIL_REASON="pg_dump error"
    rm -f "$tmp"
    return 1
  fi

  # A dump we cannot list is a dump we cannot restore. Never promote it.
  if ! pg_restore -l "$tmp" >/dev/null 2>&1; then
    FAIL_REASON="dump did not verify (pg_restore -l could not read it)"
    rm -f "$tmp"
    return 1
  fi

  size=$(wc -c < "$tmp")
  # Measure against the last good dump. A fixed floor low enough to be safe on
  # a fresh install cannot catch a 243 MB dump collapsing to 2 MB, and once the
  # good dumps age out of retention that collapse is unrecoverable.
  if prev=$(newest_dump_size); then
    if [ $((size * MIN_FRACTION_DEN)) -lt $((prev * MIN_FRACTION_NUM)) ]; then
      FAIL_REASON="dump shrank to ${size} bytes from ${prev} bytes"
      rm -f "$tmp"
      return 1
    fi
  elif [ "$size" -lt "$MIN_BYTES" ]; then
    FAIL_REASON="first dump implausibly small (${size} bytes)"
    rm -f "$tmp"
    return 1
  fi

  if ! mv "$tmp" "$final"; then
    FAIL_REASON="could not promote ${tmp} to ${final}"
    rm -f "$tmp"
    return 1
  fi
  log "OK $final (${size} bytes)"

  # Prune only timestamped automated dumps. ipo_migration_*.dump and
  # manual_*.dump are operator artifacts and must survive retention.
  ls -1t "$DIR"/ipo_[0-9]*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    log "pruning $old"
    rm -f "$old"
  done
  return 0
}

# The db can restart in the same hour a cycle fires. Gating on readiness once at
# startup only protects the first cycle; every later one would take a connection
# refusal as the day's answer and sleep on it.
run_cycle() {
  n=1
  while [ "$n" -le "$CYCLE_TRIES" ]; do
    if wait_for_db; then
      if take_backup; then
        return 0
      fi
    else
      FAIL_REASON="db unreachable after $MAX_TRIES readiness probes"
    fi
    log "attempt $n/$CYCLE_TRIES failed: $FAIL_REASON"
    n=$((n + 1))
    if [ "$n" -le "$CYCLE_TRIES" ]; then
      sleep "$CYCLE_RETRY_DELAY"
    fi
  done
  log "FAILED after $CYCLE_TRIES attempts: $FAIL_REASON"
  notify "ipo backup FAILED: $FAIL_REASON"
  return 1
}

log "backup service starting"
[ -n "${NTFY_TOPIC:-}" ] || log "NTFY_TOPIC unset — failure notifications are DISABLED"

sweep_partials

while true; do
  run_cycle || log "backup cycle failed; will retry next cycle"
  sleep 86400
done
