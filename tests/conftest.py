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
