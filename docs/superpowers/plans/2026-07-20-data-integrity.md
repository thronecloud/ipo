# Data Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every number the system stores, displays, and analyses provably correct, fresh, and comparable across stocks — so the analysis corpus is trustworthy research input rather than expensive noise.

**Architecture:** Fix in dependency order: durability first (so nothing is lost while we work), then unit correctness at a single canonical boundary, then data freshness at the point of use, then analysis-integrity gates that stop bad rows entering the corpus, and finally a homogenisation re-run that makes composites comparable across stocks. Each phase leaves the system strictly better than it found it and is independently shippable.

**Tech Stack:** Python 3.10, SQLAlchemy 2.x + Alembic, Postgres 16, FastAPI, Next.js 15 (App Router, TypeScript), pytest, Docker Compose.

## Global Constraints

- Repo `/home/magical/Coding/ipo`, branch `living-engine`. `main` is stale — never target it.
- **`src/` is LIVE**, not legacy. `src/personas.py`, `src/analyze.py`, `src/fetch_stock_data.py`, `src/fetch_screener_data.py`, `src/fetch_ipo_list.py` are imported by `engine/`. Only `src/utils.py` is unreferenced. Do not delete anything under `src/`.
- Tests run in-container: `docker compose --profile test run --rm test`. Baseline is **189 passed**. No task may reduce that number.
- Never use `git add -A`. Run `git status` first, stage explicit paths.
- **Never add Claude or any AI as a commit author or co-author.** No `Co-Authored-By` trailers.
- Never skip or disable a pre-commit hook.
- Do not run `docker compose down -v` at any point in this plan.
- Migrations must be linear. Current head is `53de03c90075`. Every new migration needs a working `downgrade()`.
- Canonical units, decided once here and referenced by every later task:
  - `roe` — **percent** (39.4 means 39.4%)
  - `revenue_growth` — **percent** (18.5 means 18.5%)
  - `debt_to_equity` — **ratio** (0.098 means 0.098x)
  - `market_cap_cr` — **crore**
  - `current_price` — **rupees**

---

## Measured Baseline

These numbers were measured against live prod on 2026-07-20 and justify the task ordering. Re-measure after Phase 4 to confirm.

| Metric | Value |
|---|---|
| Analyses in corpus | 5,567 across 2,657 stocks |
| Composites | 572 |
| Automated backups ever taken | **0** |
| Analyses with >5% price drift vs real close | 183 (5.9% of 3,126 checkable) |
| Analyses with >20% drift | 72 |
| Mean snapshot age at analysis time | 2.5 days (max 77) |
| Composites at coverage=1 | 72 |
| Composites at coverage 10 | 429 |
| Stocks with mixed prompt versions | 60 |
| Stocks with mixed models | 12 |
| **Mean composite, `opus`-dominant stocks** | **24.1** (n=86) |
| **Mean composite, `sonnet`-dominant stocks** | **33.9** (n=369) |
| Population SD of composite | ~11 |

The last three rows are the headline: model choice shifts a stock's composite by ~10 points, ~0.9 SD, for reasons unrelated to the business. This makes the book non-comparable cross-sectionally and is addressed in Phase 4.

---

## File Structure

**Phase 0 — durability**
- Modify `scripts/backup/backup.sh` — readiness wait, retry, atomic write, post-write verification, failure notification, corrected retention glob
- Modify `scripts/db-init/10-restore-newest-dump.sh` — `--exit-on-error`, non-empty assertion
- Modify `scripts/backup/restore.sh` — pre-restore safety dump, `--exit-on-error`
- Modify `docker-compose.yml` — wire `NTFY_TOPIC`, bind Postgres to loopback

**Phase 1 — unit correctness**
- Create `api/units.py` — the single canonical unit-normalisation boundary
- Modify `api/routers/consumer.py` — apply normalisation in list + detail
- Modify `web/app/(consumer)/stock/[symbol]/page.tsx` — D/E label and formatting
- Create `tests/test_units.py` — exact-value scale assertions

**Phase 2 — freshness & provenance**
- Modify `engine/analysis/prompt.py` — price from `daily_prices`, not stale snapshot
- Modify `engine/repo.py` — `upsert_daily_prices` corrects re-adjusted bars
- Create `alembic/versions/*_add_field_provenance.py` — `sector_source`, `isin_source`
- Modify `engine/quality/gapfill.py` — stamp provenance on every imputed write
- Modify `engine/quality/dimensions.py` — imputed fields score partial credit
- Modify `tests/test_daily_prices.py`, `tests/test_data_quality.py`

**Phase 3 — analysis integrity**
- Create `engine/analysis/errors.py` — transient vs permanent classification
- Modify `engine/analysis/engine.py` — do not dead-letter transient failures
- Modify `engine/scheduler.py` — count attempts not successes; orphan reaper
- Modify `engine/repo.py` — coverage gate on `CompositeScoreHistory`
- Modify `engine/scoring/confidence.py` — `n=1` uncertainty floor, direction-aware tier
- Modify `tests/test_analysis_deadletter.py`, `tests/test_confidence.py`, `tests/test_scoring.py`

**Phase 4 — homogenisation**
- Create `scripts/rescore_campaign.py` — targeted re-analysis driver
- Modify `engine/repo.py` — record `prompt_version` + `model` on composites

---

# PHASE 0 — Durability

Nothing else is safe until this is done. A manual verified dump already exists at `backups/manual_20260720.dump` (sha256 prefix `df406f98b791…`); do not delete it.

### Task 1: Backup that actually runs

**Files:**
- Modify: `scripts/backup/backup.sh`
- Modify: `docker-compose.yml` (backup service env)

**Interfaces:**
- Produces: verified dumps named `ipo_<YYYYMMDD_HHMMSS>.dump` in `/backups`; retention prunes only that pattern.

- [ ] **Step 1: Read the current script**

Run: `cat scripts/backup/backup.sh`
Note the two defects to fix: it dumps before Postgres is reachable and then sleeps 86400; and its retention glob `ipo_*.dump` also matches `ipo_migration_20260719.dump`.

- [ ] **Step 2: Replace the script**

```bash
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
```

- [ ] **Step 3: Wire NTFY_TOPIC and PGHOST into the backup service**

In `docker-compose.yml`, under the `backup` service `environment:` block, add:

```yaml
      PGHOST: db
      PGPASSWORD: ${POSTGRES_PASSWORD:-ipo}
      NTFY_TOPIC: ${NTFY_TOPIC:-}
```

- [ ] **Step 4: Rebuild and verify a dump lands**

```bash
docker compose up -d --force-recreate backup
sleep 90
docker logs ipo_backup --tail 20
ls -la backups/
```

Expected: log shows `[backup] ... OK /backups/ipo_<ts>.dump (<size> bytes)`, and a new `ipo_<ts>.dump` exists. If it shows `db not ready`, that is the retry working — wait another 30s.

- [ ] **Step 5: Prove the restart race is fixed**

```bash
docker compose restart backup db
sleep 120
docker logs ipo_backup --tail 20
```

Expected: at least one `db not ready` line followed by `OK`. Before this change, this sequence produced `Connection refused` then a 24h sleep.

- [ ] **Step 6: Commit**

```bash
git status
git add scripts/backup/backup.sh docker-compose.yml
git commit -m "backup: wait for db, verify dumps before promoting, notify on failure

The backup container raced Postgres on every restart and slept 24h after
losing, so no automated dump had ever been taken. Dumps also went straight
to their final filename with no verification, so a truncated file could
become the newest dump the restore path selects.

Retention glob narrowed to ipo_[0-9]*.dump so it stops matching the
hand-made ipo_migration_*.dump."
```

### Task 2: Restore cannot silently produce an empty database

**Files:**
- Modify: `scripts/db-init/10-restore-newest-dump.sh`
- Modify: `scripts/backup/restore.sh`

- [ ] **Step 1: Confirm the flag default in the actual image**

Run: `docker exec ipo_postgres pg_restore --help | grep -A1 exit-on-error`
Expected: `-e, --exit-on-error  exit on error, default is to continue`

This is the point: without `-e`, pg_restore continues past object errors and exits 0 on a partial restore. The comment in the script claiming `set -e` covers this is wrong.

- [ ] **Step 2: Harden the first-boot restore**

In `scripts/db-init/10-restore-newest-dump.sh`, replace the `pg_restore` invocation and the comment above it with:

```sh
# -e is REQUIRED: without it pg_restore continues past object errors and exits 0,
# producing a partial database that looks like a successful restore.
if ! pg_restore --exit-on-error --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner "${newest}"; then
  echo "[db-init] FATAL: pg_restore failed on ${newest}" >&2
  echo "[db-init] Refusing to leave a partial database. Fix the dump and recreate the volume." >&2
  exit 1
fi

# A restore that yields an empty stocks table is a failed restore, whatever the exit code.
count=$(psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -tAc "SELECT count(*) FROM stocks;")
if [ "${count:-0}" -lt 1 ]; then
  echo "[db-init] FATAL: restore completed but stocks table is empty (count=${count})" >&2
  exit 1
fi
echo "[db-init] restore verified: ${count} stocks"
```

- [ ] **Step 3: Harden the operator restore drill**

In `scripts/backup/restore.sh`, immediately before the `DROP DATABASE` call, add a safety dump, and add `--exit-on-error` to its `pg_restore`:

```sh
# Never destroy a working database without a copy of it first.
if psql -U "$POSTGRES_USER" -lqt | cut -d'|' -f1 | grep -qw "$TARGET"; then
  safety="/backups/presafety_$(date -u +%Y%m%d_%H%M%S).dump"
  echo "[restore] taking safety dump of current $TARGET -> $safety"
  pg_dump -U "$POSTGRES_USER" -d "$TARGET" -Fc -f "$safety" || {
    echo "[restore] FATAL: could not take safety dump; aborting" >&2
    exit 1
  }
fi
```

- [ ] **Step 4: Verify the guard fires on a bad dump**

```bash
head -c 50000 backups/manual_20260720.dump > /tmp/truncated.dump
docker cp /tmp/truncated.dump ipo_postgres:/tmp/truncated.dump
docker exec ipo_postgres sh -c 'pg_restore -l /tmp/truncated.dump >/dev/null 2>&1; echo "verify exit=$?"'
docker exec ipo_postgres rm -f /tmp/truncated.dump
rm -f /tmp/truncated.dump
```

Expected: `verify exit=1`. This is the check Task 1 Step 2 added to the backup path; it proves a truncated dump is detectable.

- [ ] **Step 5: Commit**

```bash
git status
git add scripts/db-init/10-restore-newest-dump.sh scripts/backup/restore.sh
git commit -m "restore: fail loudly instead of producing a partial database

pg_restore defaults to continue-on-error, so a truncated or corrupt dump
produced a partial restore that exited 0. The container then came up
healthy, alembic built the remaining schema, and the scheduler began
writing into what looked like a populated database.

Adds --exit-on-error, a post-restore non-empty assertion, and a safety
dump before the operator restore drops an existing database."
```

### Task 3: Alerts can reach the owner; Postgres is not on the public interface

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Confirm both problems**

```bash
grep -n "NTFY\|NOTIFY" docker-compose.yml || echo "NOT WIRED — every alert falls through to a print()"
docker port ipo_postgres
```

Expected: the grep prints the "NOT WIRED" message; `docker port` shows `5432/tcp -> 0.0.0.0:5432`.

- [ ] **Step 2: Bind Postgres to loopback**

In `docker-compose.yml`, under the `db` service, change the published port to:

```yaml
    ports:
      - "127.0.0.1:5432:5432"
```

- [ ] **Step 3: Wire notifications into api and scheduler**

Add to the `environment:` block of **both** the `api` and `scheduler` services:

```yaml
      NTFY_TOPIC: ${NTFY_TOPIC:-}
      NOTIFY_WEBHOOK_URL: ${NOTIFY_WEBHOOK_URL:-}
```

- [ ] **Step 4: Create the .env entry**

```bash
grep -q '^NTFY_TOPIC=' .env 2>/dev/null || echo "NTFY_TOPIC=ipo-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env
grep NTFY_TOPIC .env
```

Subscribe to that topic in the ntfy app. `.env` is gitignored — confirm with `git check-ignore .env`.

- [ ] **Step 5: Apply and verify end to end**

```bash
docker compose up -d db api scheduler
sleep 20
docker port ipo_postgres
docker exec ipo_scheduler python -c "from engine.notify import notify_safe; print(notify_safe('ipo test', 'alerting is wired'))"
```

Expected: port shows `127.0.0.1:5432`; the notify call returns `True` and a push arrives on your phone. If it prints `[notify] (no channel configured)`, the env var did not reach the container.

- [ ] **Step 6: Commit**

```bash
git status
git add docker-compose.yml
git commit -m "ops: wire NTFY_TOPIC into api+scheduler, bind Postgres to loopback

NTFY_TOPIC appeared nowhere in compose, so every failure alert in the
engine fell through to a print() into container stdout. Postgres was
published on 0.0.0.0:5432 with credentials ipo:ipo."
```

### Task 4: Reap orphaned job runs on boot

**Files:**
- Modify: `engine/scheduler.py`
- Test: `tests/test_observability.py`

**Interfaces:**
- Produces: `reap_orphaned_runs(session) -> int`, callable from scheduler startup.

- [ ] **Step 1: Confirm the zombie exists**

Run:
```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "SELECT id, job_type, started_at FROM job_runs WHERE status='running';"
```
Expected: at least one row — a 15-day-old `analyze` run. `status='running'` is written in exactly one place (`engine/repo.py:74`) and nothing ever clears it when the process dies.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_observability.py`:

```python
def test_reap_orphaned_runs_marks_stranded_runs_as_error(db_session):
    from engine.repo import JobRun
    from engine.scheduler import reap_orphaned_runs

    stranded = JobRun(job_type="analyze", status="running",
                      started_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
    live = JobRun(job_type="score", status="success",
                  started_at=datetime(2026, 7, 19, tzinfo=timezone.utc))
    db_session.add_all([stranded, live])
    db_session.commit()

    reaped = reap_orphaned_runs(db_session)

    db_session.refresh(stranded)
    db_session.refresh(live)
    assert reaped == 1
    assert stranded.status == "error"
    assert "orphaned" in (stranded.error or "")
    assert stranded.finished_at is not None
    assert live.status == "success"
```

Ensure `from datetime import datetime, timezone` is imported at the top of the file.

- [ ] **Step 3: Run it and watch it fail**

Run: `docker compose --profile test run --rm test pytest tests/test_observability.py::test_reap_orphaned_runs_marks_stranded_runs_as_error -v`
Expected: FAIL — `ImportError: cannot import name 'reap_orphaned_runs'`

- [ ] **Step 4: Implement**

Add to `engine/scheduler.py`:

```python
def reap_orphaned_runs(session) -> int:
    """Mark job_runs stranded at 'running' by process death as errored.

    status='running' is only ever cleared by the job's own finally block, so a
    container restart, rebuild, or OOM kill leaves the row running forever.
    """
    from sqlalchemy import update
    from engine.repo import JobRun

    result = session.execute(
        update(JobRun)
        .where(JobRun.status == "running")
        .values(status="error",
                error="orphaned by process restart",
                finished_at=datetime.now(timezone.utc))
    )
    session.commit()
    return result.rowcount
```

Then call it during scheduler startup, immediately before the scheduler starts ticking:

```python
    with session_scope() as s:
        n = reap_orphaned_runs(s)
        if n:
            print(f"[scheduler] reaped {n} orphaned job_run(s)")
```

- [ ] **Step 5: Verify green**

Run: `docker compose --profile test run --rm test pytest tests/test_observability.py -v`
Expected: PASS, including the new test.

- [ ] **Step 6: Commit**

```bash
git status
git add engine/scheduler.py tests/test_observability.py
git commit -m "scheduler: reap job_runs orphaned by process restart

23 of these were cleared by hand previously; the root cause was never
fixed and a new 15-day-old zombie analyze run had accumulated. Any
'currently running' indicator reads these as live work."
```

---

# PHASE 1 — Numbers parse correctly

Three metrics are wrong on screen today. All three share one root cause: yfinance's native scales are stored raw and the frontend guesses at them. The fix is one canonical boundary, not three component patches.

### Task 5: Canonical unit normalisation

**Files:**
- Create: `api/units.py`
- Create: `tests/test_units.py`

**Interfaces:**
- Produces: `normalize_quote(raw: dict) -> dict` converting yfinance-native scales to the canonical units in Global Constraints. Consumed by Task 6.

- [ ] **Step 1: Confirm the raw scales in prod**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
SELECT symbol, roe, debt_to_equity, revenue_growth
FROM stocks s JOIN stock_snapshots ss ON ss.stock_id=s.id
WHERE ss.roe IS NOT NULL ORDER BY ss.captured_at DESC LIMIT 5;"
docker exec ipo_postgres psql -U ipo -d ipo -c "
SELECT round(min(debt_to_equity)::numeric,3) lo,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY debt_to_equity)::numeric,3) med,
       round(max(debt_to_equity)::numeric,1) hi
FROM stock_snapshots WHERE debt_to_equity IS NOT NULL;"
```

Expected: `roe` values like `0.394` (a fraction, so the UI's `pct()` renders "+0.4%" instead of 39.4%), and `debt_to_equity` with a median near 9.7 and max near 637 — confirming yfinance returns D/E as a percentage, not a ratio.

- [ ] **Step 2: Write the failing test**

Create `tests/test_units.py`:

```python
"""Unit-scale contract for quote fields.

yfinance returns these three fields on three different scales. Every one of
them was being rendered on the wrong scale in the UI. These assertions pin
the canonical scale so a regression fails here rather than on screen.
"""
import pytest

from api.units import normalize_quote


def test_roe_fraction_becomes_percent():
    # yfinance returnOnEquity for IEX is 0.39419997 -> 39.4%
    assert normalize_quote({"roe": 0.39419997})["roe"] == pytest.approx(39.42, abs=0.01)


def test_revenue_growth_fraction_becomes_percent():
    assert normalize_quote({"revenue_growth": 0.185})["revenue_growth"] == pytest.approx(18.5, abs=0.01)


def test_negative_revenue_growth_keeps_sign():
    assert normalize_quote({"revenue_growth": -0.048})["revenue_growth"] == pytest.approx(-4.8, abs=0.01)


def test_debt_to_equity_percent_becomes_ratio():
    # yfinance debtToEquity 637.09 means 6.3709x, not 637x
    assert normalize_quote({"debt_to_equity": 637.09})["debt_to_equity"] == pytest.approx(6.3709, abs=0.0001)


def test_debt_to_equity_near_zero_is_not_mistaken_for_a_ratio():
    # 9.745 is the prod median: 0.09745x, i.e. essentially debt-free.
    assert normalize_quote({"debt_to_equity": 9.745})["debt_to_equity"] == pytest.approx(0.09745, abs=0.00001)


def test_price_and_pe_pass_through_untouched():
    out = normalize_quote({"current_price": 1234.5, "pe_ratio": 42.0})
    assert out["current_price"] == 1234.5
    assert out["pe_ratio"] == 42.0


def test_none_stays_none_and_is_never_coerced_to_zero():
    out = normalize_quote({"roe": None, "debt_to_equity": None, "revenue_growth": None})
    assert out["roe"] is None
    assert out["debt_to_equity"] is None
    assert out["revenue_growth"] is None


def test_zero_is_preserved_and_distinguishable_from_missing():
    assert normalize_quote({"roe": 0.0})["roe"] == 0.0


def test_unknown_keys_are_passed_through():
    assert normalize_quote({"data_quality": "full"})["data_quality"] == "full"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `docker compose --profile test run --rm test pytest tests/test_units.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.units'`

- [ ] **Step 4: Implement**

Create `api/units.py`:

```python
"""The single boundary where yfinance-native scales become canonical units.

yfinance is inconsistent: returnOnEquity and revenueGrowth are fractions
(0.39 = 39%), while debtToEquity is already a percentage (637.09 = 6.37x).
Storing all three raw and letting each consumer guess produced three
separate display bugs. Convert once, here.

Canonical units:
  roe             percent  (39.4 means 39.4%)
  revenue_growth  percent  (18.5 means 18.5%)
  debt_to_equity  ratio    (0.098 means 0.098x)
"""

FRACTION_TO_PERCENT = ("roe", "revenue_growth")
PERCENT_TO_RATIO = ("debt_to_equity",)


def normalize_quote(raw: dict) -> dict:
    out = dict(raw)
    for key in FRACTION_TO_PERCENT:
        v = out.get(key)
        if v is not None:
            out[key] = v * 100.0
    for key in PERCENT_TO_RATIO:
        v = out.get(key)
        if v is not None:
            out[key] = v / 100.0
    return out
```

- [ ] **Step 5: Verify green**

Run: `docker compose --profile test run --rm test pytest tests/test_units.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git status
git add api/units.py tests/test_units.py
git commit -m "api: add canonical unit normalisation for quote fields

ROE and revenue growth were displayed 100x too small and debt-to-equity
100x too large, because yfinance returns the first two as fractions and
the third as a percentage while all three were passed through raw."
```

### Task 6: Apply normalisation on both API surfaces

**Files:**
- Modify: `api/routers/consumer.py`
- Modify: `api/schemas.py`
- Test: `tests/test_api_contract.py`

**Interfaces:**
- Consumes: `normalize_quote` from Task 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_contract.py`:

```python
def test_quote_units_are_canonical_on_list_and_detail(client, seeded_stock_with_quote):
    """roe percent, revenue_growth percent, debt_to_equity ratio — on BOTH surfaces.

    The list and detail endpoints build their quote separately; this asserts
    they agree, which they previously did not.
    """
    symbol = seeded_stock_with_quote

    listed = client.get("/api/stocks", params={"q": symbol}).json()["items"][0]
    detail = client.get(f"/api/stocks/{symbol}").json()

    # seeded raw: roe=0.25, revenue_growth=0.30, debt_to_equity=150.0
    assert listed["quote"]["roe"] == pytest.approx(25.0)
    assert detail["quote"]["roe"] == pytest.approx(25.0)
    assert detail["quote"]["revenue_growth"] == pytest.approx(30.0)
    assert detail["quote"]["debt_to_equity"] == pytest.approx(1.5)
```

Add a `seeded_stock_with_quote` fixture to `tests/conftest.py` that creates one stock with a snapshot carrying exactly `roe=0.25`, `revenue_growth=0.30`, `debt_to_equity=150.0` and returns its symbol.

- [ ] **Step 2: Run it and watch it fail**

Run: `docker compose --profile test run --rm test pytest tests/test_api_contract.py::test_quote_units_are_canonical_on_list_and_detail -v`
Expected: FAIL — `roe` is 0.25, not 25.0

- [ ] **Step 3: Apply at both build sites**

In `api/routers/consumer.py`, add the import:

```python
from api.units import normalize_quote
```

Then wrap every place a `Quote` is constructed — the list builder and `stock_detail` — so the raw dict passes through `normalize_quote(...)` before becoming a `Quote`. Both sites must use it; the bug class here is precisely that the two surfaces diverge.

- [ ] **Step 4: Document the units on the schema**

In `api/schemas.py`, replace the `Quote` field declarations with documented ones:

```python
class Quote(BaseModel):
    current_price: float | None = None                      # rupees
    market_cap_cr: float | None = None                      # crore
    pe_ratio: float | None = None                           # ratio
    roe: float | None = None                                # percent, e.g. 39.4
    debt_to_equity: float | None = None                     # ratio, e.g. 0.098
    revenue_growth: float | None = None                     # percent, e.g. 18.5
    ipo_return_pct: float | None = None                     # percent
    data_quality: str | None = None
    as_of: datetime | None = None
```

- [ ] **Step 5: Verify green and check live**

```bash
docker compose --profile test run --rm test pytest tests/ -q
docker compose up -d --build api
sleep 15
curl -s "localhost:8000/api/stocks/IEX" | python3 -c "import json,sys; q=json.load(sys.stdin)['quote']; print('roe:', q['roe'], '| d/e:', q['debt_to_equity'], '| rev growth:', q['revenue_growth'])"
```

Expected: tests ≥189 passed; `roe: 39.4…` not `0.394`.

- [ ] **Step 6: Commit**

```bash
git status
git add api/routers/consumer.py api/schemas.py tests/test_api_contract.py tests/conftest.py
git commit -m "api: emit canonical units from list and detail quote builders"
```

### Task 7: Frontend renders the canonical units

**Files:**
- Modify: `web/app/(consumer)/stock/[symbol]/page.tsx`
- Modify: `web/app/(consumer)/page.tsx`

- [ ] **Step 1: Fix the D/E label and precision**

In `web/app/(consumer)/stock/[symbol]/page.tsx`, the D/E stat now receives a ratio. Render it with an explicit multiplier suffix so it cannot be misread:

```tsx
{num(quote.debt_to_equity, { decimals: 2, suffix: "x" })}
```

- [ ] **Step 2: Confirm no double-conversion anywhere**

Run: `grep -rn "roe\|revenue_growth\|debt_to_equity" web/app web/lib | grep -v types.ts`

Every hit must be a bare `pct(...)`, `num(...)`, or a passthrough. If any site multiplies or divides by 100, delete that arithmetic — the API now owns the scale. Leaving one in place reintroduces the bug in the opposite direction.

- [ ] **Step 3: Build and verify visually**

```bash
cd web && npm run build && cd ..
docker compose up -d --build web
sleep 20
```

Open `http://localhost:3000/stock/IEX`. Expected: ROE reads ~39.4%, not 0.4%. Open Discovery and confirm the "Rev Δ" column shows values like +18.5%, not +0.2%.

- [ ] **Step 4: Commit**

```bash
git status
git add web/app/\(consumer\)/stock/\[symbol\]/page.tsx web/app/\(consumer\)/page.tsx
git commit -m "web: render canonical quote units, label D/E as a multiple"
```

---

# PHASE 2 — Freshness and provenance

### Task 8: Personas see today's price, not a stale snapshot's

**Files:**
- Modify: `engine/analysis/prompt.py`
- Test: `tests/test_analysis_engine.py`

**Interfaces:**
- Consumes: `daily_prices` rows via a session passed into prompt building.

**Why:** `compute_content_hash` strips quote fields and `add_snapshot` is `ON CONFLICT DO NOTHING`, so when fundamentals are unchanged the freshly-fetched price is discarded and the snapshot keeps its original. Measured: 183 analyses ran with >5% price drift, 72 with >20%, worst 700%. Those personas valued a company at a price it no longer traded at.

- [ ] **Step 1: Write the failing test**

```python
def test_prompt_uses_latest_daily_close_not_stale_snapshot_price(db_session):
    """A snapshot pinned at 100 must not be quoted to the persona when the
    latest bar says 250. Measured prod drift reached 700%."""
    from engine.analysis.prompt import build_user_prompt

    stock = _make_stock(db_session, symbol="STALEPX")
    snap = _make_snapshot(db_session, stock, info={"currentPrice": 100.0, "marketCap": 1e9})
    _add_bars(db_session, stock, [(date(2026, 7, 17), 250.0)])
    db_session.commit()

    prompt = build_user_prompt(db_session, stock, snap)

    assert "250" in prompt
    assert "Current Price: 100" not in prompt
```

- [ ] **Step 2: Run it and watch it fail**

Run: `docker compose --profile test run --rm test pytest tests/test_analysis_engine.py::test_prompt_uses_latest_daily_close_not_stale_snapshot_price -v`
Expected: FAIL — prompt contains the stale 100.

- [ ] **Step 3: Implement**

In `engine/analysis/prompt.py`, add:

```python
def latest_close(session, stock_id: int) -> float | None:
    """Most recent stored close. daily_prices is refreshed nightly; snapshot
    quote fields are not, because the content hash deliberately excludes them.
    """
    from sqlalchemy import select
    from db.models import DailyPrice

    return session.scalar(
        select(DailyPrice.close)
        .where(DailyPrice.stock_id == stock_id)
        .order_by(DailyPrice.date.desc())
        .limit(1)
    )
```

In `build_user_prompt`, resolve the price once and use it for every price-derived figure in the prompt:

```python
    px = latest_close(session, stock.id) or _num(snap.info.get("currentPrice"))
```

Ensure the screener block (`format_screener`) does not emit a second, contradictory "Current Price" line — the prompt must carry exactly one price.

- [ ] **Step 4: Verify green**

Run: `docker compose --profile test run --rm test pytest tests/test_analysis_engine.py -v`
Expected: PASS

- [ ] **Step 5: Re-measure the drift the fix eliminates**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
WITH a AS (SELECT an.id, an.stock_id, an.analyzed_at::date d,
  COALESCE(ss.current_price,(ss.info->>'currentPrice')::float) seen
  FROM analyses an JOIN stock_snapshots ss ON ss.id=an.snapshot_id)
SELECT count(*) FILTER (WHERE abs(a.seen-dp.close)/NULLIF(dp.close,0)>0.05) AS needs_rerun
FROM a JOIN LATERAL (SELECT close FROM daily_prices d
  WHERE d.stock_id=a.stock_id AND d.date<=a.d ORDER BY d.date DESC LIMIT 1) dp ON true
WHERE a.seen IS NOT NULL;"
```

Record the count. These analyses are the re-run list for Phase 4.

- [ ] **Step 6: Commit**

```bash
git status
git add engine/analysis/prompt.py tests/test_analysis_engine.py
git commit -m "analysis: quote personas the latest close, not the snapshot's frozen price

The content hash excludes quote fields so unchanged fundamentals keep an
old snapshot and its original price. 183 analyses ran at >5% drift from
the real close, 72 at >20%, worst 700% — valuation scored at one price
while the backtest entered at another."
```

### Task 9: Corporate actions cannot fabricate a price cliff

**Files:**
- Modify: `engine/repo.py` (`upsert_daily_prices`)
- Test: `tests/test_daily_prices.py`

**Why:** yfinance 1.2.0 defaults `auto_adjust=True`, so history is retroactively rebased after a split. `upsert_daily_prices` is `ON CONFLICT DO NOTHING`, so the rebased bars are discarded and only post-split bars land — leaving a cliff that never happened. **Verified not yet fired** (zero adjacent moves outside [-35%, +55%] across all bars 2026-04-15→07-17); this is preventative.

- [ ] **Step 1: Write the failing test**

```python
def test_reingesting_split_adjusted_history_corrects_stored_bars(db_session):
    """After a 1:10 split Yahoo rebases history. Append-only storage keeps the
    pre-split closes and creates a -90% cliff that never happened."""
    from engine.repo import upsert_daily_prices

    stock = _make_stock(db_session, symbol="SPLITCO")
    upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": 1000, "high": 1010, "low": 990, "close": 1000, "volume": 1},
    ])
    db_session.commit()

    # Same date, rebased 1:10 by the provider.
    upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
    ])
    db_session.commit()

    bar = db_session.scalar(select(DailyPrice).where(
        DailyPrice.stock_id == stock.id, DailyPrice.date == date(2026, 1, 5)))
    assert bar.close == 100
```

- [ ] **Step 2: Run it and watch it fail**

Expected: FAIL — `close` is still 1000 because the conflict was ignored.

- [ ] **Step 3: Implement**

Replace the `on_conflict_do_nothing` in `upsert_daily_prices` with a conditional update. Keep the return semantics (count of rows written):

```python
    stmt = pg_insert(DailyPrice).values(payload)
    stmt = (
        stmt.on_conflict_do_update(
            index_elements=["stock_id", "date"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
            # Only rewrite when the provider actually changed the bar — a corporate
            # action rebase. Avoids churning every row on every nightly refresh.
            where=DailyPrice.close.op("IS DISTINCT FROM")(stmt.excluded.close),
        )
        .returning(DailyPrice.id)
    )
    return len(session.execute(stmt).fetchall())
```

Update the docstring: the function is no longer append-only.

- [ ] **Step 4: Verify green, including the idempotency test**

Run: `docker compose --profile test run --rm test pytest tests/test_daily_prices.py -v`
Expected: PASS. If an existing test asserts "re-inserting the same bars returns 0", it still passes — identical closes are excluded by the `where` clause.

- [ ] **Step 5: Add a cliff detector to the DQ audit**

In `engine/quality/dimensions.py`, extend `_prices` so a series containing an adjacent-day move outside `[-35%, +55%]` is flagged `price_cliff` at severity `warn`. Bar count and recency alone cannot see this class of corruption.

- [ ] **Step 6: Commit**

```bash
git status
git add engine/repo.py engine/quality/dimensions.py tests/test_daily_prices.py
git commit -m "prices: correct bars rebased by corporate actions instead of ignoring them

yfinance auto_adjust=True rebases history after a split; ON CONFLICT DO
NOTHING discarded the rebased bars and kept pre-split closes, which would
render as a one-day -90% return that never happened. Not yet fired in
prod — no adjacent move outside [-35%,+55%] exists today."
```

### Task 10: Imputed data is distinguishable from measured data

**Files:**
- Create: `alembic/versions/<rev>_add_field_provenance.py`
- Modify: `db/models.py`, `engine/quality/gapfill.py`, `engine/quality/dimensions.py`
- Test: `tests/test_data_quality.py`

**Why:** gapfill writes screener-breadcrumb `sector`/`industry` and AMFI `isin` into the same columns as yfinance-measured values with no source marker. `_identity` then scores quality on non-null-ness — so gapfill imputes, the grade improves, and the audit congratulates itself. It is structurally incapable of falsifying its own fills. Imputed sector also reaches the LLM prompt as if measured.

- [ ] **Step 1: Generate the migration**

```bash
docker compose exec api alembic revision -m "add field provenance"
```

Fill in:

```python
def upgrade():
    op.add_column("stocks", sa.Column("sector_source", sa.String(16), nullable=True))
    op.add_column("stocks", sa.Column("industry_source", sa.String(16), nullable=True))
    op.add_column("stocks", sa.Column("isin_source", sa.String(16), nullable=True))
    # Existing non-null values predate provenance tracking. Mark them unknown
    # rather than claiming they were measured.
    op.execute("UPDATE stocks SET sector_source='unknown' WHERE sector IS NOT NULL")
    op.execute("UPDATE stocks SET industry_source='unknown' WHERE industry IS NOT NULL")
    op.execute("UPDATE stocks SET isin_source='unknown' WHERE isin IS NOT NULL")


def downgrade():
    op.drop_column("stocks", "isin_source")
    op.drop_column("stocks", "industry_source")
    op.drop_column("stocks", "sector_source")
```

Add the three columns to `Stock` in `db/models.py` so `tests/test_migrations.py` parity holds.

- [ ] **Step 2: Write the failing test**

```python
def test_gapfill_stamps_provenance_on_imputed_sector(db_session):
    """An imputed sector must be distinguishable from a measured one."""
    stock = _make_stock(db_session, symbol="IMPUTED", sector=None)
    _add_screener_classification(db_session, stock, ["Materials", "Chemicals", "Commodity Chemicals"])
    db_session.commit()

    gapfill.fill_identity(db_session)
    db_session.refresh(stock)

    assert stock.sector == "Materials"
    assert stock.sector_source == "screener"


def test_imputed_identity_scores_partial_credit_not_full(db_session):
    """Gapfill must not be able to raise a stock's grade to the same level as
    measured data — otherwise the audit validates its own guesses."""
    measured = _make_stock(db_session, symbol="MEASURED", sector="Technology", sector_source="yfinance")
    imputed = _make_stock(db_session, symbol="GUESSED", sector="Technology", sector_source="screener")
    db_session.commit()

    assert dimensions.identity_score(imputed) < dimensions.identity_score(measured)
```

- [ ] **Step 3: Run and watch both fail**

Expected: FAIL — `sector_source` unset, and `identity_score` treats both identically.

- [ ] **Step 4: Implement**

In `engine/quality/gapfill.py`, every write of `sector`/`industry`/`isin` sets its `_source` alongside — `"screener"` for classification-derived, `"amfi"` for the AMFI map, `"yfinance"` where the value came from `info`.

In `engine/quality/dimensions.py`, add `identity_score(stock)` giving full credit for `yfinance`/`amfi` sources and partial credit (0.5) for `screener`/`unknown`, and route `_identity` through it.

- [ ] **Step 5: Add the AMFI collision guard**

`_amfi_identity_map` keys NSE and BSE symbols into one flat dict with last-wins overwrite, so a cross-namespace collision assigns the wrong company's ISIN — on the field documented as the universal cross-source identity key. Before accepting an AMFI ISIN, require the AMFI row's company name to agree with `stock.company_name` (case-insensitive, punctuation-stripped, first-two-tokens match). The name is already parsed at `amfi.py:137` and currently discarded. On disagreement, skip the fill and count it in the job stats.

- [ ] **Step 6: Verify green**

Run: `docker compose --profile test run --rm test pytest tests/test_data_quality.py tests/test_migrations.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git status
git add alembic/versions db/models.py engine/quality/gapfill.py engine/quality/dimensions.py tests/test_data_quality.py
git commit -m "quality: track field provenance so imputed values cannot pose as measured

Gapfill wrote screener-breadcrumb sector/industry and AMFI ISIN into the
same columns as yfinance values, and _identity scored quality on
non-null-ness — so imputing raised the grade and the audit validated its
own guesses. Imputed sector also reached the LLM prompt as fact."
```

### Task 11: Verify the ticker belongs to the company

**Files:**
- Modify: `engine/ingest/yf_refresh.py`
- Test: `tests/test_screener_identity.py`

**Why:** the screener URL slug is assumed to be the NSE ticker and `.NS` is appended with no verification. Nothing ever compares yfinance's `longName` against `stock.company_name` — `longName` appears nowhere in `engine/`. A slug/ticker mismatch silently analyses a different listed company and publishes the verdict under this stock's name. This is the single worst silent corruption available: every downstream number is internally consistent and about the wrong business.

- [ ] **Step 1: Write the failing test**

```python
def test_refresh_refuses_to_promote_on_company_name_mismatch(db_session):
    stock = _make_stock(db_session, symbol="ACME", company_name="Acme Industries Ltd")
    payload = {"info": {"longName": "Zenith Textiles Limited", "currentPrice": 100.0}}

    result = yf_refresh.promote(db_session, stock, payload)

    assert result["status"] == "identity_mismatch"
    assert stock.status != "active"
```

- [ ] **Step 2: Run and watch it fail**

Expected: FAIL — promotion succeeds; no identity check exists.

- [ ] **Step 3: Implement**

Add to `engine/ingest/yf_refresh.py`:

```python
def names_agree(a: str | None, b: str | None) -> bool:
    """Loose company-name agreement. Exchange and provider names differ in
    suffixes and punctuation, so compare the first two significant tokens.
    """
    if not a or not b:
        return True  # cannot disprove; do not block on missing data
    drop = {"ltd", "limited", "the", "india", "industries", "company", "co", "pvt", "private"}
    def toks(s):
        parts = [t for t in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split() if t not in drop]
        return parts[:2]
    ta, tb = toks(a), toks(b)
    return bool(ta) and bool(tb) and ta[0] == tb[0]
```

In the promote path, when `names_agree(info.get("longName"), stock.company_name)` is false: do not promote, record `identity_mismatch` in the job stats, and `notify_safe` once. Never overwrite the stock's stored data with the mismatched payload.

- [ ] **Step 4: Audit the existing corpus for mismatches**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
SELECT s.symbol, s.company_name, ss.info->>'longName' AS yf_name
FROM stocks s JOIN LATERAL (
  SELECT info FROM stock_snapshots WHERE stock_id=s.id ORDER BY captured_at DESC LIMIT 1
) ss ON true
WHERE ss.info->>'longName' IS NOT NULL
  AND lower(split_part(s.company_name,' ',1)) <> lower(split_part(ss.info->>'longName',' ',1))
LIMIT 40;"
```

Review the output by hand. Some rows will be benign (abbreviations, renames). Any genuine mismatch means that stock's entire analysis set is about the wrong company and must be purged, not just re-run. Record the list.

- [ ] **Step 5: Verify green and commit**

```bash
docker compose --profile test run --rm test pytest tests/test_screener_identity.py -v
git status
git add engine/ingest/yf_refresh.py tests/test_screener_identity.py
git commit -m "ingest: refuse to promote a payload whose company name disagrees

The screener slug was assumed to be the NSE ticker with no verification,
so a slug/ticker mismatch would analyse a different listed company and
publish the verdict under this stock's name."
```

### Task 12: A batch that accomplished nothing is not a success

**Files:**
- Modify: `engine/repo.py` (`_item_error_ratio`)
- Modify: `engine/ingest/screener_enrich.py`
- Modify: `engine/ingest/yf_refresh.py`
- Test: `tests/test_observability.py`

**Why:** `_item_error_ratio` keys only off `stats["error"]`. A screener 429 ban returns `no_data` for all 200 stocks, so the job records `success` and nobody is paged — then gapfill re-queues the same 200 the next night, forever, all green. Same for a yfinance outage via `soft_fail`.

- [ ] **Step 1: Write the failing test**

```python
def test_batch_of_all_no_data_is_recorded_as_degraded_not_success(db_session):
    """A screener IP ban returns no_data for every stock. That is a failed
    batch, not a successful one."""
    stats = {"processed": 200, "no_data": 200}
    assert repo._item_error_ratio(stats) == 1.0


def test_batch_of_all_soft_fail_is_recorded_as_degraded(db_session):
    stats = {"processed": 150, "soft_fail": 150}
    assert repo._item_error_ratio(stats) == 1.0


def test_healthy_batch_still_reports_zero(db_session):
    assert repo._item_error_ratio({"processed": 100, "ok": 100}) == 0.0
```

- [ ] **Step 2: Run and watch it fail**

Expected: FAIL — returns `None` for the first two, because neither key is `error`.

- [ ] **Step 3: Implement**

In `engine/repo.py`, treat every non-productive outcome as a failure for the ratio:

```python
FAILURE_KEYS = ("error", "no_data", "soft_fail")


def _item_error_ratio(stats: dict) -> float | None:
    processed = stats.get("processed")
    if not processed:
        return None
    failed = sum(stats.get(k) or 0 for k in FAILURE_KEYS)
    return failed / processed
```

- [ ] **Step 4: Add source-level circuit breakers**

In `engine/ingest/screener_enrich.py`: on an HTTP 429 or 403, abort the remaining batch immediately rather than continuing to hammer a rate-limiting host, record `rate_limited`, and notify once.

In `engine/ingest/yf_refresh.py`: before parking any stock, check the batch. If more than 50% soft-failed, the source is down, not the stocks — skip all parking, notify once, and return. Without this a yfinance outage parks the entire universe and emits 2,657 push notifications.

- [ ] **Step 5: Verify green and commit**

```bash
docker compose --profile test run --rm test pytest tests/ -q
git status
git add engine/repo.py engine/ingest/screener_enrich.py engine/ingest/yf_refresh.py tests/test_observability.py
git commit -m "ops: count no_data and soft_fail as batch failures; add source circuit breakers

A screener ban returning no_data for all 200 stocks recorded as success
with no alert, and gapfill re-queued the same 200 nightly forever. A
yfinance outage would have parked the entire universe and sent one push
notification per stock."
```

---

# PHASE 3 — Stop the corpus being polluted

### Task 13: Transient failures never dead-letter

**Files:**
- Create: `engine/analysis/errors.py`
- Modify: `engine/analysis/engine.py`, `engine/scheduler.py`
- Test: `tests/test_analysis_deadletter.py`

**Why:** no retryable/permanent classification exists anywhere in the analysis path. Every failure — CLI timeout, usage limit, empty stdout — increments the same counter, and three on the same `data_hash` dead-letters the pair. Because `data_hash` excludes volatile price fields, a stable company will not mint a new hash for months, so **a three-hour quota outage permanently removes stocks from the council** — non-randomly, biasing the universe. Compounding: `analyses_done_today()` counts successes, so on a rate-limited day the cap never advances and all 24 ticks fire.

**This task must land before any schema tightening.** Tightening validation without it converts stricter rejection into permanent coverage loss.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from engine.analysis.errors import is_transient

TRANSIENT = [
    "Claude AI usage limit reached|resets 09:00",
    "rate limit exceeded",
    "CLI timeout after 300s",
    "Error: connection reset by peer",
    "overloaded_error",
    "",
]
PERMANENT = [
    "contract violation: score out of range",
    "invalid json schema",
    "CLI exit 2: unknown flag --nope",
]


@pytest.mark.parametrize("msg", TRANSIENT)
def test_transient_errors_are_classified_transient(msg):
    assert is_transient(msg) is True


@pytest.mark.parametrize("msg", PERMANENT)
def test_permanent_errors_are_classified_permanent(msg):
    assert is_transient(msg) is False


def test_usage_limit_does_not_increment_dead_letter_counter(db_session):
    """Three quota failures must not permanently drop a pair from coverage."""
    stock = _make_stock(db_session, symbol="QUOTA")
    for _ in range(3):
        engine.record_failure(db_session, stock, "warren_buffett",
                              "hash1", "Claude AI usage limit reached")
    db_session.commit()

    assert engine.is_dead_lettered(db_session, stock.id, "warren_buffett", "hash1") is False
```

- [ ] **Step 2: Run and watch it fail**

Expected: FAIL — `ModuleNotFoundError: engine.analysis.errors`

- [ ] **Step 3: Implement the classifier**

Create `engine/analysis/errors.py`:

```python
"""Transient vs permanent classification for analysis failures.

A permanent failure is a property of this (stock, persona, data_hash) —
malformed output, a contract violation, a bad flag. Retrying cannot help,
so the dead-letter counter should advance.

A transient failure is a property of the WORLD at that moment — quota
exhausted, network reset, timeout. Retrying will help. Counting these
toward the dead letter permanently removes stocks from the council after
a few bad hours, which is exactly what happened in production.
"""
import re

TRANSIENT_PATTERNS = (
    r"usage limit",
    r"rate.?limit",
    r"timeout",
    r"timed out",
    r"connection (reset|refused|closed)",
    r"overloaded",
    r"temporarily unavailable",
    r"5\d\d\b",
)


def is_transient(error: str | None) -> bool:
    # An empty/absent error tells us nothing about permanence. Treat the
    # unknown as transient: over-retrying costs quota, but wrongly
    # dead-lettering costs coverage silently and forever.
    if not error:
        return True
    low = error.lower()
    return any(re.search(p, low) for p in TRANSIENT_PATTERNS)
```

- [ ] **Step 4: Wire it into the failure path**

In `engine/analysis/engine.py`, where `record_analysis_failure` is called: still record the failure row for observability, but only advance the dead-letter counter when `is_transient(err)` is false.

- [ ] **Step 5: Fix the daily cap to count attempts**

In `engine/scheduler.py`, change `analyses_done_today()` to count attempts (successes + recorded failures) rather than `Analysis` rows. As written, a rate-limited day never advances the cap and all 24 ticks fire, burning quota.

- [ ] **Step 6: Correct the test that blesses the bug**

`tests/test_analysis_parallel.py:320` (`test_backend_string_error_meta_recorded_verbatim`) feeds a usage-limit string and asserts all 10 personas get failure rows. Keep the verbatim-meta assertion — that part is correct and valuable — but add an assertion that none of the ten advanced the dead-letter counter. A plan-level rate limit is a global outage, not a per-pair fact.

- [ ] **Step 7: Purge wrongly dead-lettered pairs**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
SELECT count(*) FROM analysis_failures
WHERE error ILIKE '%usage limit%' OR error ILIKE '%rate limit%' OR error ILIKE '%timeout%';"
```

Delete those rows so the affected pairs re-enter `find_work`. Record the count — it is coverage you are getting back.

- [ ] **Step 8: Verify green and commit**

```bash
docker compose --profile test run --rm test pytest tests/ -q
git status
git add engine/analysis/errors.py engine/analysis/engine.py engine/scheduler.py tests/test_analysis_deadletter.py tests/test_analysis_parallel.py
git commit -m "analysis: never dead-letter transient failures

No retryable/permanent classification existed, so a quota outage of a few
hours permanently removed stocks from the council until their fundamentals
changed — months, for a stable company. The daily cap also counted only
successes, so a rate-limited day fired all 24 ticks."
```

### Task 14: Thin coverage cannot masquerade as confidence

**Files:**
- Modify: `engine/repo.py`, `engine/scoring/confidence.py`
- Test: `tests/test_confidence.py`, `tests/test_scoring.py`

**Why, measured:** 72 composites sit at coverage 1. `confidence.py:89` hardcodes `stderr_eff = 0.0` when `n_present <= 1`, so a single-persona stock is credited with **zero uncertainty**; LCB is the default Discovery sort, so those stocks rank top. Meanwhile `recompute_scores_for_stock` writes a `CompositeScoreHistory` row after every individual persona success, and the backtest takes the *earliest* row per stock via `DISTINCT ON` — so 13% of the cohort is single-persona and never self-heals.

- [ ] **Step 1: Write the failing tests**

```python
def test_single_axis_stock_is_not_credited_with_zero_uncertainty():
    """n=1 currently yields stderr_eff 0.0 — maximum confidence from minimum
    information, the exact inversion of the truth."""
    one = compute_confidence({"warren_buffett": 9}, 90.0)
    assert one["stderr_eff"] > 0.0


def test_full_council_outranks_a_thin_high_scorer_across_the_range():
    """The old test pinned this at a 4-point gap, inside the coverage penalty,
    so it passed while the invariant was false. Assert it where it matters."""
    thin = compute_confidence({"warren_buffett": 9}, 90.0)
    broad = compute_confidence({s: 8 for s in ALL_PERSONAS}, 80.0)
    assert broad["lcb"] > thin["lcb"]


def test_tier_is_not_high_for_a_unanimously_bearish_stock():
    """abs(composite-50)>=15 is direction-blind and the population mean is 32.7,
    so the average stock was auto-promoted to 'high' and rendered in green."""
    bearish = compute_confidence({s: 2 for s in ALL_PERSONAS}, 20.0)
    assert bearish["tier"] != "high"


def test_history_row_is_not_written_below_minimum_coverage(db_session):
    stock = _make_stock(db_session, symbol="THIN")
    _save_analysis(db_session, stock, "warren_buffett", score=7)
    repo.recompute_scores_for_stock(db_session, stock.id)
    db_session.commit()

    rows = db_session.scalars(select(CompositeScoreHistory).where(
        CompositeScoreHistory.stock_id == stock.id)).all()
    assert rows == []
```

- [ ] **Step 2: Run and watch all four fail**

Run: `docker compose --profile test run --rm test pytest tests/test_confidence.py -v -k "single_axis or outranks or bearish or minimum_coverage"`
Expected: 4 FAILED

- [ ] **Step 3: Give n=1 an honest uncertainty floor**

In `engine/scoring/confidence.py`:

```python
# A single axis tells us nothing about dispersion. Charging 0.0 credits the
# thinnest possible evidence with perfect confidence; use the population-scale
# prior instead so one opinion is never more certain than a full council.
SOLO_STDERR_PRIOR = 15.0

stderr_eff = (round(statistics.pstdev(vals) / (n_present ** 0.5), 3)
              if n_present > 1 else SOLO_STDERR_PRIOR)
```

- [ ] **Step 4: Make the tier direction-aware**

Replace the magnitude computation so `high` requires conviction *and* a bullish direction, and add a symmetric bearish tier:

```python
    signed = composite - NEUTRAL
    magnitude = abs(signed)

    if not full_coverage:
        tier = "provisional"
    elif not concordant:
        tier = "mixed"
    elif magnitude < MAG_THRESHOLD:
        tier = "moderate"
    elif signed > 0:
        tier = "high"
    else:
        tier = "high_bearish"
```

Add `high_bearish` to the API enum in `api/schemas.py` and give it a distinct, non-green chip in `web/components/TierChip.tsx` plus a caption in `TIER_CAPTION` — `high` currently has no caption at all.

- [ ] **Step 5: Gate the history write**

In `engine/repo.py`:

```python
MIN_COVERAGE_FOR_HISTORY = 7

# A history row is point-in-time research evidence and the backtest takes the
# EARLIEST row per stock. Writing one after persona #1 lands permanently
# enters that stock into the study on a single opinion.
if coverage >= MIN_COVERAGE_FOR_HISTORY:
    _write_history_row(...)
```

- [ ] **Step 6: Verify green**

Run: `docker compose --profile test run --rm test pytest tests/ -q`
Expected: ≥189 passed. `tests/test_confidence.py:76-79` will now fail if it still asserts the old bounded invariant — rewrite it to the Step 1 version rather than deleting it.

- [ ] **Step 7: Backfill — purge the contaminated history rows**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
SELECT count(*) FROM composite_score_history WHERE analysis_coverage < 7;"
```

Delete them so the backtest cohort re-forms from adequately-covered rows only. Then recompute confidence for every composite so the new `stderr_eff` and tier apply:

```bash
docker compose exec scheduler python -m engine.run score --all
```

- [ ] **Step 8: Commit**

```bash
git status
git add engine/scoring/confidence.py engine/repo.py api/schemas.py web/components/TierChip.tsx tests/test_confidence.py tests/test_scoring.py
git commit -m "scoring: thin coverage no longer outranks a full council

n=1 was credited with stderr_eff 0.0 — perfect confidence from one
opinion — while LCB is the default Discovery sort, so single-persona
stocks ranked top. The tier gate used abs(composite-50), which on a
population centred at 32.7 auto-promoted the worst names to a green
'high' badge: measured mean composite was 25.6 for 'high' and 46.2 for
'mixed'. History rows written after persona #1 also entered 13% of the
backtest cohort on a single opinion."
```

### Task 15: The consensus reflects the council

**Files:**
- Modify: `engine/repo.py`, `src/personas.py`, `engine/analysis/contract.py`
- Test: `tests/test_scoring.py`, `tests/test_analysis_contract.py`

**Why:** `consensus_recommendation` is a pure threshold on composite and ignores `recommendation_counts` entirely. Ten personas each returning score 6 / HOLD yields composite 60.0 → displayed **"BUY"** with `{"HOLD": 10}` in the adjacent field. Root cause: **nothing in the prompt stack ever defines BUY/HOLD/AVOID** — one occurrence across `src/personas.py`, the enum itself, described as "Investment recommendation".

- [ ] **Step 1: Write the failing test**

```python
def test_consensus_does_not_contradict_a_unanimous_council(db_session):
    """Ten HOLDs at score 6 produce composite 60.0, which the threshold rule
    labelled BUY while recommendation_counts said {'HOLD': 10}."""
    stock = _make_stock(db_session, symbol="UNANIMOUS")
    for slug in ALL_PERSONAS:
        _save_analysis(db_session, stock, slug, score=6, recommendation="HOLD")
    repo.recompute_scores_for_stock(db_session, stock.id)
    db_session.commit()

    cs = _composite(db_session, stock.id)
    assert cs.consensus_recommendation == "HOLD"
    assert cs.recommendation_counts == {"HOLD": 10}
```

- [ ] **Step 2: Run and watch it fail**

Expected: FAIL — `consensus_recommendation == "BUY"`

- [ ] **Step 3: Derive consensus from the votes**

In `engine/repo.py`, replace the threshold rule with the council's modal vote, breaking ties toward caution (`AVOID` > `HOLD` > `BUY`), and keep the composite as the tiebreak only when counts are empty:

```python
def _consensus_from_votes(recs: dict[str, int], composite: float) -> str:
    if not recs:
        return "BUY" if composite >= 60 else "HOLD" if composite >= 40 else "AVOID"
    # Highest count wins; ties break toward caution. This is a research tool,
    # not a sales sheet, so AVOID beats HOLD beats BUY at equal counts.
    order = {"AVOID": 0, "HOLD": 1, "BUY": 2}
    return min(recs, key=lambda r: (-recs[r], order[r]))
```

- [ ] **Step 4: Define the terms in the prompt**

In `src/personas.py`, extend `ANALYSIS_PROMPT_TEMPLATE` with an explicit mapping, and give the schema's `recommendation` field a real description plus `minimum`/`maximum` on `score`:

```
RECOMMENDATION — must be consistent with your score:
  BUY    score >= 7  — you would deploy capital at today's price
  HOLD   score 4-6   — a business you respect, but not at this price
  AVOID  score <= 3  — you would not own this at any reasonable price
```

- [ ] **Step 5: Add the joint contract check**

In `engine/analysis/contract.py`, reject a result whose `recommendation` contradicts its `score` band. **Task 13 must already be merged** — otherwise this new rejection path dead-letters pairs permanently.

- [ ] **Step 6: Verify green, recompute, commit**

```bash
docker compose --profile test run --rm test pytest tests/ -q
docker compose exec scheduler python -m engine.run score --all
git status
git add engine/repo.py src/personas.py engine/analysis/contract.py tests/test_scoring.py tests/test_analysis_contract.py
git commit -m "scoring: derive consensus from persona votes, define BUY/HOLD/AVOID

consensus_recommendation was a pure composite threshold that ignored the
ten personas' actual votes, so a unanimous HOLD council could display BUY.
The prompt never defined the three terms at all — the enum description was
the tautology 'Investment recommendation'."
```

---

# PHASE 4 — Make the book comparable

This is the phase that recovers the value of the existing spend. Do not start it until Phases 1-3 are merged; re-running into an unfixed pipeline wastes the quota twice.

### Task 16: Record what produced each composite

**Files:**
- Create: `alembic/versions/<rev>_add_composite_provenance.py`
- Modify: `db/models.py`, `engine/repo.py`
- Test: `tests/test_scoring.py`

**Why:** neither `CompositeScore` nor `CompositeScoreHistory` records `prompt_version` or `model` — only `factor_version`, which is the scoring-axis version, a different thing. 60 stocks have mixed prompt versions and 12 have mixed models inside a single composite, and nothing downstream can detect or stratify it.

- [ ] **Step 1: Migration**

Add `prompt_versions` (JSONB) and `models_used` (JSONB) to both `composite_scores` and `composite_score_history`, each holding the count per value across contributing analyses, e.g. `{"v1": 6, "v4": 4}`. Backfill from `analyses` in the same migration. Add matching columns to `db/models.py`.

- [ ] **Step 2: Populate on recompute**

In `recompute_scores_for_stock`, aggregate the contributing analyses' `prompt_version` and `model` into those dicts and persist them.

- [ ] **Step 3: Expose homogeneity**

Add a boolean `is_homogeneous` to the API composite payload — true when both dicts have exactly one key. Surface it on the stock detail page so a mixed-provenance score is visibly marked.

- [ ] **Step 4: Verify and commit**

```bash
docker compose --profile test run --rm test pytest tests/ -q
git status
git add alembic/versions db/models.py engine/repo.py api/schemas.py api/routers/consumer.py tests/test_scoring.py
git commit -m "scoring: record prompt versions and models behind each composite"
```

### Task 17: Re-analyse the non-comparable tail

**Files:**
- Create: `scripts/rescore_campaign.py`

**Why, measured:** `opus`-dominant stocks average composite **24.1** while `sonnet`-dominant average **33.9** — a ~10-point gap on a population SD of ~11, driven by the model rather than the business. 86 stocks are affected. They cluster at the bottom of every ranking regardless of quality, which alone can flatten or invert the IC. Under a one-company-one-model policy the model becomes a per-stock variable, which is the worst case for cross-sectional ranking.

- [ ] **Step 1: Build the target list, highest-impact first**

```bash
docker exec ipo_postgres psql -U ipo -d ipo -c "
COPY (
  WITH dm AS (SELECT stock_id, mode() WITHIN GROUP (ORDER BY model) m
              FROM analyses WHERE score IS NOT NULL GROUP BY stock_id)
  SELECT s.symbol FROM stocks s JOIN dm ON dm.stock_id=s.id
  WHERE dm.m = 'opus' ORDER BY s.symbol
) TO STDOUT;" > /tmp/rerun_opus.txt
wc -l /tmp/rerun_opus.txt
```

Expected: ~86 symbols. This is tier 1. Tier 2 is the 60 mixed-prompt-version stocks; tier 3 is the drift list from Task 8 Step 5.

- [ ] **Step 2: Fix the target model and prompt version**

Every re-run uses `claude-fable-5` and the current prompt version — no exceptions. A campaign that mixes models recreates the problem it exists to solve.

- [ ] **Step 3: Probe quota before committing to a batch**

Max-plan windows cap around 300-360 CLI pairs. 86 stocks × 10 personas = 860 pairs ≈ 3 windows. Run one stock first and confirm it completes before queuing the batch.

```bash
docker compose exec scheduler python -m engine.run analyze --symbols $(head -1 /tmp/rerun_opus.txt) --model claude-fable-5 --workers 10 --force
```

- [ ] **Step 4: Run tier 1 in quota-sized chunks**

```bash
split -l 30 /tmp/rerun_opus.txt /tmp/chunk_
for f in /tmp/chunk_*; do
  docker compose exec scheduler python -m engine.run analyze \
    --symbols "$(paste -sd, "$f")" --model claude-fable-5 --workers 10 --force
  docker exec ipo_postgres psql -U ipo -d ipo -c \
    "SELECT count(*) FROM analysis_failures WHERE created_at > now() - interval '1 hour';"
done
```

Between chunks, confirm the failure count is not climbing. If it is, stop — Task 13's classifier should prevent dead-lettering, but a rising count means something else is wrong.

- [ ] **Step 5: Re-measure the model effect**

Re-run the dominant-model query from the Measured Baseline table. Expected: the `opus` row is gone and the remaining group means sit within ~2 points of each other. If a ~10-point gap persists, the cause is not the model and the campaign should stop until that is understood.

- [ ] **Step 6: Recompute and commit the driver**

```bash
docker compose exec scheduler python -m engine.run score --all
git status
git add scripts/rescore_campaign.py
git commit -m "scripts: targeted re-analysis driver for model homogenisation"
```

---

## Deferred to a Follow-Up Plan

Real findings, deliberately out of scope here because they do not affect data correctness. Do not lose them:

- **Backtest measurement honesty** — the IC is reported as a bare float with no `n` and no CI, and the UI colour-codes it by sign. Measured: IC(21d) −0.0973, n=354, bootstrap 95% CI [−0.199, +0.008], which **crosses zero**. All cohort members share one April 2026 window, so the study has one time-series observation. Until this is fixed the study cannot support any conclusion about signal, in either direction.
- **`entry_idx is None` conflates two conditions** — "no price data at all" (181 SME shells) and "price series stale relative to scoring date" (35 liquid names including ABSLAMC, APTUS, ATHERENERG, BBTC, DEEPAKNTR). The entire July cohort is 0-of-120 priced.
- **Stale benchmark silently converts raw return into excess** (`study.py:229-231`) — when the benchmark ends on or before entry, the code asserts the benchmark moved 0%. Two of three configured benchmarks are frozen.
- **`K_DISP` is unconstrained by any test** — setting it to `0.0` or `1000.0` leaves all 189 green, because every test touching `lcb` has `stderr_eff == 0.0`. Task 14 changes this; add a test that pins it.
- **Spearman falls through on mismatched lengths** (`study.py:53-58`) and returns a non-correlation instead of raising.
- **Quintile bucketing has zero test coverage** — `grep -rn "quintile" tests/` returns nothing.
- **Admin GET endpoints are unauthenticated** and `/api/admin/jobs` returns raw error strings including paths and tracebacks.
- **Persona weighting re-ranks only the loaded 50 rows** client-side, and the LCB column and TierChip continue showing full-council values beside a subset composite.
- **Composite over-weights the redundant core cluster 4×** — a flat persona mean gives core 40% / value 30% / growth 20% / independent 10%, contradicting `confidence.py`'s own stated premise that the four core personas are ~one effective vote.
- **`--force` overwrites the historical verdict in place**, destroying the per-persona audit trail behind a historical score.
- **`scoring_rubric` in `src/personas.py` is dead** — defined 10 times, read by nothing; each system prompt duplicates its rubric inline. Editing the wrong copy is a silent no-op.
- **DEPLOY.md documents macOS** on a Linux host, including the reboot-survival instructions.
- **docs/HANDOFF.md publishes infrastructure coordinates** (`root@46.62.236.46`, key filename, dump path). Check repo visibility.

---

## Verification Gate

Before declaring this plan complete:

```bash
docker compose --profile test run --rm test              # expect >= 189 passed
docker exec ipo_postgres psql -U ipo -d ipo -c "SELECT count(*) FROM composite_score_history WHERE analysis_coverage < 7;"   # expect 0
docker exec ipo_postgres psql -U ipo -d ipo -c "SELECT status, count(*) FROM job_runs GROUP BY 1;"                            # expect no 'running' older than 1h
ls -la backups/                                          # expect >=1 automated ipo_<ts>.dump plus manual_20260720.dump
curl -s localhost:8000/api/stocks/IEX | python3 -c "import json,sys; print(json.load(sys.stdin)['quote']['roe'])"             # expect ~39.4
```

Then re-run the Measured Baseline queries and record the new values in this document. A plan that does not close its own loop on evidence is a plan that closed on hope.
