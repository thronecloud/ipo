#!/bin/bash
# First-boot data seed: when the Postgres volume is EMPTY (official image only
# runs /docker-entrypoint-initdb.d on a fresh data dir), restore the newest
# custom-format dump from /backups. Turns machine migration into:
#   git clone && copy backups/ && docker compose up -d
# Existing volumes are never touched — this script simply doesn't run again.
set -euo pipefail

newest=$(ls -1t /backups/ipo_*.dump 2>/dev/null | head -1 || true)

if [ -z "${newest}" ]; then
  echo "[db-init] no /backups/ipo_*.dump found — starting with an empty database"
  exit 0
fi

echo "[db-init] fresh volume detected — restoring ${newest} ($(du -h "${newest}" | cut -f1))"
# --no-owner: dump was taken as user ipo, restore target user is ipo anyway.
# Errors on individual objects abort (-e implied by set -e via exit status).
pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner "${newest}"
echo "[db-init] restore complete: $(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) || ' stocks, ' FROM stocks") $(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) || ' analyses' FROM analyses")"
