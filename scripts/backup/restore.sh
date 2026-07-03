#!/bin/sh
# Restore a backup dump.
#
#   Drill (safe, default):  scripts/backup/restore.sh backups/ipo_X.dump ipo_restore_test
#   Real recovery:          scripts/backup/restore.sh backups/ipo_X.dump ipo
#
# Restores into TARGET_DB (created if missing; dropped and recreated if it exists).
set -eu

DUMP=${1:?usage: restore.sh <dumpfile> [target_db]}
TARGET=${2:-ipo_restore_test}

if [ "$TARGET" = "ipo" ]; then
  printf "About to OVERWRITE the production database 'ipo'. Type yes to continue: "
  read -r ans
  [ "$ans" = "yes" ] || { echo "aborted"; exit 1; }
fi

echo "[restore] restoring $DUMP into database '$TARGET'..."
docker compose exec -T db psql -U ipo -d postgres -c "DROP DATABASE IF EXISTS $TARGET;" >/dev/null
docker compose exec -T db psql -U ipo -d postgres -c "CREATE DATABASE $TARGET OWNER ipo;" >/dev/null
docker compose exec -T db pg_restore -U ipo -d "$TARGET" --no-owner < "$DUMP"
echo "[restore] done. Row counts:"
docker compose exec -T db psql -U ipo -d "$TARGET" -c \
  "SELECT 'stocks' t, count(*) FROM stocks UNION ALL
   SELECT 'stock_snapshots', count(*) FROM stock_snapshots UNION ALL
   SELECT 'analyses', count(*) FROM analyses UNION ALL
   SELECT 'composite_scores', count(*) FROM composite_scores;"
