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

from conftest import PARITY_DB_NAME, PG_HOST, PG_PORT, PROJECT_ROOT
from db.base import Base
import db.models  # noqa: F401  (populate Base.metadata)

PARITY_DB = PARITY_DB_NAME
ADMIN_DSN = f"dbname=postgres user=ipo password=ipo host={PG_HOST} port={PG_PORT}"
PARITY_URL = f"postgresql+psycopg://ipo:ipo@{PG_HOST}:{PG_PORT}/{PARITY_DB}"
PARITY_DSN = f"dbname={PARITY_DB} user=ipo password=ipo host={PG_HOST} port={PG_PORT}"

# Provenance migration boundary: the revision immediately before it (schema without
# the *_source columns) and the revision that adds them + backfills 'unknown'.
PRE_PROVENANCE_REV = "53de03c90075"
PROVENANCE_REV = "d1f7a3c9e5b2"

# Composite-provenance boundary: the revision before prompt_versions/models_used
# exist on the composite tables, and the one that adds them + backfills the counts.
PRE_COMPOSITE_PROVENANCE_REV = "d1f7a3c9e5b2"
COMPOSITE_PROVENANCE_REV = "e2b5c8a41f96"

# Exchange-feed tables: added as new, empty tables (no backfill).
EXCHANGE_FEEDS_REV = "c7f1a9d3e820"
EXCHANGE_FEED_TABLES = (
    "corporate_announcements", "corporate_filings", "bulk_deals", "shareholding_patterns",
)

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


def _alembic(rev, env):
    return subprocess.run(
        ["alembic", "upgrade", rev],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )


def test_exchange_feed_tables_created_empty_and_backfill_free():
    """The four direct-feed tables are added as new, empty tables — nothing to
    backfill. Upgrading to their revision must create each with zero rows and leave
    the pre-existing schema untouched."""
    _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
    _admin(f"CREATE DATABASE {PARITY_DB}")
    try:
        env = {**os.environ, "DATABASE_URL": PARITY_URL}
        # Schema at the revision *before* the exchange feeds, then seed a stock so a
        # (hypothetical, unwanted) backfill would have something to touch.
        r = _alembic(COMPOSITE_PROVENANCE_REV, env)
        assert r.returncode == 0, f"pre-exchange-feeds upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO stocks (symbol, universe, status, first_seen, last_updated) "
                "VALUES ('SEED', '[]'::jsonb, 'active', now(), now())"
            )
        r = _alembic(EXCHANGE_FEEDS_REV, env)
        assert r.returncode == 0, f"exchange-feeds upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            for table in EXCHANGE_FEED_TABLES:
                (n,) = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                assert n == 0, f"{table} should be empty after a backfill-free migration"
    finally:
        _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")


def test_provenance_migration_backfills_preexisting_values_as_unknown():
    """The provenance migration must stamp pre-existing non-null sector/industry/isin
    'unknown' — they predate provenance and must not read as measured (identity_score
    gives an unmarked value full credit). A column that was already NULL stays NULL:
    there is nothing to disprove."""
    _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
    _admin(f"CREATE DATABASE {PARITY_DB}")
    try:
        env = {**os.environ, "DATABASE_URL": PARITY_URL}
        # Schema at the revision *before* provenance, then seed a legacy row:
        # sector + isin populated, industry NULL.
        r = _alembic(PRE_PROVENANCE_REV, env)
        assert r.returncode == 0, f"pre-provenance upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO stocks (symbol, sector, isin, universe, status, "
                "first_seen, last_updated) VALUES "
                "('OLDSTOCK', 'Technology', 'INE111A01011', '[]'::jsonb, 'active', "
                "now(), now())"
            )
        # Apply the provenance migration (adds the columns + backfills).
        r = _alembic(PROVENANCE_REV, env)
        assert r.returncode == 0, f"provenance upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            row = conn.execute(
                "SELECT sector_source, industry_source, isin_source "
                "FROM stocks WHERE symbol='OLDSTOCK'"
            ).fetchone()
        assert row == ("unknown", None, "unknown")
    finally:
        _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")


def test_composite_provenance_migration_backfills_counts_from_analyses():
    """The composite-provenance migration must stamp prompt_versions/models_used on
    pre-existing composites as a count-per-value across the SAME latest-per-persona
    selection recompute uses: a superseded older analysis is excluded, a NULL
    prompt_version/model counts as 'unknown', and a score-less analysis is ignored.
    The count is applied to composite_score_history rows for the stock too."""
    _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
    _admin(f"CREATE DATABASE {PARITY_DB}")
    try:
        env = {**os.environ, "DATABASE_URL": PARITY_URL}
        r = _alembic(PRE_COMPOSITE_PROVENANCE_REV, env)
        assert r.returncode == 0, f"pre-composite-provenance upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO stocks (symbol, universe, status, first_seen, last_updated) "
                "VALUES ('OLDCOMP', '[]'::jsonb, 'active', now(), now())"
            )
            (stock_id,) = conn.execute(
                "SELECT id FROM stocks WHERE symbol='OLDCOMP'"
            ).fetchone()
            conn.execute(
                "INSERT INTO composite_scores (stock_id, computed_at) "
                "VALUES (%s, now())", (stock_id,)
            )
            conn.execute(
                "INSERT INTO composite_score_history (stock_id, as_of_date) "
                "VALUES (%s, current_date)", (stock_id,)
            )
            # warren_buffett: latest v4/claude-fable-5 supersedes older v1/opus.
            # charlie_munger: v4/claude-fable-5. benjamin_graham: NULL/NULL -> unknown.
            # peter_lynch: score NULL -> not a contributing analysis, ignored.
            conn.execute(
                "INSERT INTO analyses "
                "(stock_id, persona, score, prompt_version, model, analyzed_at) VALUES "
                "(%(s)s, 'warren_buffett', 3, 'v1', 'opus', now() - interval '2 day'),"
                "(%(s)s, 'warren_buffett', 9, 'v4', 'claude-fable-5', now()),"
                "(%(s)s, 'charlie_munger', 8, 'v4', 'claude-fable-5', now()),"
                "(%(s)s, 'benjamin_graham', 4, NULL, NULL, now()),"
                "(%(s)s, 'peter_lynch', NULL, 'v4', 'claude-fable-5', now())",
                {"s": stock_id},
            )
        r = _alembic(COMPOSITE_PROVENANCE_REV, env)
        assert r.returncode == 0, f"composite-provenance upgrade failed:\n{r.stderr}"
        with psycopg.connect(PARITY_DSN, autocommit=True) as conn:
            cs = conn.execute(
                "SELECT prompt_versions, models_used FROM composite_scores "
                "WHERE stock_id=%s", (stock_id,)
            ).fetchone()
            hist = conn.execute(
                "SELECT prompt_versions, models_used FROM composite_score_history "
                "WHERE stock_id=%s", (stock_id,)
            ).fetchone()
        assert cs == ({"v4": 2, "unknown": 1}, {"claude-fable-5": 2, "unknown": 1})
        assert hist == ({"v4": 2, "unknown": 1}, {"claude-fable-5": 2, "unknown": 1})
    finally:
        _admin(f"DROP DATABASE IF EXISTS {PARITY_DB} WITH (FORCE)")
