"""X2 — ORM ⇔ Alembic-migration parity guard.

The rest of the suite builds the schema with `create_all` (the ORM), but production
runs Alembic migrations. They can silently diverge (an untracked model, a missing
migration). This test builds a fresh DB *via migrations only* and asserts the ORM
metadata autogenerates NO structural difference against it — so a model added without
a migration (or vice-versa) fails CI.
"""

import os
import subprocess

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from conftest import PG_HOST, PG_PORT, PROJECT_ROOT
from db.base import Base
import db.models  # noqa: F401  (populate Base.metadata)

PARITY_DB = "ipo_migparity"
ADMIN_DSN = f"dbname=postgres user=ipo password=ipo host={PG_HOST} port={PG_PORT}"
PARITY_URL = f"postgresql+psycopg://ipo:ipo@{PG_HOST}:{PG_PORT}/{PARITY_DB}"

# Structural drift we refuse to allow. modify_*/index reflection noise is ignored;
# a new/removed table or column (the untracked-model trap) is not.
STRUCTURAL = {"add_table", "remove_table", "add_column", "remove_column"}


def _admin(sql):
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql)


def _structural(diffs):
    out = []
    for d in diffs:
        # diffs are tuples or lists-of-tuples (grouped by table)
        entries = d if isinstance(d, list) else [d]
        for e in entries:
            if isinstance(e, tuple) and e and e[0] in STRUCTURAL:
                out.append(e[0:1] + tuple(
                    getattr(x, "name", x) for x in e[1:] if not hasattr(x, "columns")
                ))
    return out


def test_orm_matches_migration_head():
    _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
    _admin(f"CREATE DATABASE {PARITY_DB}")
    try:
        env = {**os.environ, "DATABASE_URL": PARITY_URL}
        r = subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0, f"alembic upgrade head failed:\n{r.stderr}"

        engine = create_engine(PARITY_URL)
        try:
            with engine.connect() as conn:
                diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        finally:
            engine.dispose()

        drift = _structural(diffs)
        assert not drift, (
            "ORM models and Alembic migration head disagree on tables/columns — "
            f"ship the missing migration:\n{drift}"
        )
    finally:
        _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
