"""Daily OHLCV ingestion: upsert + corporate-action rebase + refresh_one wiring.

`fetch_payload` is monkeypatched so no network / yfinance call ever happens.
"""

from datetime import date

from sqlalchemy import func, select

from db.models import DailyPrice
from engine import repo
from engine.ingest import yf_refresh as yfr
from tests.factories import make_stock, yf_payload


def _bars(start_day: int, n: int) -> list[dict]:
    """Bars whose values are a function of the DATE, not of the window offset — the
    provider returns the same bar for the same day on every fetch, so overlapping
    windows must agree. (Values keyed to the offset would make every overlapping
    re-fetch look like a corporate-action rebase.)"""
    return [
        {
            "date": date(2026, 1, day),
            "open": 100.0 + day,
            "high": 105.0 + day,
            "low": 99.0 + day,
            "close": 102.0 + day,
            "volume": 1000 + day,
        }
        for day in range(start_day, start_day + n)
    ]


def test_upsert_inserts_only_new_dates(db_session):
    stock = make_stock(db_session, "PRICES1")

    inserted = repo.upsert_daily_prices(db_session, stock.id, _bars(1, 3))
    db_session.commit()
    assert inserted == 3
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 3

    # Overlapping range (2 old dates + 2 new): only the 2 new ones land.
    inserted2 = repo.upsert_daily_prices(db_session, stock.id, _bars(2, 4))
    db_session.commit()
    assert inserted2 == 2
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 5


def test_upsert_empty_is_noop(db_session):
    stock = make_stock(db_session, "PRICES2")
    assert repo.upsert_daily_prices(db_session, stock.id, []) == 0


def test_stored_bar_values_roundtrip(db_session):
    stock = make_stock(db_session, "PRICES3")
    repo.upsert_daily_prices(db_session, stock.id, _bars(10, 1))
    db_session.commit()
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert (bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        date(2026, 1, 10), 110.0, 115.0, 109.0, 112.0, 1010,
    )


def test_refresh_one_persists_price_rows(db_session, monkeypatch):
    """refresh_one pops _price_rows from the payload into daily_prices."""
    stock = make_stock(db_session, "PRICES4", status="active")
    payload = yf_payload()
    payload["_price_rows"] = _bars(1, 5)
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    res = yfr.refresh_one(db_session, stock)
    db_session.commit()

    assert res == "new_snapshot"
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 5


def test_refresh_one_without_price_rows_is_safe(db_session, monkeypatch):
    """Legacy payloads with no _price_rows key just store zero bars (no crash)."""
    stock = make_stock(db_session, "PRICES5", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))

    res = yfr.refresh_one(db_session, stock)
    db_session.commit()

    assert res == "new_snapshot"
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 0


# ---------- corporate-action rebase ----------

def test_reingesting_split_adjusted_history_corrects_stored_bars(db_session):
    """After a 1:10 split Yahoo rebases history. Append-only storage would keep the
    pre-split closes and create a -90% cliff that never happened."""
    stock = make_stock(db_session, "SPLITCO")
    repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": 1000.0, "high": 1010.0,
         "low": 990.0, "close": 1000.0, "volume": 1},
    ])
    db_session.commit()

    # Same date, rebased 1:10 by the provider. Nothing is INSERTED — the existing bar
    # is rewritten — so the return is 0 and the correction shows up under bars_rebased.
    counts: dict = {}
    inserted = repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "volume": 10},
    ], counts=counts)
    db_session.commit()

    bar = db_session.scalar(select(DailyPrice).where(
        DailyPrice.stock_id == stock.id, DailyPrice.date == date(2026, 1, 5)))
    assert inserted == 0
    assert counts["bars_rebased"] == 1
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        100.0, 101.0, 99.0, 100.0, 10)


def test_identical_reingest_writes_nothing(db_session):
    """The nightly refresh re-sends the whole `period=max` series. Bars whose values
    are unchanged must not be rewritten — otherwise every refresh churns the table."""
    stock = make_stock(db_session, "NOCHURN")
    rows = _bars(1, 20)
    counts: dict = {}
    assert repo.upsert_daily_prices(db_session, stock.id, rows) == 20
    db_session.commit()

    # A re-send of the identical series must touch nothing: no insert AND no rewrite.
    # (The inserted-only return is 0 whether or not unchanged bars are rewritten, so the
    # rebase tally is what actually proves the where-clause suppressed the writes.)
    assert repo.upsert_daily_prices(db_session, stock.id, rows, counts=counts) == 0
    db_session.commit()
    assert repo.upsert_daily_prices(db_session, stock.id, rows, counts=counts) == 0
    db_session.commit()
    assert counts.get("bars_rebased", 0) == 0


def test_subtolerance_float_noise_does_not_rewrite(db_session):
    """Float round-trip noise is not a corporate action. A bar differing in the last
    bits must not be rewritten, or it would churn nightly forever."""
    stock = make_stock(db_session, "FUZZ")
    repo.upsert_daily_prices(db_session, stock.id, _bars(1, 1))
    db_session.commit()

    noisy = _bars(1, 1)
    noisy[0]["close"] = 103.0 * (1 + 1e-13)
    noisy[0]["open"] = 101.0 * (1 - 1e-13)
    counts: dict = {}
    assert repo.upsert_daily_prices(db_session, stock.id, noisy, counts=counts) == 0
    assert counts.get("bars_rebased", 0) == 0  # sub-tolerance noise is not a rewrite


def test_null_close_is_repaired_by_reingest(db_session):
    """A bar stored with a NULL close is a hole; a later fetch carrying a real
    value must fill it."""
    stock = make_stock(db_session, "NULLFIX")
    repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": None, "high": None,
         "low": None, "close": None, "volume": None},
    ])
    db_session.commit()

    # Filling the hole rewrites the existing bar, so nothing is inserted; the repair
    # is visible as a rebase.
    counts: dict = {}
    assert repo.upsert_daily_prices(db_session, stock.id, _bars(5, 1), counts=counts) == 0
    db_session.commit()
    assert counts["bars_rebased"] == 1
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert bar.close == 107.0


def test_duplicate_date_payload_stores_last_row(db_session):
    """yfinance sometimes serves a frame with the same date twice. ON CONFLICT DO UPDATE
    would raise CardinalityViolation on the dup, so the payload is deduped first — the
    LAST occurrence (the provider's later word) is the one stored."""
    stock = make_stock(db_session, "DUPDATE")
    inserted = repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": 100.0, "high": 105.0,
         "low": 99.0, "close": 102.0, "volume": 1000},
        {"date": date(2026, 1, 5), "open": 200.0, "high": 205.0,
         "low": 199.0, "close": 202.0, "volume": 2000},
    ])
    db_session.commit()
    assert inserted == 1
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        200.0, 205.0, 199.0, 202.0, 2000)


def test_all_null_incoming_does_not_degrade_stored_bar(db_session):
    """A glitched fetch row (every field NaN->None) must not overwrite a good stored bar
    with NULLs. It carries no data, so it is a complete no-op: nothing inserted, nothing
    rebased, stored values intact."""
    stock = make_stock(db_session, "NULLGLITCH")
    repo.upsert_daily_prices(db_session, stock.id, _bars(5, 1))
    db_session.commit()

    counts: dict = {}
    inserted = repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": None, "high": None,
         "low": None, "close": None, "volume": None},
    ], counts=counts)
    db_session.commit()
    assert inserted == 0
    assert counts.get("bars_rebased", 0) == 0
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        105.0, 110.0, 104.0, 107.0, 1005)


def test_partial_row_corrects_carried_field_and_preserves_the_rest(db_session):
    """A row with open=None but a genuinely changed close corrects the close and leaves
    the stored open intact — coalesce writes the field it carries and preserves the rest."""
    stock = make_stock(db_session, "PARTIAL")
    repo.upsert_daily_prices(db_session, stock.id, _bars(5, 1))  # open=105.0, close=107.0
    db_session.commit()

    counts: dict = {}
    inserted = repo.upsert_daily_prices(db_session, stock.id, [
        {"date": date(2026, 1, 5), "open": None, "high": 110.0,
         "low": 104.0, "close": 10.7, "volume": 1005},
    ], counts=counts)
    db_session.commit()
    assert inserted == 0
    assert counts["bars_rebased"] == 1
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert bar.open == 105.0     # preserved — incoming NULL never degrades it
    assert bar.close == 10.7     # corrected


def test_rebase_count_is_reported_to_the_caller(db_session):
    """A rebase rewrites point-in-time research input. `log()` is a bare print, so
    without an accumulator the only record is an ephemeral container log."""
    stock = make_stock(db_session, "COUNTED")
    repo.upsert_daily_prices(db_session, stock.id, _bars(1, 3))
    db_session.commit()

    counts: dict = {}
    rebased = [dict(b, close=b["close"] / 10) for b in _bars(1, 3)]
    repo.upsert_daily_prices(db_session, stock.id, rebased, counts=counts)
    db_session.commit()
    assert counts["bars_rebased"] == 3

    # Insert-only work leaves the tally untouched.
    repo.upsert_daily_prices(db_session, stock.id, _bars(10, 2), counts=counts)
    db_session.commit()
    assert counts["bars_rebased"] == 3
