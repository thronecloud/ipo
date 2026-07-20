"""
Test bootstrap for the WisdomInvest living engine.

Critical ordering: DATABASE_URL must point at the throwaway `ipo_test` database
BEFORE db.base is imported anywhere (db.base builds its engine at import time,
and its load_dotenv() call never overrides variables already in the environment).
The production `ipo` database is never touched.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for p in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("TEST_PG_PORT", "5432")
TEST_DB_NAME = "ipo_test"
TEST_DATABASE_URL = f"postgresql+psycopg://ipo:ipo@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"

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


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create a fresh ipo_test database for the session; drop it afterwards."""
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
