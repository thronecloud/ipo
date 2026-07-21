"""Refresh + promotion lifecycle tests — engine/ingest/yf_refresh.py.

`fetch_payload` is monkeypatched so no network / yfinance call ever happens.
"""

from sqlalchemy import func, select

import engine.ingest.yf_refresh as yfr
from db.models import Stock, StockSnapshot
from factories import make_stock, yf_payload


def _reload(db_session, symbol: str) -> Stock:
    db_session.expire_all()
    return db_session.scalar(select(Stock).where(Stock.symbol == symbol))


def _snapshot_count(db_session, stock_id: int) -> int:
    return db_session.scalar(
        select(func.count(StockSnapshot.id)).where(StockSnapshot.stock_id == stock_id)
    )


def test_new_stock_success_promotes_to_active(db_session, monkeypatch):
    stock = make_stock(db_session, "NEWOK", status="new")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))

    stats = yfr.refresh(symbols=["NEWOK"], delay=0, verbose=False)

    assert stats["processed"] == 1
    assert stats["new_snapshot"] == 1
    assert stats["promoted"] == 1
    stock = _reload(db_session, "NEWOK")
    assert stock.status == "active"
    assert _snapshot_count(db_session, stock.id) == 1
    snap = db_session.scalar(
        select(StockSnapshot).where(StockSnapshot.stock_id == stock.id)
    )
    assert snap.data_quality == "full"
    assert snap.fetch_status == "success"
    assert snap.current_price == 100.0


def test_new_stock_first_error_gets_grace(db_session, monkeypatch):
    """A single transient error no longer exiles a freshly-discovered stock forever."""
    make_stock(db_session, "NEWBAD", status="new")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (None, "error: boom"))

    stats = yfr.refresh(symbols=["NEWBAD"], delay=0, verbose=False)

    assert stats["error"] == 1 and stats["soft_fail"] == 1
    assert stats["unfetchable"] == 0 and stats["promoted"] == 0
    stock = _reload(db_session, "NEWBAD")
    assert stock.status == "new"              # grace — stays in the pipeline
    assert stock.fetch_failures == 1


def test_new_stock_repeated_errors_park_unfetchable(db_session, monkeypatch):
    make_stock(db_session, "NEWGONE", status="new")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (None, "error: boom"))
    for _ in range(yfr.PARK_THRESHOLD):
        yfr.refresh(symbols=["NEWGONE"], delay=0, verbose=False)
    stock = _reload(db_session, "NEWGONE")
    assert stock.status == "unfetchable"      # exiled only after PARK_THRESHOLD attempts
    assert stock.fetch_failures >= yfr.PARK_THRESHOLD


def test_new_stock_minimal_data_not_promoted(db_session, monkeypatch):
    """Below-viability (minimal) data must not graduate a stock into analysis."""
    make_stock(db_session, "NEWMIN", status="new")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "minimal"))
    stats = yfr.refresh(symbols=["NEWMIN"], delay=0, verbose=False)
    stock = _reload(db_session, "NEWMIN")
    assert stats["promoted"] == 0 and stats["soft_fail"] == 1
    assert stock.status == "new"
    assert stock.fetch_failures == 1          # soft failure, not a clean success


def test_parked_stock_revives_on_success(db_session, monkeypatch):
    make_stock(db_session, "REVIVE", status="unfetchable")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))
    stats = yfr.refresh(symbols=["REVIVE"], delay=0, verbose=False)
    stock = _reload(db_session, "REVIVE")
    assert stats["revived"] == 1
    assert stock.status == "active"           # recovered symbol auto-revived


def test_new_stock_without_symbol_parked_unfetchable(db_session, monkeypatch):
    make_stock(db_session, "NOSYM", status="new", yf_symbol=None)

    def _boom(sym):  # must never be called: no yf_symbol short-circuits
        raise AssertionError("fetch_payload should not be called")

    monkeypatch.setattr(yfr, "fetch_payload", _boom)
    stats = yfr.refresh(symbols=["NOSYM"], delay=0, verbose=False)

    assert stats["no_symbol"] == 1
    assert _reload(db_session, "NOSYM").status == "unfetchable"


def test_active_stock_fetch_error_stays_active(db_session, monkeypatch):
    stock = make_stock(db_session, "ACTERR", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (None, "error: transient"))

    stats = yfr.refresh(symbols=["ACTERR"], delay=0, verbose=False)

    assert stats["error"] == 1
    assert stats["unfetchable"] == 0
    stock = _reload(db_session, "ACTERR")
    assert stock.status == "active"
    assert _snapshot_count(db_session, stock.id) == 0


def test_unchanged_payload_is_noop(db_session, monkeypatch):
    stock = make_stock(db_session, "SAME", status="active")
    payload = yf_payload()
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    stats1 = yfr.refresh(symbols=["SAME"], delay=0, verbose=False)
    stats2 = yfr.refresh(symbols=["SAME"], delay=0, verbose=False)

    assert stats1["new_snapshot"] == 1
    assert stats2["new_snapshot"] == 0
    assert stats2["unchanged"] == 1
    stock = _reload(db_session, "SAME")
    assert _snapshot_count(db_session, stock.id) == 1


def test_changed_payload_appends_snapshot(db_session, monkeypatch):
    stock = make_stock(db_session, "CHANGED", status="active")
    # vary a fundamental (revenue) so the hash actually changes (price is volatile)
    payloads = iter([(yf_payload(revenue=1000.0), "full"), (yf_payload(revenue=2000.0), "full")])
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: next(payloads))

    yfr.refresh(symbols=["CHANGED"], delay=0, verbose=False)
    stats2 = yfr.refresh(symbols=["CHANGED"], delay=0, verbose=False)

    assert stats2["new_snapshot"] == 1
    stock = _reload(db_session, "CHANGED")
    assert _snapshot_count(db_session, stock.id) == 2


def test_promote_false_leaves_new_status(db_session, monkeypatch):
    make_stock(db_session, "NOPROMO", status="new")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))

    stats = yfr.refresh(symbols=["NOPROMO"], delay=0, verbose=False, promote=False)

    assert stats["new_snapshot"] == 1
    assert stats["promoted"] == 0
    assert _reload(db_session, "NOPROMO").status == "new"


def test_refresh_records_rebased_bars_in_job_stats(db_session, monkeypatch):
    """A corporate action rewrites stored history. That rewrite must leave a durable,
    queryable record — stdout logs die with the container."""
    from datetime import date
    from sqlalchemy import select as sa_select
    from db.models import JobRun
    from engine.repo import upsert_daily_prices

    stock = make_stock(db_session, "REBASED")
    bars = [{"date": date(2026, 1, 5), "open": 1000.0, "high": 1010.0,
             "low": 990.0, "close": 1000.0, "volume": 1}]
    upsert_daily_prices(db_session, stock.id, bars)
    db_session.commit()

    split = [dict(bars[0], open=100.0, high=101.0, low=99.0, close=100.0)]
    monkeypatch.setattr(yfr, "fetch_payload",
                        lambda sym: ({**yf_payload(), "_price_rows": split}, "full"))
    stats = yfr.refresh(symbols=["REBASED"], delay=0, verbose=False)

    assert stats["bars_rebased"] == 1
    job = db_session.scalar(
        sa_select(JobRun).where(JobRun.job_type == "refresh")
        .order_by(JobRun.id.desc()).limit(1)
    )
    assert job.stats["bars_rebased"] == 1


def test_backfill_records_rebased_bars_without_inflating_added(db_session, monkeypatch):
    """The backfill path also corrects rebased bars; that must be tallied under
    bars_rebased, not counted as bars_added."""
    from datetime import date
    from engine.repo import upsert_daily_prices

    stock = make_stock(db_session, "BFREBASE", status="active")
    bars = [{"date": date(2026, 1, 5), "open": 1000.0, "high": 1010.0,
             "low": 990.0, "close": 1000.0, "volume": 1}]
    upsert_daily_prices(db_session, stock.id, bars)
    db_session.commit()

    split = [dict(bars[0], open=100.0, high=101.0, low=99.0, close=100.0)]
    monkeypatch.setattr(yfr, "_history_rows", lambda sym: (split, False))
    stats = yfr.backfill_prices(symbols=["BFREBASE"], delay=0, verbose=False)

    assert stats["bars_rebased"] == 1
    assert stats["bars_added"] == 0
