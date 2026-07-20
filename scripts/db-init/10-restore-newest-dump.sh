#!/bin/bash
# First-boot data seed: when the Postgres volume is EMPTY (official image only
# runs /docker-entrypoint-initdb.d on a fresh data dir), restore the newest
# custom-format dump from /backups. Turns machine migration into:
#   git clone && copy backups/ && docker compose up -d
# The entrypoint only runs this on a fresh volume, so a populated volume is
# never seeded twice. It is NOT inert if invoked by hand against a running
# container: restoring into a populated database fails, which takes the abort
# path below. That path is what makes the guard in first_boot_only() mandatory.
set -euo pipefail

# The entrypoint's first-boot temp server is socket-only (-c listen_addresses='');
# a server serving real traffic is not. Empty here means "this process is the
# initialization the data directory is being created by", which is the only
# situation in which discarding that data directory is a repair rather than a
# deletion. Anything else — including a probe we cannot run — is a refusal.
first_boot_only() {
  local addrs
  if ! addrs=$(psql --username "$POSTGRES_USER" --dbname postgres -tAc 'SHOW listen_addresses'); then
    echo "[db-init] cannot confirm this is first-boot init — refusing to touch $PGDATA" >&2
    exit 1
  fi
  if [ -n "$addrs" ]; then
    echo "[db-init] not running under first-boot init — refusing to touch $PGDATA" >&2
    exit 1
  fi
}

# A failed init script still leaves the initdb-created cluster on disk, so the
# next boot sees PG_VERSION, skips initialization, and serves an EMPTY database
# as "healthy" — api then runs alembic and the scheduler writes into it. Only a
# fresh volume reaches this script, so nothing here is worth keeping: wipe it so
# the restore is retried on every restart instead of silently giving up.
abort() {
  echo "[db-init] FATAL: $*" >&2
  first_boot_only
  echo "[db-init] Refusing to leave a partial database; discarding the data directory." >&2
  rm -rf "${PGDATA:?PGDATA unset}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  # Removing only some of it is the worst outcome: without PG_VERSION the next
  # boot re-runs initdb, which refuses a non-empty directory and crash-loops on
  # an error that names neither this script nor the restore.
  if [ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "[db-init] FATAL: could not clear $PGDATA; it is now partially removed and" >&2
    echo "[db-init] must be emptied by hand before this container will start." >&2
    exit 1
  fi
  exit 1
}

newest=$(ls -1t /backups/ipo_*.dump 2>/dev/null | head -1 || true)

if [ -z "${newest}" ]; then
  echo "[db-init] no /backups/ipo_*.dump found — starting with an empty database"
  exit 0
fi

echo "[db-init] fresh volume detected — restoring ${newest} ($(du -h "${newest}" | cut -f1))"
# --no-owner: dump was taken as user ipo, restore target user is ipo anyway.
# --exit-on-error stops at the first failing object. Without it pg_restore works
# through the rest of the archive applying whatever it can, so a bad dump writes
# a partial schema before it reports anything.
if ! pg_restore --exit-on-error --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner "${newest}"; then
  abort "pg_restore failed on ${newest}"
fi

# A dump can restore with no errors at all and still be useless — a schema-only
# or empty-source dump exits 0 and yields zero rows. This is the check that
# turns that into a failure instead of a healthy, empty stack.
if ! count=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tAc "SELECT count(*) FROM stocks;"); then
  abort "restore reported success but the stocks table is not queryable"
fi
if [ "${count:-0}" -lt 1 ]; then
  abort "restore completed but stocks table is empty (count=${count})"
fi
echo "[db-init] restore verified: ${count} stocks"
