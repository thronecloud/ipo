#!/bin/sh
# Daily Postgres backup with retention. Runs one dump immediately on start,
# then every 24h. Keeps the newest 14 dumps.
set -u

RETAIN=${BACKUP_RETAIN:-14}

while true; do
  ts=$(date -u +%Y%m%d_%H%M%S)
  f="/backups/ipo_${ts}.dump"
  if pg_dump -h db -U ipo -d ipo -Fc -f "$f"; then
    echo "[backup] $(date -u) wrote $f ($(du -h "$f" | cut -f1))"
  else
    echo "[backup] $(date -u) FAILED" >&2
    rm -f "$f"
  fi
  # retention: delete everything beyond the newest $RETAIN dumps
  ls -1t /backups/ipo_*.dump 2>/dev/null | tail -n +"$((RETAIN + 1))" | while read -r old; do
    rm -f "$old" && echo "[backup] pruned $old"
  done
  sleep 86400
done
