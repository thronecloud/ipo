"""Concurrency & idempotency invariants (P0-1, P1-6).

Uses two real SessionLocal() sessions against the ipo_test DB — not mocks — so the
tests exercise the actual Postgres constraints and locking.
"""

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.base import SessionLocal
from db.models import CompositeScore, Stock
from engine.repo import recompute_scores_for_stock
from factories import make_analysis, make_stock, utc


def _seed_scored_stock(session, symbol):
    stock = make_stock(session, symbol)
    for i, persona in enumerate(
        ["warren_buffett", "charlie_munger", "benjamin_graham"]
    ):
        make_analysis(session, stock, persona, 6, "HOLD", analyzed_at=utc(-i))
    return stock


def test_composite_unique_constraint(db_session):
    """Two composite rows for one stock must be impossible."""
    stock = make_stock(db_session, "UNIQ1")
    db_session.add(CompositeScore(stock_id=stock.id, composite_score=50.0))
    db_session.commit()
    db_session.add(CompositeScore(stock_id=stock.id, composite_score=60.0))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_recompute_is_idempotent_single_row(db_session):
    stock = _seed_scored_stock(db_session, "UNIQ2")
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    n = db_session.scalar(
        select(func.count()).select_from(CompositeScore).where(CompositeScore.stock_id == stock.id)
    )
    assert n == 1


def test_concurrent_recompute_yields_one_row(db_session):
    """Two OS threads with independent sessions recompute the same stock at once."""
    stock = _seed_scored_stock(db_session, "UNIQ3")
    db_session.commit()
    sid = stock.id
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        s = SessionLocal()
        try:
            st = s.get(Stock, sid)
            barrier.wait()
            recompute_scores_for_stock(s, st)
            s.commit()
        except Exception as e:  # pragma: no cover - surfaced via errors list
            errors.append(e)
            s.rollback()
        finally:
            s.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"concurrent recompute raised: {errors}"
    n = db_session.scalar(
        select(func.count()).select_from(CompositeScore).where(CompositeScore.stock_id == sid)
    )
    assert n == 1


def test_add_snapshot_idempotent_no_duplicate(db_session):
    from db.models import StockSnapshot
    from engine.repo import add_snapshot, extract_columns
    from factories import yf_payload
    stock = make_stock(db_session, "SNAP1")
    p = yf_payload()
    s1, c1 = add_snapshot(db_session, stock, p, extract_columns(p["info"]), data_quality="full")
    db_session.commit()
    s2, c2 = add_snapshot(db_session, stock, p, extract_columns(p["info"]), data_quality="full")
    db_session.commit()
    assert c1 is True and c2 is False and s1.id == s2.id
    assert db_session.scalar(
        select(func.count()).select_from(StockSnapshot).where(StockSnapshot.stock_id == stock.id)
    ) == 1


def test_upsert_daily_prices_conflict_safe(db_session):
    from datetime import date
    from db.models import DailyPrice
    from engine.repo import upsert_daily_prices
    stock = make_stock(db_session, "PX1")
    rows = [{"date": date(2026, 1, d), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1} for d in range(1, 6)]
    assert upsert_daily_prices(db_session, stock.id, rows) == 5
    db_session.commit()
    # overlap: days 4,5 exist; 6,7 new -> 2 inserted, no IntegrityError
    rows2 = [{"date": date(2026, 1, d), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1} for d in range(4, 8)]
    assert upsert_daily_prices(db_session, stock.id, rows2) == 2
    db_session.commit()
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 7


def test_concurrent_get_or_create_same_symbol(db_session):
    """Two sessions create the same new symbol at once → one row, no IntegrityError."""
    from engine.repo import get_or_create_stock
    db_session.commit()
    barrier = threading.Barrier(2)
    errors, ids = [], []

    def worker():
        s = SessionLocal()
        try:
            barrier.wait()
            st = get_or_create_stock(s, "RACESYM", company_name="Race Co")
            s.commit()
            ids.append(st.id)
        except Exception as e:  # pragma: no cover
            errors.append(e)
            s.rollback()
        finally:
            s.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, f"concurrent get_or_create raised: {errors}"
    assert db_session.scalar(
        select(func.count()).select_from(Stock).where(Stock.symbol == "RACESYM")
    ) == 1


def test_reconcile_heals_orphaned_composite(db_session):
    """Analyses present but composite missing (interrupted batch) -> reconcile recreates it."""
    from engine.repo import reconcile_scores
    stock = _seed_scored_stock(db_session, "UNIQ4")
    recompute_scores_for_stock(db_session, stock)
    db_session.commit()
    # simulate a lost composite (crash between analysis commit and rescore)
    db_session.query(CompositeScore).filter(CompositeScore.stock_id == stock.id).delete()
    db_session.commit()
    assert db_session.scalar(
        select(func.count()).select_from(CompositeScore).where(CompositeScore.stock_id == stock.id)
    ) == 0
    healed = reconcile_scores(db_session)
    db_session.commit()
    assert healed >= 1
    assert db_session.scalar(
        select(func.count()).select_from(CompositeScore).where(CompositeScore.stock_id == stock.id)
    ) == 1
