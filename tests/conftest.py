"""
Test bootstrap for the WisdomInvest living engine.

Critical ordering: DATABASE_URL must point at the throwaway test database
BEFORE db.base is imported anywhere (db.base builds its engine at import time,
and its load_dotenv() call never overrides variables already in the environment).
The production `ipo` database is never touched.

The database name carries a per-run id so that suites running concurrently (one
worktree per implementation agent) cannot truncate each other's fixtures. The id
is computed once at import, so every fixture, every test and the migration-parity
database in test_migrations.py agree on it for the whole invocation.
"""

import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("TEST_PG_PORT", "5432")

TEST_DB_PREFIX = "ipo_test_"
PARITY_DB_PREFIX = "ipo_migparity_"


def _run_id() -> str:
    """Stable within one pytest invocation, distinct across concurrent ones.

    TEST_RUN_ID lets a caller pin the name (useful for debugging a leftover
    database); anything unset or unusable falls back to a fresh uuid. Stripping
    to [a-z0-9] keeps the result a safe bare SQL identifier and makes it
    structurally impossible for the prefixed name to collide with `ipo`.
    """
    cleaned = re.sub(r"[^a-z0-9]", "", os.environ.get("TEST_RUN_ID", "").lower())[:24]
    return cleaned or uuid.uuid4().hex[:12]


TEST_RUN_ID = _run_id()
TEST_DB_NAME = f"{TEST_DB_PREFIX}{TEST_RUN_ID}"
PARITY_DB_NAME = f"{PARITY_DB_PREFIX}{TEST_RUN_ID}"
TEST_DATABASE_URL = f"postgresql+psycopg://ipo:ipo@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"

# Belt-and-braces on _run_id()'s sanitising: the suite must never be able to
# address the production database, whatever TEST_RUN_ID contained.
assert TEST_DB_NAME.startswith(TEST_DB_PREFIX) and TEST_DB_NAME != "ipo"
assert PARITY_DB_NAME.startswith(PARITY_DB_PREFIX) and PARITY_DB_NAME != "ipo"

# MUST happen before any project import pulls in db.base.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Second line of defence behind engine.notify's PYTEST_CURRENT_TEST guard, which
# cannot cover two cases: PYTEST_CURRENT_TEST is unset outside a test's own
# phases (collection, session-fixture teardown), and it is not inherited in a
# meaningful way by an engine subprocess. Empty (not absent) is what blocks it:
# db.base's load_dotenv() skips keys already present, so this stops the real
# topic from ever entering the environment the suite or its children see.
os.environ["NTFY_TOPIC"] = ""
os.environ["NOTIFY_WEBHOOK_URL"] = ""

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Maintenance connection: user ipo -> db `postgres` (never the prod `ipo` db).
ADMIN_DSN = f"dbname=postgres user=ipo password=ipo host={PG_HOST} port={PG_PORT}"


def _admin_execute(sql: str) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql)


def _run_lock_key(run_id: str) -> int:
    """Signed 64-bit advisory-lock key naming a run, not a single database.

    One key covers both databases a run owns (its own and the parity one), so a
    sweeper can ask a single question: is anybody still running as `run_id`?
    """
    digest = hashlib.blake2b(run_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _sweep_orphan_databases(conn) -> None:
    """Drop test databases left behind by runs that are no longer alive.

    Unique names lose the self-healing the old fixed `ipo_test` name had: a run
    killed mid-suite (SIGKILL, a dead container) never reaches its teardown, and
    its database lingers forever. Emptiness checks can't distinguish an orphan
    from a live run that is between connections, so ownership is asserted
    explicitly instead: every run holds a session-level advisory lock on its own
    id for the whole invocation, taken *before* it creates any database. A key
    this connection can acquire therefore belongs to nobody, and that run's
    leftovers are safe to drop. Sweeping is best-effort — a database another
    process is mid-drop on must not fail the suite that noticed it.
    """
    rows = conn.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE %s OR datname LIKE %s",
        (rf"{TEST_DB_PREFIX}%".replace("_", r"\_"),
         rf"{PARITY_DB_PREFIX}%".replace("_", r"\_")),
    ).fetchall()

    for (name,) in rows:
        for prefix in (TEST_DB_PREFIX, PARITY_DB_PREFIX):
            if name.startswith(prefix):
                run_id = name[len(prefix):]
                break
        else:
            continue
        if run_id == TEST_RUN_ID:
            continue

        key = _run_lock_key(run_id)
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
            continue  # that run is still alive; leave its database alone
        try:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except psycopg.Error:
            pass
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def pytest_report_header(config):
    return f"test database: {TEST_DB_NAME}"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create this run's throwaway database; drop it (and any orphans) around."""
    # Held open for the whole session: closing it releases the ownership lock
    # and makes this run's databases sweepable by the next one.
    owner = psycopg.connect(ADMIN_DSN, autocommit=True)
    owner.execute("SELECT pg_advisory_lock(%s)", (_run_lock_key(TEST_RUN_ID),))
    _sweep_orphan_databases(owner)

    _admin_execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
    _admin_execute(f"CREATE DATABASE {TEST_DB_NAME}")

    import db.base as db_base

    # Hard safety stop: if db.base got imported before this module set the env
    # var, its engine points at the production database. Never proceed.
    assert TEST_DB_NAME in db_base.DATABASE_URL, (
        f"db.base is bound to {db_base.DATABASE_URL!r}, not the {TEST_DB_NAME} "
        "database — refusing to run tests against it."
    )

    import db.models  # noqa: F401  (populate Base.metadata)

    db_base.Base.metadata.create_all(db_base.engine)
    yield
    db_base.engine.dispose()
    _admin_execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
    _admin_execute(f"DROP DATABASE IF EXISTS {PARITY_DB_NAME} WITH (FORCE)")
    owner.close()


@pytest.fixture()
def db_session(test_database):
    """Per-test session against a truncated (empty) database."""
    import db.base as db_base

    tables = ", ".join(t.name for t in db_base.Base.metadata.sorted_tables)
    with db_base.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    session = db_base.SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def seeded_stock_with_quote(db_session):
    """One stock whose latest yfinance snapshot carries yfinance-native scales:
    roe/revenue_growth as fractions, debt_to_equity as a percentage. Returns
    the symbol. Canonical output is roe 25.0, revenue_growth 30.0, d/e 1.5."""
    from engine.repo import add_snapshot, extract_columns
    from factories import full_info, make_stock, utc, yf_payload

    stock = make_stock(db_session, "UNITS", company_name="Units Ltd",
                       universe=["ipo_2025"], issue_price=100.0)
    payload = yf_payload(price=120.0)
    payload["info"] = full_info(
        price=120.0,
        returnOnEquity=0.25,
        revenueGrowth=0.30,
        debtToEquity=150.0,
    )
    add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                 data_quality="full", captured_at=utc(-1))
    db_session.commit()
    return stock.symbol
