#!/bin/sh
# Restore a backup dump.
#
#   Drill (safe, default):  scripts/backup/restore.sh backups/ipo_X.dump ipo_restore_test
#   Real recovery:          scripts/backup/restore.sh backups/ipo_X.dump ipo
#
# Restores into TARGET_DB (created if missing; dropped and recreated if it exists).
set -eu

KEEP_SAFETY=3

DUMP=${1:?usage: restore.sh <dumpfile> [target_db]}
TARGET=${2:-ipo_restore_test}

if [ "$TARGET" = "ipo" ]; then
  printf "About to OVERWRITE the production database 'ipo'. Type yes to continue: "
  read -r ans
  [ "$ans" = "yes" ] || { echo "aborted"; exit 1; }
fi

echo "[restore] restoring $DUMP into database '$TARGET'..."

# Never destroy a working database without a copy of it first. Written to the
# host side of the ./backups mount, since the container sees /backups read-only.
if docker compose exec -T db psql -U ipo -lqt | cut -d'|' -f1 | grep -qw "$TARGET"; then
  name="presafety_$(date -u +%Y%m%d_%H%M%S).dump"
  safety="backups/$name"
  echo "[restore] taking safety dump of current $TARGET -> $safety"
  if ! docker compose exec -T db pg_dump -U ipo -d "$TARGET" -Fc > "$safety"; then
    rm -f "$safety"
    echo "[restore] FATAL: could not take safety dump; aborting" >&2
    exit 1
  fi
  # This dump is written by a host-side stream redirect, which is precisely
  # where a full disk produces a truncated file. `pg_restore -l` reads only the
  # TOC at the head of the archive and would call that file good; `-f /dev/null`
  # decodes every entry, so a short file fails here instead of after DROP.
  if ! docker compose exec -T db pg_restore -f /dev/null "/backups/$name" >/dev/null 2>&1; then
    rm -f "$safety"
    echo "[restore] FATAL: safety dump did not verify; aborting" >&2
    exit 1
  fi

  # Each of these is a full-size dump. Unpruned, a few dozen drills fill the
  # disk and take the daily backup down with them. Deliberately kept outside
  # the ipo_[0-9]*.dump glob so first-boot seeding can never select one.
  ls -1t backups/presafety_*.dump 2>/dev/null | tail -n +$((KEEP_SAFETY + 1)) | while read -r old; do
    echo "[restore] pruning old safety dump $old"
    rm -f "$old"
  done
fi

docker compose exec -T db psql -U ipo -d postgres -c "DROP DATABASE IF EXISTS $TARGET;" >/dev/null
docker compose exec -T db psql -U ipo -d postgres -c "CREATE DATABASE $TARGET OWNER ipo;" >/dev/null
# --exit-on-error: stop at the first failing object instead of working through
# the rest of the archive and leaving a partial database behind.
docker compose exec -T db pg_restore --exit-on-error -U ipo -d "$TARGET" --no-owner < "$DUMP"
echo "[restore] done. Row counts:"
docker compose exec -T db psql -U ipo -d "$TARGET" -c \
  "SELECT 'stocks' t, count(*) FROM stocks UNION ALL
   SELECT 'stock_snapshots', count(*) FROM stock_snapshots UNION ALL
   SELECT 'analyses', count(*) FROM analyses UNION ALL
   SELECT 'composite_scores', count(*) FROM composite_scores;"
